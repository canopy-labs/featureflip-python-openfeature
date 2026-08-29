"""Tests for the Featureflip <-> OpenFeature mapping rules."""

from __future__ import annotations

import pytest
from featureflip import EvaluationDetail, EvaluationReason
from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import Reason

from featureflip_openfeature.mapping import (
    PREREQUISITE_FAILED,
    matches_boolean,
    matches_float,
    matches_integer,
    matches_object,
    matches_string,
    to_featureflip_context,
    to_resolution_details,
)


class TestToFeatureflipContext:
    def test_none_becomes_an_empty_context(self) -> None:
        assert to_featureflip_context(None) == {}

    def test_targeting_key_becomes_user_id(self) -> None:
        context = EvaluationContext(targeting_key="user-42")
        assert to_featureflip_context(context) == {"user_id": "user-42"}

    def test_attributes_pass_through_unchanged(self) -> None:
        context = EvaluationContext(
            targeting_key="user-42", attributes={"plan": "pro", "country": "GB"}
        )
        assert to_featureflip_context(context) == {
            "user_id": "user-42",
            "plan": "pro",
            "country": "GB",
        }

    def test_explicit_user_id_wins_over_targeting_key(self) -> None:
        context = EvaluationContext(
            targeting_key="targeting", attributes={"user_id": "explicit"}
        )
        assert to_featureflip_context(context)["user_id"] == "explicit"

    def test_camel_case_alias_also_wins(self) -> None:
        """`userId` is the accepted alias, so it must suppress the mapping too."""
        context = EvaluationContext(
            targeting_key="targeting", attributes={"userId": "explicit"}
        )
        mapped = to_featureflip_context(context)
        assert mapped["userId"] == "explicit"
        assert "user_id" not in mapped

    def test_absent_targeting_key_adds_nothing(self) -> None:
        context = EvaluationContext(attributes={"plan": "pro"})
        assert to_featureflip_context(context) == {"plan": "pro"}

    def test_the_source_context_is_not_mutated(self) -> None:
        attributes = {"plan": "pro"}
        context = EvaluationContext(targeting_key="user-42", attributes=attributes)
        to_featureflip_context(context)
        assert attributes == {"plan": "pro"}


class TestToResolutionDetails:
    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (EvaluationReason.RULE_MATCH, Reason.TARGETING_MATCH.value),
            (EvaluationReason.FALLTHROUGH, Reason.DEFAULT.value),
            (EvaluationReason.FLAG_DISABLED, Reason.DISABLED.value),
            (EvaluationReason.PREREQUISITE_FAILED, PREREQUISITE_FAILED),
        ],
    )
    def test_reason_mapping(self, reason: EvaluationReason, expected: str) -> None:
        details = to_resolution_details(
            EvaluationDetail(value=True, reason=reason), default_value=False
        )
        assert details.reason == expected
        assert details.value is True
        assert details.error_code is None

    def test_flag_not_found_returns_the_caller_default(self) -> None:
        """The SDK's error detail can carry a wrong-typed off-variation value."""
        detail = EvaluationDetail(value="off-value", reason=EvaluationReason.FLAG_NOT_FOUND)
        details = to_resolution_details(detail, default_value=False)
        assert details.value is False
        assert details.reason == Reason.ERROR.value
        assert details.error_code == ErrorCode.FLAG_NOT_FOUND

    def test_error_reason_returns_the_caller_default(self) -> None:
        detail = EvaluationDetail(value="off-value", reason=EvaluationReason.ERROR)
        details = to_resolution_details(detail, default_value=False)
        assert details.value is False
        assert details.reason == Reason.ERROR.value
        assert details.error_code == ErrorCode.GENERAL

    def test_error_message_is_carried_when_the_sdk_supplies_one(self) -> None:
        detail = EvaluationDetail(
            value=None, reason=EvaluationReason.ERROR, error=RuntimeError("boom")
        )
        assert to_resolution_details(detail, default_value=False).error_message == "boom"

    def test_variation_key_becomes_the_variant(self) -> None:
        detail = EvaluationDetail(
            value=True, reason=EvaluationReason.FALLTHROUGH, variation_key="on"
        )
        assert to_resolution_details(detail, default_value=False).variant == "on"

    def test_rule_id_and_prerequisite_key_become_flag_metadata(self) -> None:
        detail = EvaluationDetail(
            value=True,
            reason=EvaluationReason.RULE_MATCH,
            rule_id="rule-1",
            prerequisite_key="base",
        )
        details = to_resolution_details(detail, default_value=False)
        assert details.flag_metadata == {
            "ruleId": "rule-1",
            "prerequisiteKey": "base",
        }

    def test_flag_metadata_is_empty_when_neither_is_present(self) -> None:
        detail = EvaluationDetail(value=True, reason=EvaluationReason.FALLTHROUGH)
        assert to_resolution_details(detail, default_value=False).flag_metadata == {}


class TestTypeGuards:
    @pytest.mark.parametrize(
        ("value", "expected"), [(True, True), (False, True), (1, False), ("x", False)]
    )
    def test_boolean(self, value: object, expected: bool) -> None:
        assert matches_boolean(value) is expected

    @pytest.mark.parametrize(
        ("value", "expected"), [("x", True), ("", True), (1, False), (True, False)]
    )
    def test_string(self, value: object, expected: bool) -> None:
        assert matches_string(value) is expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, True),
            (0, True),
            (-3, True),
            (1.0, True),  # JSON does not distinguish 1 from 1.0
            (100.0, True),
            (1.5, False),
            ("1", False),
            (None, False),
            # bool is a subclass of int in Python — the trap this guard exists for.
            (True, False),
            (False, False),
        ],
    )
    def test_integer(self, value: object, expected: bool) -> None:
        assert matches_integer(value) is expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1.5, True),
            (0.0, True),
            (1, True),  # an integral flag value is a perfectly good float
            ("1.5", False),
            (None, False),
            (True, False),
        ],
    )
    def test_float(self, value: object, expected: bool) -> None:
        assert matches_float(value) is expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"a": 1}, True),
            ({}, True),
            ([1, 2], True),
            ([], True),
            # A str is a Sequence in Python; a caller asking for an object wants
            # structure, not characters.
            ("string", False),
            (b"bytes", False),
            (1, False),
            (None, False),
            (True, False),
        ],
    )
    def test_object(self, value: object, expected: bool) -> None:
        assert matches_object(value) is expected
