"""Mapping between Featureflip and OpenFeature shapes.

Kept separate from the provider so the context, reason and type rules can be
tested directly — they are where the cross-SDK contract actually lives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagResolutionDetails, Reason

if TYPE_CHECKING:
    from featureflip import EvaluationDetail
    from openfeature.evaluation_context import EvaluationContext

#: No standard OpenFeature reason models an unmet prerequisite. Reasons are open
#: strings, so surface it verbatim rather than mislabelling it as DEFAULT or
#: DISABLED — same choice the Node and .NET providers made.
PREREQUISITE_FAILED = "PREREQUISITE_FAILED"

#: Featureflip reason name -> OpenFeature reason. Keyed by the enum member NAME
#: rather than the member itself so the table stays readable and a reason this
#: provider does not know about falls through to UNKNOWN instead of raising.
_REASON_MAP: dict[str, str] = {
    "RULE_MATCH": Reason.TARGETING_MATCH.value,
    "FALLTHROUGH": Reason.DEFAULT.value,
    "FLAG_DISABLED": Reason.DISABLED.value,
    "PREREQUISITE_FAILED": PREREQUISITE_FAILED,
}

#: Reasons that mean "no usable value" — the caller's default is substituted and
#: the runtime type guard is skipped. See `to_resolution_details`.
_ERROR_REASONS: dict[str, ErrorCode] = {
    "FLAG_NOT_FOUND": ErrorCode.FLAG_NOT_FOUND,
    "ERROR": ErrorCode.GENERAL,
}


def to_featureflip_context(context: EvaluationContext | None) -> dict[str, Any]:
    """Map an OpenFeature evaluation context onto a Featureflip context dict.

    ``targeting_key`` becomes ``user_id`` (the canonical wire field, and what
    percentage rollouts bucket on) unless the caller already supplied
    ``user_id`` or its accepted alias ``userId`` — an explicit attribute wins.

    Args:
        context: The OpenFeature context, or None.

    Returns:
        A plain dict suitable for the Featureflip SDK's ``context`` parameter.
    """
    if context is None:
        return {}

    mapped: dict[str, Any] = dict(context.attributes or {})
    if (
        context.targeting_key is not None
        and mapped.get("user_id") is None
        and mapped.get("userId") is None
    ):
        mapped["user_id"] = context.targeting_key
    return mapped


def to_resolution_details(
    detail: EvaluationDetail,
    default_value: Any,
) -> FlagResolutionDetails[Any]:
    """Map a Featureflip evaluation detail onto OpenFeature resolution details.

    On an error reason (``FLAG_NOT_FOUND``/``ERROR``) this substitutes the
    caller's ``default_value`` rather than passing ``detail.value`` through: the
    SDK's error details can carry a flag's off-variation value (a prerequisite
    chain past the depth limit, or an errored prerequisite), which would be
    wrong-typed and is not what the OpenFeature error contract prescribes — the
    caller's default is.

    Args:
        detail: The Featureflip evaluation detail.
        default_value: The caller's default, used on error reasons.

    Returns:
        The equivalent OpenFeature resolution details.
    """
    reason_name = detail.reason.name

    error_code = _ERROR_REASONS.get(reason_name)
    if error_code is not None:
        return FlagResolutionDetails(
            value=default_value,
            reason=Reason.ERROR.value,
            error_code=error_code,
            error_message=str(detail.error) if detail.error is not None else None,
        )

    flag_metadata: dict[str, Any] = {}
    if detail.rule_id is not None:
        flag_metadata["ruleId"] = detail.rule_id
    if detail.prerequisite_key is not None:
        flag_metadata["prerequisiteKey"] = detail.prerequisite_key

    return FlagResolutionDetails(
        value=detail.value,
        reason=_REASON_MAP.get(reason_name, Reason.UNKNOWN.value),
        variant=detail.variation_key,
        flag_metadata=flag_metadata,
    )


def matches_boolean(value: Any) -> bool:
    """Whether ``value`` satisfies a boolean flag read."""
    return isinstance(value, bool)


def matches_string(value: Any) -> bool:
    """Whether ``value`` satisfies a string flag read."""
    return isinstance(value, str)


def matches_integer(value: Any) -> bool:
    """Whether ``value`` satisfies an integer flag read.

    Accepts whole-number floats (``1.0``, ``1e2``) as well as ints, mirroring
    the .NET provider — flag values arrive as JSON, which does not distinguish
    ``1`` from ``1.0``, so rejecting the latter would make an integer flag's
    readability depend on how the value happened to be serialized.

    ``bool`` is excluded explicitly. It is a subclass of ``int`` in Python, so
    a bare ``isinstance(value, int)`` would let a boolean flag satisfy
    ``get_integer_value`` and hand the caller ``1``.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def matches_float(value: Any) -> bool:
    """Whether ``value`` satisfies a float flag read.

    Any JSON number qualifies, including an integral one — a flag holding ``1``
    is a perfectly good float. ``bool`` is excluded for the subclass reason
    above.
    """
    return not isinstance(value, bool) and isinstance(value, (int, float))


def matches_object(value: Any) -> bool:
    """Whether ``value`` satisfies an object flag read.

    Objects and arrays only, matching the ``Sequence | Mapping`` default that
    OpenFeature's ``resolve_object_details`` is typed against. A string is a
    ``Sequence`` in Python and is deliberately rejected: a caller reaching for
    ``get_object_value`` wants structure, not characters.
    """
    if isinstance(value, (str, bytes)):
        return False
    return isinstance(value, (Mapping, Sequence))
