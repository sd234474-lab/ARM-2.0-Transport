"""Approved TYPE completeness, STS and explicit plate category rules."""


def normalize_type_name(name: str) -> str:
    """Trim a type name without applying future uniqueness or length rules."""
    if not isinstance(name, str):
        raise TypeError("Vehicle type name must be a string")
    normalized = name.strip()
    if not normalized:
        raise ValueError("Vehicle type name must not be empty")
    return normalized


def _tri_state(value: bool | None, field: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise TypeError(f"{field} must be True, False, or None")
    return value


def normalize_type_requirements(
    requires_sts: bool | None = True,
    requires_calibration: bool | None = None,
    requires_wialon: bool | None = None,
) -> tuple[bool | None, bool | None, bool | None]:
    """Commands require STS=True; calibration and Wialon remain tri-state."""
    sts = _tri_state(requires_sts, "requires_sts")
    calibration = _tri_state(requires_calibration, "requires_calibration")
    wialon = _tri_state(requires_wialon, "requires_wialon")
    if sts is not True:
        raise ValueError("Требуется СТС: выберите «Требуется». Пустое значение и «Не требуется» запрещены")
    return sts, calibration, wialon


def normalize_plate_category(value):
    if value not in {None, "Автомобильный", "Самоходная техника"}:
        raise ValueError("Неизвестный тип государственного номера")
    return value


def plate_compatible(plate, category):
    from app.arm2.vehicle_store import _CAR, _SELF_PROPELLED
    return bool((_CAR if category == "Автомобильный" else _SELF_PROPELLED).fullmatch(plate)) if category else False


def type_status(name, requires_sts, requires_calibration, requires_wialon, plate_category=None):
    normalize_type_name(name)
    normalize_plate_category(plate_category)
    for field, value in (("requires_sts", requires_sts), ("requires_calibration", requires_calibration),
                         ("requires_wialon", requires_wialon)):
        _tri_state(value, field)
    if requires_sts is False:
        raise ValueError("Требуется СТС: выберите «Требуется»")
    return "CONFIRMED" if (requires_sts is True and requires_calibration is not None
                           and requires_wialon is not None and plate_category is not None) else "REVIEW"
