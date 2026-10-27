"""End-to-end tests against a REAL FeatureflipClient and the real OpenFeature API.

`test_provider.py` drives a fake client, which is fast but cannot catch the
provider calling a method the SDK does not actually have, or with the wrong
signature. These wire a genuine client (HTTP mocked at the SDK's own boundary)
through OpenFeature's public API, so a drift between the two packages fails
here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from featureflip import Config
from featureflip.models import (
    FlagConfiguration,
    FlagType,
    Prerequisite,
    ServeConfig,
    ServeType,
    Variation,
)
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEvent
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import Reason

from featureflip_openfeature import FeatureflipProvider
from featureflip_openfeature.mapping import PREREQUISITE_FAILED


def flag(
    key: str,
    value: Any,
    *,
    enabled: bool = True,
    prerequisites: list[Prerequisite] | None = None,
) -> FlagConfiguration:
    return FlagConfiguration(
        key=key,
        version=1,
        type=FlagType.BOOLEAN if isinstance(value, bool) else FlagType.STRING,
        enabled=enabled,
        variations=[Variation(key="on", value=value), Variation(key="off", value=None)],
        rules=[],
        fallthrough=ServeConfig(type=ServeType.FIXED, variation="on"),
        off_variation="off",
        prerequisites=prerequisites or [],
    )


@pytest.fixture
def sdk_http():
    """Patch the SDK's HTTP client so a real core can be built with no network."""
    from featureflip._core import _reset_for_testing

    _reset_for_testing()
    with patch("featureflip._core.HttpClient") as mock_http:
        mock_http.return_value.get_flags.return_value = ([], [])
        yield mock_http
    _reset_for_testing()


def build_provider(sdk_http, flags: list[FlagConfiguration]) -> FeatureflipProvider:
    sdk_http.return_value.get_flags.return_value = (flags, [])
    provider = FeatureflipProvider(
        sdk_key="test-key", config=Config(streaming=False, send_events=False)
    )
    provider.initialize(EvaluationContext())
    return provider


class TestAgainstTheRealSdk:
    def test_each_resolution_records_one_evaluation_event(self, sdk_http) -> None:
        """A flag read only through the provider must still count (#4300).

        The provider resolves through `variation_detail`, which recorded no event,
        so every flag read this way looked never evaluated to analytics,
        staleness and the archive guard.
        """
        from unittest.mock import MagicMock

        provider = build_provider(sdk_http, [flag("enabled-flag", True)])
        try:
            assert provider._client is not None
            processor = MagicMock()
            provider._client._core._event_processor = processor
            provider.resolve_boolean_details(
                "enabled-flag", False, EvaluationContext(targeting_key="u1")
            )
            assert processor.queue_event.call_count == 1
            event = processor.queue_event.call_args[0][0]
            assert event["type"] == "Evaluation"
            assert event["flagKey"] == "enabled-flag"
            assert event["userId"] == "u1"
            assert event["variation"] == "on"
            assert event["reason"] == "fallthrough"
        finally:
            provider.shutdown()

    def test_initialize_builds_a_working_client(self, sdk_http) -> None:
        provider = build_provider(sdk_http, [flag("enabled-flag", True)])
        try:
            details = provider.resolve_boolean_details("enabled-flag", False)
            assert details.value is True
            assert details.reason == Reason.DEFAULT.value
            assert details.variant == "on"
        finally:
            provider.shutdown()

    def test_unknown_flag_reports_flag_not_found(self, sdk_http) -> None:
        provider = build_provider(sdk_http, [])
        try:
            details = provider.resolve_boolean_details("missing", True)
            assert details.value is True
            assert details.error_code == ErrorCode.FLAG_NOT_FOUND
        finally:
            provider.shutdown()

    def test_disabled_flag_reports_disabled(self, sdk_http) -> None:
        provider = build_provider(sdk_http, [flag("off-flag", True, enabled=False)])
        try:
            assert provider.resolve_boolean_details("off-flag", True).reason == (
                Reason.DISABLED.value
            )
        finally:
            provider.shutdown()

    def test_unmet_prerequisite_surfaces_the_custom_reason(self, sdk_http) -> None:
        provider = build_provider(
            sdk_http,
            [
                flag("base", True, enabled=False),
                flag("gated", True, prerequisites=[Prerequisite("base", "on")]),
            ],
        )
        try:
            details = provider.resolve_boolean_details("gated", False)
            assert details.reason == PREREQUISITE_FAILED
            assert details.flag_metadata.get("prerequisiteKey") == "base"
        finally:
            provider.shutdown()

    def test_type_mismatch_against_a_real_evaluation(self, sdk_http) -> None:
        provider = build_provider(sdk_http, [flag("string-flag", "hello")])
        try:
            details = provider.resolve_boolean_details("string-flag", False)
            assert details.value is False
            assert details.error_code == ErrorCode.TYPE_MISMATCH
        finally:
            provider.shutdown()

    def test_shutdown_closes_the_client_it_created(self, sdk_http) -> None:
        provider = build_provider(sdk_http, [flag("a", True)])
        client = provider._client
        provider.shutdown()
        assert client is not None
        assert client._closed is True

    def test_configuration_change_reaches_openfeature(self, sdk_http) -> None:
        """The whole chain: SDK store update -> SDK hook -> provider -> OpenFeature."""
        provider = build_provider(sdk_http, [flag("a", True)])
        captured: list[Any] = []
        provider.attach(lambda _p, event, details: captured.append((event, details)))
        try:
            assert provider._client is not None
            provider._client._core._on_polling_update([flag("a", False)])

            assert len(captured) == 1
            event, details = captured[0]
            assert event == ProviderEvent.PROVIDER_CONFIGURATION_CHANGED
            assert details.flags_changed == ["a"]
        finally:
            provider.shutdown()


def register(sdk_http, flags: list[FlagConfiguration]) -> None:
    """Register through the API exactly as the README tells callers to.

    `set_provider_and_wait`, NOT `set_provider`: the plain form runs the
    provider's `initialize()` on a daemon thread and returns immediately, so a
    test using it races the flag load and passes or fails on timing. These
    tests previously pre-initialized the provider by hand and then called
    `set_provider`, which is why the docs could recommend the racy call for a
    whole release without any test noticing.
    """
    sdk_http.return_value.get_flags.return_value = (flags, [])
    api.set_provider_and_wait(
        FeatureflipProvider(
            sdk_key="test-key", config=Config(streaming=False, send_events=False)
        )
    )


class TestThroughTheOpenFeatureApi:
    """Drives the documented registration path end to end."""

    def test_evaluation_through_the_public_client(self, sdk_http) -> None:
        """Proves the provider satisfies the abstract surface OpenFeature calls."""
        register(sdk_http, [flag("checkout", True)])
        try:
            client = api.get_client()

            assert client.get_boolean_value("checkout", False) is True

            details = client.get_boolean_details(
                "checkout", False, EvaluationContext(targeting_key="user-42")
            )
            assert details.value is True
            assert details.reason == Reason.DEFAULT.value
            assert details.variant == "on"
        finally:
            api.clear_providers()

    def test_flags_are_loaded_by_the_time_set_provider_and_wait_returns(
        self, sdk_http
    ) -> None:
        """The regression guard for the racy quickstart shipped in 0.1.0.

        Asserts on `error_code`, not just the value. Whether the *value* is
        right under the plain `set_provider` is a coin flip — against these
        fast mocks the daemon init thread usually wins, so a value-only
        assertion passes most runs and would be a flaky guard rather than a
        real one. An uninitialized provider deterministically reports
        `PROVIDER_NOT_READY`, so that is what pins the contract.

        No sleep and no retry: the statement immediately after registration
        must already see a fully initialized provider.
        """
        register(sdk_http, [flag("checkout", True)])
        try:
            details = api.get_client().get_boolean_details("checkout", False)
            assert details.error_code is None
            assert details.value is True
        finally:
            api.clear_providers()

    def test_string_and_object_flags_through_the_public_client(self, sdk_http) -> None:
        register(sdk_http, [flag("theme", "dark")])
        try:
            assert api.get_client().get_string_value("theme", "light") == "dark"
        finally:
            api.clear_providers()

    def test_type_mismatch_through_the_public_client(self, sdk_http) -> None:
        """`bool` subclasses `int`, so this must NOT hand back 1."""
        register(sdk_http, [flag("checkout", True)])
        try:
            details = api.get_client().get_integer_details("checkout", 0)
            assert details.value == 0
            assert details.error_code == ErrorCode.TYPE_MISMATCH
        finally:
            api.clear_providers()

    def test_default_is_returned_for_a_missing_flag(self, sdk_http) -> None:
        register(sdk_http, [])
        try:
            assert api.get_client().get_string_value("nope", "fallback") == "fallback"
        finally:
            api.clear_providers()
