"""Table I/O, permissions and authenticated original snapshots for the VEH pilot."""
import base64
import hashlib
import hmac
import io
import json
import subprocess
import sys
from pathlib import Path
from threading import BoundedSemaphore

from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.arm2.bulk_parse import MANIFEST
from app.arm2.read_routes import search_vehicles
from app.config import settings
from app.models import User
from app.permissions import Permission, has_permission

FIELDS = ("name", "plate_number", "model", "type_id", "owner", "accounting_state", "wialon_unit_id")
LABELS = {"id": "VEH-ID", "name": "Рабочее имя", "plate_number": "Госномер", "model": "Модель",
          "type_id": "TYPE — ID", "type_name": "TYPE — наименование", "owner_kind": "Владелец — категория",
          "owner_id": "Владелец — ID", "owner_name": "Владелец — наименование", "owner_cp_id": "Владелец CP — ID",
          "owner_org_id": "Владелец ORG — ID", "owner": "Операционный владелец", "accounting_state": "Учётное состояние",
          "wialon_unit_id": "Wialon ID", "version": "Техническая версия"}
_PARSERS = BoundedSemaphore(2)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def permissions(db, user, edit=False):
    user = db.get(User, user.id, populate_existing=True)
    if user is None or not user.active or user.deleted_at is not None:
        raise HTTPException(403, "Пользователь недоступен")
    for code in [Permission.ARM2_VEH_VIEW, *([Permission.ARM2_VEH_EDIT] if edit else [])]:
        if not has_permission(user, code):
            raise HTTPException(403, "Нет прав на технику")
    return user


def visible_columns(user):
    columns = ["id", "plate_number", "name", "model", "accounting_state", "wialon_unit_id", "version"]
    if has_permission(user, Permission.ARM2_TYPE_VIEW):
        columns += ["type_id", "type_name"]
    if all(has_permission(user, p) for p in (Permission.ARM2_CP_VIEW, Permission.ARM2_ORG_VIEW)):
        columns += ["owner_kind", "owner_id", "owner_name"]
    else:
        for kind in ("CP", "ORG"):
            if has_permission(user, Permission(f"arm2_{kind.lower()}_view")):
                columns += [f"owner_{kind.lower()}_id"]
    return columns


def xlsx(headers, rows, manifest=()):
    book = Workbook()
    ws = book.active
    ws.title = "Техника"
    for row in [headers, *rows]:
        ws.append(["" if v is None else str(v) for v in row])
        for cell in ws[ws.max_row]:
            cell.data_type, cell.number_format = "s", "@"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 24
    if manifest:
        service = book.create_sheet(MANIFEST)
        service.sheet_state = "veryHidden"
        service.protection.sheet = True
        for entry in manifest:
            service.append([entry])
            service.cell(service.max_row, 1).data_type = "s"
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def signing_key():
    key = settings().arm2_table_signing_key
    return key.encode() if len(key) >= 32 else None


def namespace(db):
    # Bind a manifest to this tenant database, without exposing connection data.
    return fingerprint(db.get_bind().url.render_as_string(hide_password=True))


def export_vehicles(db, user, filters):
    key = signing_key()
    if key is None:
        raise HTTPException(503, "Для выгрузки настройте серверный ARM2_TABLE_SIGNING_KEY (не менее 32 символов)")
    rows, manifests = [], []
    with Session(db.get_bind()) as snapshot:
        snapshot.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        user = permissions(snapshot, user)
        columns = visible_columns(user)
        scope = namespace(snapshot)
        for offset in range(0, 10_001, 100):
            page = search_vehicles(snapshot, user, limit=100, offset=offset, **filters)
            if page["matched"] > 10_000:
                raise HTTPException(413, "Выгрузка ограничена 10000 VEH; уточните фильтры")
            for item in page["items"]:
                item["owner_id"] = item.get("owner_cp_id") or item.get("owner_org_id")
                values = {c: item.get(c) for c in columns}
                rows.append([values[c] for c in columns])
                body = {"schema": 1, "scope": scope, "values": values}
                manifests.append(canonical({"body": body, "mac": hmac.new(key, canonical(body).encode(), hashlib.sha256).hexdigest()}))
            if offset + 100 >= page["matched"]:
                break
    return xlsx([LABELS[c] for c in columns], rows, manifests)


def originals(db, entries):
    if not entries:
        return {}, "Сторонняя таблица: исходного снимка нет. Принимайте только явно выбранные отличия"
    key, result = signing_key(), {}
    try:
        if key is None:
            raise ValueError()
        for entry in entries:
            signed = json.loads(entry)
            body = signed["body"]
            expected = hmac.new(key, canonical(body).encode(), hashlib.sha256).hexdigest()
            if (not hmac.compare_digest(expected, signed["mac"]) or body["schema"] != 1
                    or body["scope"] != namespace(db) or body["values"]["id"] in result):
                raise ValueError()
            result[body["values"]["id"]] = body["values"]
        return result, "Подлинный снимок ARM: сравниваются исходное, текущее и загруженное значения. Строки без снимка считаются сторонними"
    except (KeyError, ValueError, TypeError):
        return {}, "Подлинность снимка не подтверждена. Таблица считается сторонней; версиям файла не доверяем"


def parse_table(data, filename, sheet):
    if not _PARSERS.acquire(blocking=False):
        raise HTTPException(429, "Разбор занят; повторите позже")
    try:
        request = {"data": base64.b64encode(data).decode(), "filename": filename, "sheet": sheet}
        try:
            process = subprocess.run([sys.executable, str(Path(__file__).with_name("bulk_parse.py"))],
                                     input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, timeout=8, check=True)
            result = json.loads(process.stdout)
        except (subprocess.SubprocessError, ValueError):
            raise HTTPException(422, "Таблица не прочитана за 8 секунд либо повреждена") from None
        if "error" in result:
            raise HTTPException(422, result["error"])
        return result
    finally:
        _PARSERS.release()


def suggest(headers):
    names = {**{k.casefold(): k for k in LABELS}, **{v.casefold(): k for k, v in LABELS.items()},
             "государственный номер": "plate_number", "номер": "plate_number", "название": "name"}
    return [names.get(value.strip().casefold(), "") for value in headers]
