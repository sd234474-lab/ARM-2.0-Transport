"""Explicit paths written by the B3–B5 and approval workflows."""

from uuid import UUID


def record(value):
    if not isinstance(value, dict):
        raise ValueError("Ожидается структура реквизитов")
    return value


def items(value):
    if not isinstance(value, list):
        raise ValueError("Ожидается список записей")
    return value


def records(value, field):
    return (record(item) for item in items(record(value).get(field, [])))


def typed(result, kind, identifier, path):
    if identifier in (None, "", "new", "$CP"):
        return
    if not isinstance(identifier, str):
        raise ValueError(f"Некорректный ID в {path}")
    result.append((kind, identifier, path))


def typed_owner(result, value, path):
    if value is None:
        return
    if not isinstance(value, list) or len(value) != 2 or value[0] not in ("CP", "ORG"):
        raise ValueError(f"Некорректная ссылка владельца в {path}")
    typed(result, *value, path)


def bulk_selection(row):
    p = record(row.payload)
    result = []
    choices = record(p.get("decisions", {}))
    keys = set()
    for plan in records(p, "plans"):
        if not isinstance(plan.get("key"), str) or "fields" not in plan or plan["key"] in keys:
            raise ValueError("Неизвестная форма плана табличного обмена")
        keys.add(plan["key"])
        typed(result, "VEH", plan.get("vehicle_id"), "payload.plans.vehicle_id")
        fields = record(plan["fields"])
        for name, index in record(choices.get(plan["key"], {})).items():
            options = items(fields.get(name, []))
            if type(index) is not int or not 0 <= index < len(options):
                raise ValueError("Выбор относится к неизвестному варианту плана")
            option = record(options[index])
            if option.get("error") or option.get("no_change"):
                continue
            if name == "type_id":
                typed(result, "TYPE", option["value"], "payload.plans.fields.type_id")
            elif name == "owner":
                typed_owner(result, option["value"], "payload.plans.fields.owner")
    if choices.keys() - keys:
        raise ValueError("Выбор относится к неизвестному плану")
    return result


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
        typed(result, kind, document.get(field), path + "." + field)
    for file in records(document, "current_files"):
        typed(result, "FILE", file["id"], path + ".current_files.id")


def questionnaire_selection(row):
    choices, comparison, payload, staging = map(record, (row.choices, row.preview, row.payload, row.staging))
    if payload.get("schema") not in (None, "ARM_IMPORT_V2", "ARM_IMPORT_V3", "ARM_IMPORT_V4"):
        raise ValueError("Неизвестная версия анкеты")
    result = []
    vehicle_choices = record(choices.get("vehicles", {}))
    document_choices = record(choices.get("documents", {}))
    typed(result, "CP", choices.get("cp_target"), "choices.cp_target")
    for choice in record(choices.get("representatives", {})).values():
        typed(result, "CPREP", record(choice).get("target"), "choices.representatives.target")
    document_rows = set()
    for choice in document_choices.values():
        choice = record(choice)
        if not choice.get("accept"):
            continue
        parent = choice.get("parent_key")
        if isinstance(parent, str):
            document_rows.add(parent)
            if parent.startswith("VEH-"):
                typed(result, "VEH", parent, "choices.documents.parent_key")
        add_document(result, choice, "choices.documents")
    for document in records(comparison, "documents"):
        if document.get("selected"):
            document_rows.add(document.get("parent_key"))
            add_document(result, document, "preview.documents")
    for vehicle in records(comparison, "vehicles"):
        key = vehicle["key"]
        choice = record(vehicle_choices.get(key, {}))
        if not (choice.get("include") or key in document_rows):
            continue
        typed(result, "VEH", vehicle.get("id"), "preview.vehicles.id")
        fields = record(vehicle.get("fields", {}))
        owner = record(fields.get("owner", {}))
        for name in ("current", "proposed"):
            typed_owner(result, owner.get(name), "preview.vehicles.owner")
        type_choice = record(fields.get("type_id", {}))
        if choice.get("include") and type_choice.get("selected") and not type_choice.get("error"):
            typed(result, "TYPE", type_choice.get("proposed"), "preview.vehicles.type_id")
    for choice in vehicle_choices.values():
        choice = record(choice)
        if choice.get("include") and "type_id" in choice.get("fields", ()):
            typed(result, "TYPE", choice.get("type_id"), "choices.vehicles.type_id")
    for group in records(staging, "groups"):
        add_document(result, group, "staging.groups")
        for file_id in items(group.get("replacements", [])):
            typed(result, "FILE", file_id, "staging.groups.replacements")
        for file in records(group, "files"):
            if file["existing"]:
                typed(result, "FILE", file["id"], "staging.groups.files.id")
    return result


def snapshot_references(snapshots, path):
    result = []
    for raw in items(snapshots):
        snapshot = record(raw)
        for kind, key in (("VEH", "vehicle_id"), ("DOCUMENT", "document_id"), ("DOCUMENT-REV", "revision_id")):
            typed(result, kind, snapshot[key], path + "." + key)
        for identifier in items(snapshot["file_ids"]):
            typed(result, "FILE", identifier, path + ".file_ids")
    return result


def approval_snapshot(row):
    result = snapshot_references(row.vehicles_snapshot, "vehicles_snapshot")
    for identifier in items(row.response_file_ids):
        typed(result, "FILE", identifier, "response_file_ids")
    return result


def approval_attempt(row):
    plan = record(row.plan)
    if row.action == "letter":
        return snapshot_references(plan["snapshots"], "plan.snapshots")
    if row.action == "calibration":
        return (("VEH", row.target_id, "target_id"),)
    if row.action == "response":
        return (("APPROVAL", row.target_id, "target_id"),)
    if row.action == "response-stage":
        identifier = record(plan["file"])["id"]
        return (("APPROVAL", row.target_id, "target_id"), ("FILE", identifier, "plan.file.id"))
    raise ValueError("Неизвестная операция согласования")


def questionnaire_upload(row):
    result = []
    for preparations in record(row.prepared).values():
        for file in items(preparations):
            file = record(file)
            if file["existing"]:
                typed(result, "FILE", file["id"], "prepared.files.id")
    return result


def file_acceptance(row):
    result = []
    for file in records(row.plan, "files"):
        if file["existing"]:
            typed(result, "FILE", file["id"], "plan.files.id")
    correction = row.plan.get("correction")
    if correction is not None:
        typed(result, "FILE", record(correction)["file_id"], "plan.correction.file_id")
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
    payload = record(row.payload)
    UUID(row.resource_id)
    UUID(payload["command_id"])
    if type(payload["revision"]) is not int or payload["revision"] <= 0 or type(payload["without_documents"]) is not bool:
        raise ValueError("Неизвестная форма команды приёмки анкеты")
    return ()
