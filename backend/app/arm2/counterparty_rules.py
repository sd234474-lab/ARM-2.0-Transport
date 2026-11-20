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


def optional_email(value):
    value = optional_text(value, "email", 320)
    if value is not None and not _EMAIL.fullmatch(value):
        raise ValueError("email has invalid format")
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
        "email": optional_email(values["email"]),
        "bank_name": optional_text(values["bank_name"], "bank_name", 500),
        "bik": optional_digits(values["bik"], "bik", (9,)),
        "settlement_account": optional_digits(values["settlement_account"], "settlement_account", (20,)),
        "correspondent_account": optional_digits(values["correspondent_account"], "correspondent_account", (20,)),
        "primary_signer_id": values["primary_signer_id"],
    }
    return normalized


PERSON = "Физическое лицо"
LEGAL = "Юридическое лицо"
NAME_FIELDS = ("last_name", "first_name", "middle_name")


def normalize_counterparty(values):
    result = normalize_party(values)
    if values.get("type") not in (PERSON, LEGAL):
        raise ValueError("Выберите тип: Физическое лицо или Юридическое лицо")
    result["type"] = values["type"]
    if result["type"] == PERSON and result["legal_form"] not in (None, "ИП"):
        raise ValueError("ОПФ физического лица: ИП или пусто")
    result.update({f: optional_text(values[f], f, 200) for f in NAME_FIELDS})
    return result


def official_names(values):
    """Display projection only. Never store a second source of personal names."""
    if values.get("type") != PERSON:
        return values.get("full_name"), values.get("short_name")
    full = " ".join(values.get(f) or "" for f in NAME_FIELDS).strip()
    full = " ".join(full.split()) or None
    short = " ".join(filter(None, [values.get("last_name"),
        "".join((values.get(f) or "")[0:1] + "." for f in NAME_FIELDS[1:] if values.get(f))])) or None
    if values.get("legal_form") == "ИП":
        full = "Индивидуальный предприниматель" + (" " + full if full else "")
        short = "ИП" + (" " + short if short else "")
    return full, short


def inapplicable_fields(values):
    if values.get("type") == PERSON:
        return ("full_name", "short_name", "kpp", *(() if values.get("legal_form") == "ИП" else ("ogrn",)))
    return NAME_FIELDS


def transition_clears(current, values):
    """Candidates, never implicit writes: the caller decides set/clear/keep."""
    fields = set(inapplicable_fields(values))
    if current and current["type"] != values["type"]:
        fields.update(("ogrn", "legal_form"))
    return {f: current[f] for f in fields if current and current.get(f)}


def counterparty_response(values):
    result = dict(values)
    result["inapplicable_values"] = {f: values[f] for f in inapplicable_fields(values) if values.get(f)}
    result["full_name"], result["short_name"] = official_names(values)
    return result
