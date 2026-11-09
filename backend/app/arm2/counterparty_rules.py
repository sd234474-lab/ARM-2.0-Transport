import re

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def required_name(value, field="name", limit=500):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} must not exceed {limit} characters")
    return value


def optional_text(value, field, limit=1000):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip() or None
    if value is not None and len(value) > limit:
        raise ValueError(f"{field} must not exceed {limit} characters")
    return value


def optional_digits(value, field, lengths):
    value = optional_text(value, field, max(lengths))
    if value is not None and (not value.isdigit() or len(value) not in lengths):
        expected = " or ".join(map(str, lengths))
        raise ValueError(f"{field} must contain exactly {expected} digits")
    return value


def normalize_party(values):
    normalized = {
        "name": required_name(values["name"]),
        "full_name": optional_text(values["full_name"], "full_name", 2000),
        "legal_address": optional_text(values["legal_address"], "legal_address", 2000),
        "postal_address": optional_text(values["postal_address"], "postal_address", 2000),
        "legal_form": optional_text(values["legal_form"], "legal_form", 500),
        "short_name": optional_text(values["short_name"], "short_name", 500),
        "inn": optional_digits(values["inn"], "inn", (10, 12)),
        "kpp": optional_digits(values["kpp"], "kpp", (9,)),
        "ogrn": optional_digits(values["ogrn"], "ogrn", (13, 15)),
        "phone": optional_text(values["phone"], "phone", 200),
        "email": optional_text(values["email"], "email", 320),
        "bank_name": optional_text(values["bank_name"], "bank_name", 500),
        "bik": optional_digits(values["bik"], "bik", (9,)),
        "settlement_account": optional_digits(values["settlement_account"], "settlement_account", (20,)),
        "correspondent_account": optional_digits(values["correspondent_account"], "correspondent_account", (20,)),
        "primary_signer_id": values["primary_signer_id"],
    }
    if normalized["email"] is not None and not _EMAIL.fullmatch(normalized["email"]):
        raise ValueError("email has invalid format")
    return normalized


def party_status(values):
    return "CONFIRMED" if values["full_name"] and values["legal_address"] else "REVIEW"
