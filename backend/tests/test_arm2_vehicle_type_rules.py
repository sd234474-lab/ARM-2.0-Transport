import pytest

from app.arm2.vehicle_type_rules import (
    normalize_type_name,
    normalize_type_requirements,
    type_status,
)


def test_fully_answered_type_is_confirmed_even_with_false_answers():
    assert normalize_type_requirements(True, False, False) == (True, False, False)
    assert type_status("Самосвал", True, False, False) == "CONFIRMED"


@pytest.mark.parametrize(
    ("requires_calibration", "requires_wialon"),
    [(None, True), (False, None)],
)
def test_missing_optional_tri_state_answer_requires_review(
    requires_calibration,
    requires_wialon,
):
    assert type_status(
        "Самосвал",
        True,
        requires_calibration,
        requires_wialon,
    ) == "REVIEW"


def test_type_name_is_trimmed_and_whitespace_only_is_rejected():
    assert normalize_type_name("  Самосвал  ") == "Самосвал"
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_type_name("   ")


def test_sts_defaults_to_true_and_false_is_rejected():
    assert normalize_type_requirements() == (True, None, None)
    assert type_status("Самосвал", None, True, True) == "REVIEW"
    with pytest.raises(ValueError, match="requires_sts must be True"):
        normalize_type_requirements(False, True, True)
    with pytest.raises(ValueError, match="requires_sts must be True"):
        type_status("Самосвал", False, True, True)


@pytest.mark.parametrize(
    ("requires_sts", "requires_calibration", "requires_wialon"),
    [(1, True, True), (True, 0, True), (True, False, "false")],
)
def test_non_boolean_requirement_values_are_rejected(
    requires_sts,
    requires_calibration,
    requires_wialon,
):
    with pytest.raises(TypeError, match="must be True, False, or None"):
        normalize_type_requirements(
            requires_sts,
            requires_calibration,
            requires_wialon,
        )


def test_status_depends_on_completeness_not_vehicle_category():
    assert type_status("Самосвал", True, True, True) == "CONFIRMED"
    assert type_status("Легковой автомобиль", True, True, True) == "CONFIRMED"
    assert type_status("Самосвал", True, None, True) == "REVIEW"
    assert type_status("Самосвал обновлённый", True, False, True) == "CONFIRMED"
