"""Explicit paths written by the B3–B5 and approval workflows."""

import re
from uuid import UUID

from app.arm2.bulk_diff import CLEARABLE
from app.arm2.bulk_tables import FIELDS, LABELS
from app.arm2.object_registry import OBJECT_REGISTRY

BULK_SCHEMA = "ARM_BULK_V1"
BULK_TABLE = {"headers", "rows", "mapping", "excluded", "clears", "decisions", "confirmations"}
BULK_PAYLOAD = BULK_TABLE | {"schema", "originals", "warning", "plans", "preview_id", "failures",
                             "report_revision", "report_fingerprint"}
BULK_PLAN = {"key", "sources", "errors", "fields", "action"}
BULK_MATCH = {"vehicle_id", "version", "matched", "similar", "has_snapshot"}
BULK_OPTION = {"value", "label", "current", "current_label", "original", "has_original", "error",
               "conflict", "clear", "sources"}
FILE_METADATA = {"id", "original_upload_name", "stored_name", "storage_ref", "size", "sha256", "media_type", "extension"}
DOCUMENT_CHOICE = {"upload_id", "parent_key", "kind_id", "document_id", "replace_file_id", "append", "accept"}
DOCUMENT_PREVIEW = DOCUMENT_CHOICE - {"accept"} | {
    "key", "name", "role", "parent_type", "parent_id", "version", "kind_version", "revision_id", "selected",
    "equal", "error", "sha256", "media_type", "attachment_options", "candidates", "current_files",
}
VEHICLE_CHOICE = {"fields", "type_id", "without_type", "confirm_owner", "include", "edits", "volume"}
VEHICLE_PREVIEW_FIELDS = {"name", "model", "type_id", "accounting_state", "owner"}


def record(value):
    if not isinstance(value, dict):
        raise ValueError("Ожидается структура реквизитов")
    return value


def shape(value, path, allowed, *, required=()):
    row = record(value)
    if set(row) - allowed or set(required) - row.keys():
        raise ValueError(f"Неизвестная форма {path}")
    return row


def items(value):
    if not isinstance(value, list):
        raise ValueError("Ожидается список записей")
    return value


def records(value, field):
    return (record(item) for item in items(record(value).get(field, [])))


def reference_id(kind, identifier, path):
    if not isinstance(identifier, str):
        raise ValueError(f"Некорректный ID в {path}")
    if kind in ("FILE", "DOCUMENT-REV"):
        if not re.fullmatch(f"{kind}-[0-9]{{6,}}", identifier):
            raise ValueError(f"Некорректный ID в {path}")
    else:
        OBJECT_REGISTRY.require(kind).validate_id(identifier)


def typed(result, kind, identifier, path, *, placeholders=()):
    if identifier is None or identifier in placeholders:
        return
    reference_id(kind, identifier, path)
    result.append((kind, identifier, path))


def typed_owner(result, value, path, *, new_cp=False):
    if value is None:
        return
    if not isinstance(value, list) or len(value) != 2 or value[0] not in ("CP", "ORG"):
        raise ValueError(f"Некорректная ссылка владельца в {path}")
    if new_cp and value == ["CP", "$CP"]:
        return
    reference_id(*value, path)
    result.append((*value, path))


def all_items(value, check, path):
    if not all(check(item) for item in items(value)):
        raise ValueError(f"Неизвестный формат {path}")


def string(value):
    return isinstance(value, str)


def nullable_string(value):
    return value is None or string(value)


def boolean(value):
    return type(value) is bool


def source_lines(value, row_count):
    all_items(value, lambda item: type(item) is int and 2 <= item <= row_count + 1, "plans.sources")
    if not value:
        raise ValueError("Не указаны исходные строки плана")


def bulk_option(name, value, row_count):
    option = shape(value, "plans.fields", BULK_OPTION | {"no_change"}, required=BULK_OPTION)
    if (not all(boolean(option[key]) for key in ("has_original", "conflict", "clear"))
            or ("no_change" in option and not boolean(option["no_change"]))
            or not all(string(option[key]) for key in ("label", "current_label"))
            or not nullable_string(option["error"])):
        raise ValueError("Неизвестный формат варианта реквизита")
    source_lines(option["sources"], row_count)
    references = []
    for key in ("value", "current", "original"):
        field = option[key]
        path = f"payload.plans.fields.{name}"
        found = []
        if name == "type_id":
            typed(found, "TYPE", field, path)
        elif name == "owner":
            typed_owner(found, field, path)
        elif not nullable_string(field):
            raise ValueError(f"Неизвестный формат {path}")
        if key == "value" and not option["error"] and not option.get("no_change"):
            references = found
    return references


def bulk_plan(raw, row_count):
    plan = record(raw)
    expanded = bool(plan.keys() & BULK_MATCH)
    keys = BULK_PLAN | BULK_MATCH if expanded else BULK_PLAN
    shape(plan, "plans", keys, required=keys)
    action, key = plan["action"], plan["key"]
    if not string(key) or not key or action not in ("CREATE", "UPDATE", "SKIP", "CONFLICT"):
        raise ValueError("Неизвестная форма плана табличного обмена")
    source_lines(plan["sources"], row_count)
    all_items(plan["errors"], string, "plans.errors")
    refs = []
    if expanded:
        identifier, version, matched = (plan[name] for name in ("vehicle_id", "version", "matched"))
        typed(refs, "VEH", identifier, "payload.plans.vehicle_id")
        if identifier is None:
            if version is not None or matched is not None or action == "UPDATE":
                raise ValueError("Некорректная цель плана создания")
        else:
            shape(matched, "plans.matched", {"name", "plate_number"}, required={"name", "plate_number"})
            if type(version) is not int or version < 1 or action == "CREATE" or not all(map(string, matched.values())):
                raise ValueError("Некорректная цель плана обновления")
        items(plan["similar"])  # Candidate descriptions are never references.
        if not boolean(plan["has_snapshot"]):
            raise ValueError("Некорректный признак исходного снимка")
    elif action != "CONFLICT" or plan["fields"] or not plan["errors"]:
        raise ValueError("Сокращённый план допустим только для отклонённой строки")
    fields = shape(plan["fields"], "plans.fields", set(FIELDS))
    if any(not options for options in fields.values()):
        raise ValueError("Отсутствуют варианты реквизита")
    return key, refs, {name: [bulk_option(name, option, row_count) for option in items(options)]
                       for name, options in fields.items()}


def bulk_references(payload):
    p = shape(payload, "payload", BULK_PAYLOAD, required=BULK_TABLE | {"plans", "preview_id"})
    if "schema" in p and p["schema"] != BULK_SCHEMA:
        raise ValueError("Неизвестная версия плана табличного обмена")
    rows = items(p["rows"])
    all_items(p["headers"], string, "headers")
    all_items(p["mapping"], lambda value: string(value) and (not value or value in LABELS and value != "owner"), "mapping")
    if len(p["mapping"]) != len(p["headers"]):
        raise ValueError("Не совпадает ширина сопоставления и заголовков")
    for row in rows:
        all_items(row, string, "rows")
        if len(row) != len(p["headers"]):
            raise ValueError("Не совпадает ширина строки и заголовков")
    all_items(p["excluded"], lambda i: type(i) is int and 0 <= i < len(rows), "excluded")
    for key, cleared in record(p["clears"]).items():
        if not string(key) or not key.isascii() or not key.isdigit() or int(key) >= len(rows):
            raise ValueError("Некорректная строка очистки")
        all_items(cleared, lambda value: string(value) and value in CLEARABLE, "clears")
    record(p.get("originals", {}))  # Signed baseline and source cells have no live references.
    if not nullable_string(p.get("warning")):
        raise ValueError("Некорректное предупреждение загрузки")
    uuid_text(p["preview_id"])
    if "report_revision" in p and (type(p["report_revision"]) is not int or p["report_revision"] < 1):
        raise ValueError("Некорректная версия отчёта")
    if "report_fingerprint" in p and not re.fullmatch(r"[0-9a-f]{64}", p["report_fingerprint"]):
        raise ValueError("Некорректный отпечаток отчёта")
    for command, failure in record(p.get("failures", {})).items():
        uuid_text(command)
        shape(failure, "failures", {"row_key", "status", "message"}, required={"row_key", "status", "message"})
        if not string(failure["row_key"]) or not string(failure["message"]) or failure["status"] not in (403, 409, 422):
            raise ValueError("Неизвестный результат попытки")
    result = []
    plans = {}
    for raw in records(p, "plans"):
        key, refs, fields = bulk_plan(raw, len(rows))
        if key in plans:
            raise ValueError("Ключ плана повторяется")
        plans[key] = fields
        result.extend(refs)
    for key, choices in record(p["decisions"]).items():
        fields = plans[key]
        for name, index in record(choices).items():
            options = fields[name]
            if type(index) is not int or not 0 <= index < len(options):
                raise ValueError("Выбор относится к неизвестному варианту плана")
            result.extend(options[index])
    for key, flags in record(p["confirmations"]).items():
        flags = shape(flags, "confirmations", {"owner", "plate_number"})
        if key not in plans or not all(map(boolean, flags.values())):
            raise ValueError("Подтверждение относится к неизвестному плану")
    return result


def uuid_text(value):
    if not string(value):
        raise ValueError("Некорректный идентификатор операции")
    UUID(value)


def bulk_selection(row):
    return bulk_references(row.payload)


def add_document(result, document, path):
    parent = document.get("parent_id")
    if parent is not None:
        kind = document.get("parent_type")
        if kind not in ("CP", "ORG", "VEH"):
            raise ValueError("Неизвестный родитель документа")
        typed(result, kind, parent, path + ".parent_id")
    for kind, field in (
        ("DOC-KIND", "kind_id"), ("DOCUMENT", "document_id"), ("DOCUMENT-REV", "revision_id"),
        ("FILE", "replace_file_id"),
    ):
        placeholders = ("", "new") if field == "document_id" else ("",)
        typed(result, kind, document.get(field), path + "." + field,
              placeholders=placeholders if path == "choices.documents" else ())
    for file in records(document, "current_files"):
        shape(file, path + ".current_files", {"id", "name", "sha256"}, required={"id"})
        typed(result, "FILE", file["id"], path + ".current_files.id")


def selected_flag(row, key):
    value = row.get(key, False)
    if not boolean(value):
        raise ValueError(f"Некорректный признак {key}")
    return value


def document_parent(result, key, path):
    if key in (None, "", "CP"):
        return
    if string(key) and key.startswith("VEH-"):
        typed(result, "VEH", key, path)
    elif not string(key) or not re.fullmatch(r"v:[0-9]+", key):
        raise ValueError(f"Некорректная привязка в {path}")


def planned_file(value, path, extra, *, required=()):
    file = shape(value, path, FILE_METADATA | extra, required=FILE_METADATA.union(required))
    reference_id("FILE", file["id"], path)
    if (type(file["size"]) is not int or file["size"] < 1
            or not all(string(file[field]) for field in FILE_METADATA - {"size"})
            or not re.fullmatch(r"[0-9a-f]{64}", file["sha256"])):
        raise ValueError(f"Некорректные реквизиты файла в {path}")
    if "existing" in extra and not boolean(file["existing"]):
        raise ValueError(f"Неизвестно, опубликован ли файл в {path}")
    return file


def questionnaire_selection(row):
    choices, comparison, payload, staging = map(record, (row.choices, row.preview, row.payload, row.staging))
    if "schema" in payload and payload["schema"] not in ("ARM_IMPORT_V2", "ARM_IMPORT_V3", "ARM_IMPORT_V4"):
        raise ValueError("Неизвестная версия анкеты")
    shape(choices, "choices", {"cp_target", "cp_fields", "cp_clears", "cp_values", "cp_edits", "confirm_identity",
                               "representatives", "vehicles", "documents"})
    shape(comparison, "preview", {"cp", "representatives", "vehicles", "documents", "document_kind_notice",
                                  "document_kinds", "types", "warnings", "filled_date", "uploaded_at"})
    shape(staging, "staging", {"command_id", "fingerprint", "groups"},
          required={"command_id", "fingerprint", "groups"} if staging else ())
    if staging:
        uuid_text(staging["command_id"])
    result = []
    vehicle_choices = record(choices.get("vehicles", {}))
    document_choices = record(choices.get("documents", {}))
    typed(result, "CP", choices.get("cp_target"), "choices.cp_target", placeholders=("new", ""))
    for choice in record(choices.get("representatives", {})).values():
        shape(choice, "choices.representatives", {"target", "fields", "signer", "sources", "edits"})
        typed(result, "CPREP", choice.get("target"), "choices.representatives.target", placeholders=("new", ""))
    document_rows = set()
    for choice in document_choices.values():
        shape(choice, "choices.documents", DOCUMENT_CHOICE)
        parent = choice.get("parent_key")
        selected = selected_flag(choice, "accept")
        refs = []
        document_parent(refs, parent, "choices.documents.parent_key")
        add_document(refs, choice, "choices.documents")
        if selected:
            document_rows.add(parent)
            result.extend(refs)
    for document in records(comparison, "documents"):
        shape(document, "preview.documents", DOCUMENT_PREVIEW, required={"key", "parent_key", "selected"})
        refs = []
        document_parent(refs, document["parent_key"], "preview.documents.parent_key")
        add_document(refs, document, "preview.documents")
        if selected_flag(document, "selected"):
            document_rows.add(document.get("parent_key"))
            result.extend(refs)
    for vehicle in records(comparison, "vehicles"):
        shape(vehicle, "preview.vehicles", {"key", "row", "id", "plate_number", "version", "fields", "errors",
                                           "withdrawal", "confirm_owner", "without_type", "source", "type_version",
                                           "volume_proposal", "volume_notice", "legacy_hint", "diagnostic_hint",
                                           "calibration"}, required={"key", "id", "fields"})
        key = vehicle["key"]
        choice = record(vehicle_choices.get(key, {}))
        refs = []
        typed(refs, "VEH", vehicle["id"], "preview.vehicles.id")
        fields = shape(vehicle["fields"], "preview.vehicles.fields", VEHICLE_PREVIEW_FIELDS)
        comparison_field = {"current", "proposed", "selected", "error", "equal", "current_display", "proposed_display"}
        owner = shape(fields.get("owner", {}), "preview.vehicles.owner", comparison_field)
        for name in ("current", "proposed"):
            typed_owner(refs, owner.get(name), "preview.vehicles.owner", new_cp=True)
        type_choice = shape(fields.get("type_id", {}), "preview.vehicles.type_id", comparison_field)
        type_refs = []
        typed(type_refs, "TYPE", type_choice.get("proposed"), "preview.vehicles.type_id")
        included = selected_flag(choice, "include")
        if included or key in document_rows:
            result.extend(refs)
        if included and selected_flag(type_choice, "selected") and not type_choice.get("error"):
            result.extend(type_refs)
    for choice in vehicle_choices.values():
        shape(choice, "choices.vehicles", VEHICLE_CHOICE)
        refs = []
        typed(refs, "TYPE", choice.get("type_id"), "choices.vehicles.type_id", placeholders=("",))
        if selected_flag(choice, "include") and "type_id" in items(choice.get("fields", [])):
            result.extend(refs)
    for group in records(staging, "groups"):
        group_shape = {"parent_type", "parent_id", "parent_key", "kind_id", "document_id", "document_name",
                       "expected_version", "replacements", "files"}
        shape(group, "staging.groups", group_shape, required=group_shape)
        document_parent(result, group["parent_key"], "staging.groups.parent_key")
        add_document(result, group, "staging.groups")
        for file_id in items(group.get("replacements", [])):
            typed(result, "FILE", file_id, "staging.groups.replacements")
        for file in records(group, "files"):
            planned_file(file, "staging.groups.files", {"existing", "upload_id"}, required={"existing"})
            if file["existing"]:
                typed(result, "FILE", file["id"], "staging.groups.files.id")
    return result


def snapshot_references(snapshots, path):
    result = []
    for raw in items(snapshots):
        keys = {"vehicle_id", "name", "plate_number", "volume_m3", "vehicle_version", "document_id",
                "document_version", "revision_id", "file_ids"}
        snapshot = shape(raw, path, keys, required=keys)
        for kind, key in (("VEH", "vehicle_id"), ("DOCUMENT", "document_id"), ("DOCUMENT-REV", "revision_id")):
            reference_id(kind, snapshot[key], path + "." + key)
            result.append((kind, snapshot[key], path + "." + key))
        for identifier in items(snapshot["file_ids"]):
            reference_id("FILE", identifier, path + ".file_ids")
            result.append(("FILE", identifier, path + ".file_ids"))
    return result


def approval_snapshot(row):
    result = snapshot_references(row.vehicles_snapshot, "vehicles_snapshot")
    for identifier in items(row.response_file_ids):
        typed(result, "FILE", identifier, "response_file_ids")
    return result


def approval_attempt(row):
    plan = record(row.plan)
    if row.action == "letter":
        keys = {"snapshots", "files", "created_at"}
        shape(plan, "plan.letter", keys, required=keys)
        for file in records(plan, "files"):
            planned_file(file, "plan.letter.files", {"token", "generated_bundle"}, required={"token"})
        return snapshot_references(plan["snapshots"], "plan.snapshots")
    if row.action == "calibration":
        shape(plan, "plan.calibration", set())
        return (("VEH", row.target_id, "target_id"),)
    if row.action == "response":
        shape(plan, "plan.response", set())
        return (("APPROVAL", row.target_id, "target_id"),)
    if row.action == "response-stage":
        keys = {"file", "session_hash"}
        shape(plan, "plan.response-stage", keys, required=keys)
        identifier = planned_file(plan["file"], "plan.response-stage.file", {"token", "generated_bundle"},
                                  required={"token"})["id"]
        return (("APPROVAL", row.target_id, "target_id"), ("FILE", identifier, "plan.file.id"))
    raise ValueError("Неизвестная операция согласования")


def questionnaire_upload(row):
    result = []
    for command, preparations in record(row.prepared).items():
        uuid_text(command)
        for file in items(preparations):
            file = planned_file(file, "prepared.files", {"existing", "upload_id"}, required={"existing"})
            if file["existing"]:
                typed(result, "FILE", file["id"], "prepared.files.id")
    return result


def file_acceptance(row):
    result = []
    keys = {"revision_id", "number", "noop", "files", "uploaded", "correction"}
    plan = shape(row.plan, "plan.acceptance", keys, required=keys - {"correction"})
    if plan["revision_id"] is not None:
        reference_id("DOCUMENT-REV", plan["revision_id"], "plan.revision_id")
    for file in records(plan, "files"):
        planned_file(file, "plan.files", {"existing", "stage_id"}, required={"existing", "stage_id"})
        if file["existing"]:
            typed(result, "FILE", file["id"], "plan.files.id")
    correction = plan.get("correction")
    if correction is not None:
        shape(correction, "plan.correction", {"file_id", "stage_id"}, required={"file_id"})
        typed(result, "FILE", correction["file_id"], "plan.correction.file_id")
    for identifier in items(plan["uploaded"]):
        reference_id("FILE", identifier, "plan.uploaded")
    return result


def questionnaire_outcome(row):
    result = []
    stored = record(row.result)
    for section in ("objects", "changes"):
        for entry in records(stored, section):
            typed(result, entry["kind"], entry.get("id") or entry.get("document_id"), f"result.{section}")
    typed(result, "CP", stored.get("cp_id"), "result.cp_id")
    return result


def questionnaire_pending_targets(row):
    if not record(row.result).get("without_documents"):
        return ()
    return [(kind, identifier, "receipt.partial_acceptance") for kind, identifier, _ in questionnaire_outcome(row)]


def approval_outcome(row):
    result = record(row.result)
    if row.action == "calibration":
        return (("VEH", result["vehicle_id"], "result.vehicle_id"),)
    if row.action in ("letter", "response"):
        return (("APPROVAL", result["request_id"], "result.request_id"),)
    if row.action == "response-stage":
        return (("APPROVAL", row.target_id, "target_id"),)
    raise ValueError("Неизвестный результат согласования")


def questionnaire_job(row):
    # The command only addresses its draft. Its domain references live in the draft policies.
    keys = {"command_id", "revision", "preview_id", "without_documents", "session_hash"}
    payload = shape(row.payload, "payload.apply", keys, required=keys)
    uuid_text(row.resource_id)
    uuid_text(payload["command_id"])
    if type(payload["revision"]) is not int or payload["revision"] <= 0 or type(payload["without_documents"]) is not bool:
        raise ValueError("Неизвестная форма команды приёмки анкеты")
    return ()
