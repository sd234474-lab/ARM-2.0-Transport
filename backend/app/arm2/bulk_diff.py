"""VEH-specific reconciliation. Nothing in this module writes subject records."""
import difflib
import re

from sqlalchemy import select

from app.arm2.bulk_tables import FIELDS, permissions
from app.arm2.counterparty_models import Counterparty
from app.arm2.ids import is_canonical_id
from app.arm2.organization_models import Organization
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import normalize_plate_number
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_rules import plate_compatible
from app.permissions import Permission, has_permission

MODELS = {"TYPE": VehicleType, "CP": Counterparty, "ORG": Organization}
CLEARABLE = {"type_id", "owner", "wialon_unit_id", "model"}


def state(record):
    if record is None:
        return {"name": None, "plate_number": None, "model": None, "type_id": None, "owner": None,
                "accounting_state": None, "wialon_unit_id": None}
    return {**{f: getattr(record, f) for f in FIELDS if f != "owner"},
            "owner": ["CP", record.owner_cp_id] if record.owner_cp_id else
            ["ORG", record.owner_org_id] if record.owner_org_id else None}


def original_state(raw):
    result = {f: raw[f] for f in FIELDS if f != "owner" and f in raw}
    if "owner_kind" in raw:
        result["owner"] = [raw["owner_kind"], raw["owner_id"]] if raw.get("owner_id") else None
    elif "owner_cp_id" in raw and "owner_org_id" in raw:
        result["owner"] = ["CP", raw["owner_cp_id"]] if raw["owner_cp_id"] else ["ORG", raw["owner_org_id"]] if raw["owner_org_id"] else None
    return result


def resolve(db, user, kind, identifier, name, original):
    if not has_permission(user, Permission(f"arm2_{kind.lower()}_view")):
        raise ValueError("Нет прав просмотра указанного справочника")
    model = MODELS[kind]
    if identifier:
        if not is_canonical_id(kind, identifier):
            raise ValueError("Некорректный постоянный ID справочника")
        record = db.get(model, identifier)
        if record is None:
            raise ValueError("Ссылка не найдена; справочники по тексту не создаются")
        if name and record.name != name:
            unchanged_label = (kind == "TYPE" and original.get("type_id") == identifier and original.get("type_name") == name)
            unchanged_label |= (kind in {"CP", "ORG"} and original.get("owner_kind") == kind
                                and original.get("owner_id") == identifier and original.get("owner_name") == name)
            if not unchanged_label:
                raise ValueError("ID и наименование указывают на разные записи")
        return identifier
    found = db.scalars(select(model).where(model.name == name).limit(2)).all()
    if len(found) != 1:
        raise ValueError("Наименование не найдено либо неоднозначно; укажите постоянный ID")
    return found[0].id


def owner_value(db, user, raw, original):
    cp, org = raw.get("owner_cp_id"), raw.get("owner_org_id")
    if cp and org:
        raise ValueError("Можно назначить только одного владельца CP или ORG")
    if cp or org:
        if raw.get("owner_id") and raw["owner_id"] != (cp or org):
            raise ValueError("Противоречащие ID владельца")
        kind = "CP" if cp else "ORG"
        if raw.get("owner_kind") and raw["owner_kind"] != kind:
            raise ValueError("Противоречащая категория владельца")
        return [kind, resolve(db, user, kind, cp or org, raw.get("owner_name"), original)]
    kind, identifier, name = raw.get("owner_kind"), raw.get("owner_id"), raw.get("owner_name")
    if not any((kind, identifier, name)):
        return None
    if not kind and identifier:
        kind = identifier.split("-")[0]
    if kind in {"CP", "ORG"}:
        return [kind, resolve(db, user, kind, identifier, name, original)]
    if kind:
        raise ValueError("Категория владельца должна быть CP или ORG")
    matches = []
    for candidate in ("CP", "ORG"):
        if has_permission(user, Permission(f"arm2_{candidate.lower()}_view")):
            for record in db.scalars(select(MODELS[candidate]).where(MODELS[candidate].name == name).limit(2)):
                matches.append([candidate, record.id])
    if len(matches) != 1:
        raise ValueError("Владелец не найден либо неоднозначен; задайте категорию и ID")
    return matches[0]


def owner_access(user, before, after):
    for value in (before, after):
        if value:
            kind = value[0].lower()
            if not all(has_permission(user, Permission(f"arm2_{kind}_{op}")) for op in ("view", "edit")):
                raise ValueError("Для переназначения нужны права просмотра и редактирования прежнего и нового владельца")


def label_value(db, user, field, value):
    if value is None:
        return "Пусто"
    if field == "type_id":
        record = db.get(VehicleType, value) if has_permission(user, Permission.ARM2_TYPE_VIEW) else None
        return f"{value} · {record.name}" if record else "Нет доступа к типу"
    if field == "owner":
        record = db.get(MODELS[value[0]], value[1]) if has_permission(user, Permission(f"arm2_{value[0].lower()}_view")) else None
        return f"{value[0]} · {value[1]} · {record.name}" if record else "Нет доступа к владельцу"
    return str(value)


def readable(user, field, value):
    if field == "type_id":
        return has_permission(user, Permission.ARM2_TYPE_VIEW)
    return field != "owner" or not value or has_permission(user, Permission(f"arm2_{value[0].lower()}_view"))


def validate_value(db, user, field, value, current, proposed_plate, proposed_type):
    if field == "type_id" and not has_permission(user, Permission.ARM2_TYPE_VIEW):
        raise ValueError("Нет прав на тип техники")
    if value is None:
        if field not in CLEARABLE:
            raise ValueError("Очистка этого реквизита запрещена")
        if field == "type_id" and not has_permission(user, Permission.ARM2_TYPE_VIEW):
            raise ValueError("Нет прав на тип техники")
    elif field in {"name", "model", "wialon_unit_id"}:
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError("Требуется непустой текст не более 500 символов")
    elif field == "accounting_state" and value not in {"Действующая", "Архивная"}:
        raise ValueError("Состояние: Действующая либо Архивная")
    if field == "type_id" and value:
        record = db.get(VehicleType, value)
        if record is None or (value != current["type_id"] and record.status != "CONFIRMED"):
            raise ValueError("Назначить можно только существующий CONFIRMED TYPE")
        if record.plate_category is None or not plate_compatible(proposed_plate, record.plate_category):
            raise ValueError("Номер несовместим с категорией TYPE либо категория не заполнена")
    if field == "plate_number" and proposed_type:
        record = db.get(VehicleType, proposed_type)
        if record is None or not plate_compatible(value, record.plate_category):
            raise ValueError("Госномер несовместим с категорией TYPE")
    if field == "owner":
        owner_access(user, current["owner"], value)


def preview(db, user, payload):
    user = permissions(db, user, edit=True)
    groups, rejected = {}, []
    suggestions = {}
    mapping = payload["mapping"]
    for index, cells in enumerate(payload["rows"]):
        if index in payload.get("excluded", []):
            continue
        source = index + 2
        raw = {field: cells[c].strip() for c, field in enumerate(mapping) if field and cells[c].strip()}
        plate, identifier = raw.get("plate_number"), raw.get("id")
        try:
            if plate:
                plate = normalize_plate_number(re.sub(r"\s+", "", plate))
            if identifier and not is_canonical_id("VEH", identifier):
                raise ValueError("Некорректный VEH-ID")
            by_id = db.get(Vehicle, identifier) if identifier else None
            by_plate = db.scalar(select(Vehicle).where(Vehicle.plate_number == plate)) if plate else None
            if identifier and by_id is None:
                raise ValueError("VEH-ID не найден; создание с чужим ID запрещено")
            if by_id and by_plate and by_id.id != by_plate.id:
                raise ValueError("VEH-ID и госномер указывают на разные VEH; исключите строку либо исправьте сопоставление")
            current = by_id or by_plate
            if not current and not plate:
                raise ValueError("Для CREATE нужен валидный госномер")
        except ValueError as error:
            rejected.append({"key": f"error:{index}", "sources": [source], "action": "CONFLICT", "errors": [str(error)], "fields": {}})
            continue
        key = current.id if current else plate
        nearby = {}
        if not current:
            prefix = plate[:2]
            if prefix not in suggestions:
                suggestions[prefix] = dict(db.execute(select(Vehicle.plate_number, Vehicle.id)
                                           .where(Vehicle.plate_number.startswith(prefix))
                                           .order_by(Vehicle.plate_number).limit(200)).all())
            nearby = suggestions[prefix]
        group = groups.setdefault(key, {"key": key, "sources": [], "errors": [], "fields": {},
                                       "vehicle_id": current.id if current else None,
                                       "version": current.version if current else None,
                                       "matched": {"name": current.name, "plate_number": current.plate_number} if current else None,
                                       "similar": [{"plate": p, "id": nearby[p]} for p in difflib.get_close_matches(plate or "", nearby, n=3, cutoff=.8)] if not current else []})
        group["sources"].append(source)
        current_values = state(current)
        original_raw = payload.get("originals", {}).get(identifier, {})
        group["has_snapshot"] = group.get("has_snapshot", True) and bool(original_raw)
        before = original_state(original_raw)
        values = {f: raw[f] for f in ("name", "model", "accounting_state", "wialon_unit_id") if f in raw}
        if plate:
            values["plate_number"] = plate
        if not current and "name" not in values:
            values["name"] = plate
        errors = {}
        if raw.get("type_id") or raw.get("type_name"):
            try:
                values["type_id"] = resolve(db, user, "TYPE", raw.get("type_id"), raw.get("type_name"), original_raw)
            except ValueError as error:
                errors["type_id"] = str(error)
        try:
            owner = owner_value(db, user, raw, original_raw)
            if owner:
                values["owner"] = owner
        except ValueError as error:
            errors["owner"] = str(error)
        clears = payload.get("clears", {}).get(str(index), [])
        for field in clears:
            values[field] = None
            errors.pop(field, None)
        proposed_plate = values.get("plate_number") or current_values["plate_number"]
        proposed_type = values.get("type_id", current_values["type_id"])
        for field in set(values) | set(errors):
            value = values.get(field)
            if field not in errors and value == current_values[field]:
                group.setdefault("_unchanged", {}).setdefault(field, []).append(
                    {"value": value, "label": label_value(db, user, field, value), "current": value,
                     "current_label": label_value(db, user, field, value), "original": before.get(field) if readable(user, field, before.get(field)) else None,
                     "has_original": field in before and readable(user, field, before.get(field)), "error": None, "conflict": False, "clear": field in clears,
                     "no_change": True, "sources": [source]})
                continue
            if field not in errors and field not in clears and field in before and value == before[field]:
                continue
            error = errors.get(field)
            if not error:
                try:
                    validate_value(db, user, field, value, current_values, proposed_plate, proposed_type)
                except ValueError as failure:
                    error = str(failure)
            options = group["fields"].setdefault(field, [])
            conflict = field in before and before[field] != current_values[field] and value != current_values[field]
            option = next((o for o in options if o["value"] == value and o["error"] == error), None)
            if option:
                option["sources"].append(source)
                option["conflict"] |= conflict
            else:
                options.append({"value": value, "label": label_value(db, user, field, value) if not error else str(raw.get(field, "")),
                                "current": current_values[field] if readable(user, field, current_values[field]) else None, "current_label": label_value(db, user, field, current_values[field]),
                                "original": before.get(field) if readable(user, field, before.get(field)) else None, "has_original": field in before and readable(user, field, before.get(field)),
                                "error": error, "conflict": conflict, "clear": field in clears, "sources": [source]})
    result = []
    for group in groups.values():
        # A duplicate equal to current ARM remains an alternative when another
        # source proposes a different value; never hide that disagreement.
        for field, unchanged in group.pop("_unchanged", {}).items():
            options = group["fields"].get(field)
            if options:
                for item in unchanged:
                    same = next((o for o in options if o["value"] == item["value"] and not o["error"]), None)
                    if same:
                        same["sources"].extend(item["sources"])
                    else:
                        options.append(item)
        for options in group["fields"].values():
            if len(options) > 1:
                for option in options:
                    option["conflict"] = True
        problem = group["errors"] or any(o["error"] or o["conflict"] for opts in group["fields"].values() for o in opts)
        group["action"] = "CONFLICT" if problem else ("UPDATE" if group["vehicle_id"] else "CREATE") if group["fields"] else "SKIP"
        result.append(group)
    return result + rejected
