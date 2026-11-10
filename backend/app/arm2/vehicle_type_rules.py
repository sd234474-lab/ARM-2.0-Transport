"""Pure rules for future ARM 2.0 vehicle types.

These functions only normalize a name and the three approved requirements, then
derive completeness. Name uniqueness, persistence, versioning, and permissions
belong to later database and service layers.
"""


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
    """Validate tri-state answers; False is forbidden only for the STS rule."""
    sts = _tri_state(requires_sts, "requires_sts")
    calibration = _tri_state(requires_calibration, "requires_calibration")
    wialon = _tri_state(requires_wialon, "requires_wialon")
    if sts is False:
        raise ValueError("requires_sts must be True when it is specified")
    return sts, calibration, wialon


def type_status(
    name: str,
    requires_sts: bool | None,
    requires_calibration: bool | None,
    requires_wialon: bool | None,
) -> str:
    """Return REVIEW or CONFIRMED from completeness, never from vehicle category."""
    normalize_type_name(name)
    requirements = normalize_type_requirements(
        requires_sts,
        requires_calibration,
        requires_wialon,
    )
    return "CONFIRMED" if all(value is not None for value in requirements) else "REVIEW"
