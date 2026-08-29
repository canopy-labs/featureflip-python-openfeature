"""Tests for the FeatureflipProvider itself.

The provider is driven against a fake client rather than the real SDK: these
assert the provider's contract (ownership, type guards, event republishing), and
the SDK's own behaviour is covered by its suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from featureflip import EvaluationDetail, EvaluationReason
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEvent, ProviderEventDetails
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import Reason

from featureflip_openfeature import FeatureflipProvider
from featureflip_openfeature.mapping import PREREQUISITE_FAILED

if TYPE_CHECKING:
    from openfeature.provider import FeatureProvider


class FakeClient:
    """Stands in for `featureflip.FeatureflipClient`."""

    def __init__(self, detail: EvaluationDetail | None = None) -> None:
        self.detail = detail or EvaluationDetail(
            value=True, reason=EvaluationReason.FALLTHROUGH, variation_key="on"
        )
        self.closed = False
        self.listeners: list[Any] = []
        self.tracked: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
        self.seen_contexts: list[dict[str, Any]] = []
        self.raises: Exception | None = None

    def variation_detail(
        self,
        key: str,  # noqa: ARG002 - part of the SDK signature this fake stands in for
        context: dict[str, Any],
        default: Any,  # noqa: ARG002 - ditto
    ) -> EvaluationDetail:
        if self.raises is not None:
            raise self.raises
        self.seen_contexts.append(context)
        return self.detail

    def track(
        self, event_name: str, context: dict[str, Any], metadata: dict[str, Any] | None
    ) -> None:
        self.tracked.append((event_name, context, metadata))

    def on_update(self, listener: Any) -> Any:
        self.listeners.append(listener)

        def unsubscribe() -> None:
            self.listeners.remove(listener)

        return unsubscribe

    def close(self) -> None:
        self.closed = True

    def emit_update(self, keys: list[str]) -> None:
        for listener in list(self.listeners):
            listener(keys)


def make_provider(client: FakeClient) -> FeatureflipProvider:
    provider = FeatureflipProvider(client=client)
    provider.initialize(EvaluationContext())
    return provider


def capture_events(provider: FeatureflipProvider) -> list[tuple[Any, Any]]:
    """Attach an emit sink the way OpenFeature's provider registry does."""
    captured: list[tuple[Any, Any]] = []

    def on_emit(
        _provider: FeatureProvider, event: Any, details: ProviderEventDetails
    ) -> None:
        captured.append((event, details))

    provider.attach(on_emit)
    return captured


class TestConstruction:
    def test_requires_exactly_one_of_sdk_key_or_client(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FeatureflipProvider()
        with pytest.raises(ValueError, match="exactly one"):
            FeatureflipProvider(sdk_key="k", client=FakeClient())  # type: ignore[arg-type]

    def test_metadata_names_the_provider(self) -> None:
        provider = FeatureflipProvider(client=FakeClient())  # type: ignore[arg-type]
        assert provider.get_metadata().name == "featureflip-python"

    def test_sdk_key_form_does_not_build_a_client_until_initialize(self) -> None:
        """`FeatureflipClient.__init__` blocks; that wait belongs in initialize()."""
        provider = FeatureflipProvider(sdk_key="sdk-key")
        assert provider._client is None


class TestResolution:
    def test_boolean_resolution(self) -> None:
        provider = make_provider(FakeClient())
        details = provider.resolve_boolean_details("flag", False)
        assert details.value is True
        assert details.reason == Reason.DEFAULT.value
        assert details.variant == "on"

    def test_string_resolution(self) -> None:
        client = FakeClient(
            EvaluationDetail(value="blue", reason=EvaluationReason.RULE_MATCH)
        )
        details = make_provider(client).resolve_string_details("flag", "red")
        assert details.value == "blue"
        assert details.reason == Reason.TARGETING_MATCH.value

    def test_integer_resolution(self) -> None:
        client = FakeClient(EvaluationDetail(value=42, reason=EvaluationReason.FALLTHROUGH))
        assert make_provider(client).resolve_integer_details("flag", 0).value == 42

    def test_whole_number_float_satisfies_an_integer_read(self) -> None:
        """JSON does not distinguish 1 from 1.0, so neither can the guard."""
        client = FakeClient(
            EvaluationDetail(value=42.0, reason=EvaluationReason.FALLTHROUGH)
        )
        details = make_provider(client).resolve_integer_details("flag", 0)
        assert details.value == 42
        assert isinstance(details.value, int)
        assert details.error_code is None

    def test_fractional_float_does_not_satisfy_an_integer_read(self) -> None:
        client = FakeClient(
            EvaluationDetail(value=1.5, reason=EvaluationReason.FALLTHROUGH)
        )
        details = make_provider(client).resolve_integer_details("flag", 7)
        assert details.value == 7
        assert details.error_code == ErrorCode.TYPE_MISMATCH

    def test_float_resolution_widens_an_integer_value(self) -> None:
        client = FakeClient(EvaluationDetail(value=3, reason=EvaluationReason.FALLTHROUGH))
        details = make_provider(client).resolve_float_details("flag", 0.0)
        assert details.value == 3.0
        assert isinstance(details.value, float)

    def test_object_resolution(self) -> None:
        payload = {"theme": "dark", "items": [1, 2]}
        client = FakeClient(
            EvaluationDetail(value=payload, reason=EvaluationReason.FALLTHROUGH)
        )
        assert make_provider(client).resolve_object_details("flag", {}).value == payload

    def test_prerequisite_failed_surfaces_verbatim(self) -> None:
        client = FakeClient(
            EvaluationDetail(
                value=False,
                reason=EvaluationReason.PREREQUISITE_FAILED,
                prerequisite_key="base",
            )
        )
        details = make_provider(client).resolve_boolean_details("flag", True)
        assert details.reason == PREREQUISITE_FAILED
        assert details.flag_metadata == {"prerequisiteKey": "base"}
        # Not an error: the flag resolved, to its off variation.
        assert details.error_code is None

    def test_context_is_mapped_before_evaluation(self) -> None:
        client = FakeClient()
        provider = make_provider(client)
        provider.resolve_boolean_details(
            "flag", False, EvaluationContext(targeting_key="u1", attributes={"plan": "pro"})
        )
        assert client.seen_contexts == [{"user_id": "u1", "plan": "pro"}]


class TestTypeMismatch:
    @pytest.mark.parametrize(
        ("value", "accessor", "default"),
        [
            ("not-a-bool", "resolve_boolean_details", False),
            (1, "resolve_boolean_details", False),
            (True, "resolve_string_details", "fallback"),
            ("nope", "resolve_integer_details", 7),
            ("nope", "resolve_float_details", 1.5),
            ("a string", "resolve_object_details", {}),
        ],
    )
    def test_mismatch_returns_the_caller_default_with_type_mismatch(
        self, value: Any, accessor: str, default: Any
    ) -> None:
        client = FakeClient(
            EvaluationDetail(value=value, reason=EvaluationReason.FALLTHROUGH)
        )
        details = getattr(make_provider(client), accessor)("flag", default)
        assert details.value == default
        assert details.reason == Reason.ERROR.value
        assert details.error_code == ErrorCode.TYPE_MISMATCH

    @pytest.mark.parametrize(
        "accessor", ["resolve_integer_details", "resolve_float_details"]
    )
    def test_a_boolean_never_satisfies_a_numeric_read(self, accessor: str) -> None:
        """`bool` subclasses `int`, so an unguarded check would hand back 1."""
        client = FakeClient(
            EvaluationDetail(value=True, reason=EvaluationReason.FALLTHROUGH)
        )
        details = getattr(make_provider(client), accessor)("flag", 0)
        assert details.error_code == ErrorCode.TYPE_MISMATCH
        assert details.value == 0

    def test_error_reasons_are_not_relabelled_as_type_mismatch(self) -> None:
        """An error detail can carry a wrong-typed off-value; that is not a mismatch."""
        client = FakeClient(
            EvaluationDetail(value="wrong-typed-off-value", reason=EvaluationReason.ERROR)
        )
        details = make_provider(client).resolve_boolean_details("flag", False)
        assert details.error_code == ErrorCode.GENERAL
        assert details.value is False


class TestErrorHandling:
    def test_error_reason_without_a_throw_is_reported(self) -> None:
        """The SDK fails safe rather than raising, so this is the common path."""
        client = FakeClient(
            EvaluationDetail(value=None, reason=EvaluationReason.ERROR)
        )
        details = make_provider(client).resolve_boolean_details("flag", True)
        assert details.value is True
        assert details.reason == Reason.ERROR.value
        assert details.error_code == ErrorCode.GENERAL

    def test_flag_not_found_is_reported(self) -> None:
        client = FakeClient(
            EvaluationDetail(value=None, reason=EvaluationReason.FLAG_NOT_FOUND)
        )
        details = make_provider(client).resolve_boolean_details("missing", True)
        assert details.value is True
        assert details.error_code == ErrorCode.FLAG_NOT_FOUND

    def test_an_unexpected_throw_degrades_to_the_default(self) -> None:
        client = FakeClient()
        client.raises = RuntimeError("boom")
        details = make_provider(client).resolve_boolean_details("flag", True)
        assert details.value is True
        assert details.error_code == ErrorCode.GENERAL
        assert details.error_message == "boom"

    def test_resolving_before_initialize_reports_provider_not_ready(self) -> None:
        provider = FeatureflipProvider(sdk_key="sdk-key")
        details = provider.resolve_boolean_details("flag", False)
        assert details.value is False
        assert details.error_code == ErrorCode.PROVIDER_NOT_READY


class TestLifecycle:
    def test_shutdown_closes_a_client_the_provider_created(self) -> None:
        client = FakeClient()
        provider = FeatureflipProvider(sdk_key="sdk-key")
        provider._client = client  # type: ignore[assignment]
        provider.initialize(EvaluationContext())
        provider.shutdown()
        assert client.closed is True

    def test_shutdown_does_not_close_a_caller_supplied_client(self) -> None:
        """It is a refcounted handle the caller still holds."""
        client = FakeClient()
        provider = make_provider(client)
        provider.shutdown()
        assert client.closed is False

    def test_shutdown_unsubscribes_even_from_a_caller_owned_client(self) -> None:
        client = FakeClient()
        provider = make_provider(client)
        assert len(client.listeners) == 1
        provider.shutdown()
        assert client.listeners == []

    def test_reinitializing_does_not_double_subscribe(self) -> None:
        client = FakeClient()
        provider = make_provider(client)
        provider.initialize(EvaluationContext())
        assert len(client.listeners) == 1


class TestEvents:
    def test_flag_update_emits_provider_configuration_changed(self) -> None:
        client = FakeClient()
        provider = make_provider(client)
        captured = capture_events(provider)

        client.emit_update(["flag-a", "flag-b"])

        assert len(captured) == 1
        event, details = captured[0]
        assert event == ProviderEvent.PROVIDER_CONFIGURATION_CHANGED
        assert details.flags_changed == ["flag-a", "flag-b"]

    def test_no_event_is_emitted_after_shutdown(self) -> None:
        client = FakeClient()
        provider = make_provider(client)
        captured = capture_events(provider)
        provider.shutdown()

        client.emit_update(["flag-a"])

        assert captured == []
