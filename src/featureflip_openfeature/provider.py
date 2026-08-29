"""OpenFeature provider backed by the Featureflip Python server SDK."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from featureflip import Config, FeatureflipClient
from openfeature.event import ProviderEventDetails
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagResolutionDetails, Reason
from openfeature.provider import AbstractProvider, Metadata

from featureflip_openfeature.mapping import (
    matches_boolean,
    matches_float,
    matches_integer,
    matches_object,
    matches_string,
    to_featureflip_context,
    to_resolution_details,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from openfeature.evaluation_context import EvaluationContext
    from openfeature.flag_evaluation import FlagValueType
    from openfeature.track import TrackingEventDetails

logger = logging.getLogger(__name__)

#: Reported as the provider name via `get_metadata()`.
PROVIDER_NAME = "featureflip-python"


class FeatureflipProvider(AbstractProvider):
    """OpenFeature provider backed by the Featureflip Python server SDK.

    Construct it either with an SDK key (the provider creates and owns the
    client) or with an existing :class:`~featureflip.FeatureflipClient` (the
    caller keeps ownership).

    The SDK key form defers client construction to :meth:`initialize`, because
    ``FeatureflipClient.__init__`` blocks on the initial flag fetch. Building it
    eagerly in ``__init__`` would move that wait out of
    ``OpenFeature.set_provider_and_wait()``, where callers expect it, and into
    the constructor.

    Example:
        >>> from openfeature import api
        >>> # set_provider_and_wait, not set_provider: the plain form runs
        >>> # initialize() on a background thread and returns before flags load.
        >>> api.set_provider_and_wait(FeatureflipProvider(sdk_key="sdk-xxx"))
        >>> client = api.get_client()
        >>> client.get_boolean_value("new-checkout", False)
        False
    """

    def __init__(
        self,
        sdk_key: str | None = None,
        config: Config | None = None,
        *,
        client: FeatureflipClient | None = None,
    ) -> None:
        """Initialize the provider.

        Args:
            sdk_key: Featureflip server SDK key. The provider constructs the
                client in :meth:`initialize` and closes it in :meth:`shutdown`.
            config: Optional client configuration, used with ``sdk_key``.
            client: An existing client to use instead. The caller retains
                ownership — :meth:`shutdown` will not close it.

        Raises:
            ValueError: If both or neither of ``sdk_key`` and ``client`` are
                given.
        """
        super().__init__()
        if (sdk_key is None) == (client is None):
            raise ValueError("Pass exactly one of sdk_key or client.")

        self._sdk_key = sdk_key
        self._config = config
        # Set now only on the injected-client path; the sdk_key path fills it in
        # during initialize().
        self._client: FeatureflipClient | None = client
        self._owns_client = client is None

        # Guards the subscription so a concurrent initialize/shutdown cannot
        # double-subscribe or leave a listener attached after teardown.
        self._subscription_lock = threading.Lock()
        self._unsubscribe: Callable[[], None] | None = None

    def get_metadata(self) -> Metadata:
        return Metadata(name=PROVIDER_NAME)

    def initialize(self, evaluation_context: EvaluationContext) -> None:  # noqa: ARG002
        """Construct the client if we own it, then subscribe to flag updates.

        The evaluation context is unused: Featureflip is a dynamic-context
        (server) provider, so targeting comes from the per-evaluation context
        rather than being bound at initialization.
        """
        if self._client is None:
            # Blocks on the initial flag fetch, bounded by Config.init_timeout.
            self._client = FeatureflipClient(
                sdk_key=self._sdk_key, config=self._config or Config()
            )

        # Subscribed only after the client is ready, so the initial flag load is
        # never reported as a configuration change — OpenFeature signals that
        # with PROVIDER_READY. Guarded because OpenFeature may re-initialize an
        # already-initialized provider.
        with self._subscription_lock:
            if self._unsubscribe is None:
                self._unsubscribe = self._client.on_update(self._on_flags_changed)

    def shutdown(self) -> None:
        """Unsubscribe, and close the client only if this provider created it."""
        # Unsubscribed regardless of ownership: the listener emits on this
        # provider's event channel, so leaving it attached to a caller-owned
        # client would keep publishing after OpenFeature has torn us down.
        with self._subscription_lock:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None

        # A caller-supplied client is a refcounted handle the caller still
        # holds; closing it would make THEIR handle start returning defaults.
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _on_flags_changed(self, flag_keys: list[str]) -> None:
        """Republish an SDK flag-update as an OpenFeature event.

        Runs on the SDK's streaming/polling thread, so it must not raise: an
        exception here would be swallowed by the SDK but is better not produced.
        """
        try:
            self.emit_provider_configuration_changed(
                ProviderEventDetails(flags_changed=list(flag_keys))
            )
        except Exception:
            logger.warning(
                "Failed to emit PROVIDER_CONFIGURATION_CHANGED", exc_info=True
            )

    # --- Resolution ---

    def resolve_boolean_details(
        self,
        flag_key: str,
        default_value: bool,
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[bool]:
        return self._resolve(
            flag_key, default_value, evaluation_context, matches_boolean, "boolean"
        )

    def resolve_string_details(
        self,
        flag_key: str,
        default_value: str,
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[str]:
        return self._resolve(
            flag_key, default_value, evaluation_context, matches_string, "string"
        )

    def resolve_integer_details(
        self,
        flag_key: str,
        default_value: int,
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[int]:
        details = self._resolve(
            flag_key, default_value, evaluation_context, matches_integer, "integer"
        )
        # A whole-number float passes the guard (JSON does not distinguish 1
        # from 1.0), so narrow it to the int the caller asked for.
        if isinstance(details.value, float):
            details.value = int(details.value)
        return details

    def resolve_float_details(
        self,
        flag_key: str,
        default_value: float,
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[float]:
        details = self._resolve(
            flag_key, default_value, evaluation_context, matches_float, "float"
        )
        if isinstance(details.value, int) and not isinstance(details.value, bool):
            details.value = float(details.value)
        return details

    def resolve_object_details(
        self,
        flag_key: str,
        default_value: Sequence[FlagValueType] | Mapping[str, FlagValueType],
        evaluation_context: EvaluationContext | None = None,
    ) -> FlagResolutionDetails[Sequence[FlagValueType] | Mapping[str, FlagValueType]]:
        return self._resolve(
            flag_key, default_value, evaluation_context, matches_object, "object"
        )

    def track(
        self,
        tracking_event_name: str,
        evaluation_context: EvaluationContext | None = None,
        tracking_event_details: TrackingEventDetails | None = None,
    ) -> None:
        """Record a custom analytics event against the Featureflip client."""
        if self._client is None:
            return

        metadata: dict[str, Any] | None = None
        if tracking_event_details is not None:
            metadata = dict(tracking_event_details.attributes)
            if tracking_event_details.value is not None:
                metadata["value"] = tracking_event_details.value

        self._client.track(
            tracking_event_name,
            to_featureflip_context(evaluation_context),
            metadata,
        )

    def _resolve(
        self,
        flag_key: str,
        default_value: Any,
        evaluation_context: EvaluationContext | None,
        matches_type: Callable[[Any], bool],
        expected: str,
    ) -> FlagResolutionDetails[Any]:
        """Evaluate one flag and map the result, enforcing the requested type."""
        if self._client is None:
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.ERROR.value,
                error_code=ErrorCode.PROVIDER_NOT_READY,
                error_message="Featureflip provider has not been initialized.",
            )

        try:
            detail = self._client.variation_detail(
                flag_key, to_featureflip_context(evaluation_context), default_value
            )
        except Exception as err:  # pragma: no cover - the SDK does not raise here
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.ERROR.value,
                error_code=ErrorCode.GENERAL,
                error_message=str(err),
            )

        # On an error reason, to_resolution_details substitutes the caller's
        # (correctly-typed) default below, so skip the type guard here: the SDK's
        # error details can carry a wrong-typed off-variation value, and guarding
        # it would mislabel a genuine error as TYPE_MISMATCH. Only successful
        # resolutions need the runtime check.
        if detail.reason.name not in ("FLAG_NOT_FOUND", "ERROR") and not matches_type(
            detail.value
        ):
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.ERROR.value,
                error_code=ErrorCode.TYPE_MISMATCH,
                error_message=(
                    f"Flag '{flag_key}' did not resolve to a {expected} value"
                ),
            )

        return to_resolution_details(detail, default_value)
