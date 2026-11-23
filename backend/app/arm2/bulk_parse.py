"""Bounded, formula-free table parser. Executed in a disposable timed subprocess."""
import base64
import csv
import io
import json
import sys
import zipfile

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS, MAX_COLS, MAX_CELL = 1000, 32, 1000
MANIFEST = "_ARM_B4_manifest"


def parse(data, filename, sheet=None):
    if len(data) > MAX_BYTES:
        raise ValueError("Файл больше 5 МиБ")
    manifest, chosen = [], None
    if filename.lower().endswith(".xlsx"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 200 or sum(m.file_size for m in members) > 20 * 1024 * 1024:
                raise ValueError("Превышены лимиты распакованного XLSX")
            for m in members:
                lower = m.filename.lower()
                if (m.flag_bits & 1 or m.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or "vbaproject" in lower or "externallinks/" in lower
                        or m.file_size > 10 * 1024 * 1024):
                    raise ValueError("Макросы, внешние ссылки и чрезмерный XLSX запрещены")
                # Prevent DTD/entity expansion before openpyxl sees the XML.
                if lower.endswith((".xml", ".rels")):
                    xml = archive.read(m)
                    if b"\x00" in xml or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                        raise ValueError("DTD и XML entities запрещены")
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            sheets = [n for n in book.sheetnames if n != MANIFEST]
            if sheet is None and len(sheets) != 1:
                return {"sheets": sheets}
            chosen = sheet or sheets[0]
            if chosen not in sheets:
                raise ValueError("Выбранный лист не найден")
            ws = book[chosen]
            ws.reset_dimensions()  # Never trust edited dimension metadata.
            rows = []
            for row in ws.iter_rows():
                if len(rows) > MAX_ROWS or len(row) > MAX_COLS:
                    raise ValueError("Не более 1000 строк данных и 32 столбцов")
                values = []
                for cell in row:
                    if cell.data_type in {"f", "e"}:
                        raise ValueError("Формулы и ошибки Excel запрещены; вставьте значения")
                    values.append("" if cell.value is None else str(cell.value))
                rows.append(values)
            if MANIFEST in book.sheetnames:
                for row in book[MANIFEST].iter_rows(values_only=True):
                    if len(manifest) > MAX_ROWS or len(row) != 1 or not isinstance(row[0], str) or len(row[0]) > 16000:
                        raise ValueError("Некорректный служебный лист")
                    manifest.append(row[0])
        finally:
            book.close()
    elif filename.lower().endswith((".csv", ".tsv")):
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("cp1251")
        if filename.lower().endswith(".tsv"):
            delimiter = "\t"
        else:
            delimiter = csv.Sniffer().sniff(content[:8192], delimiters=",;\t").delimiter if any(c in content.splitlines()[0] for c in ",;\t") else ","
        rows = []
        for row in csv.reader(io.StringIO(content), delimiter=delimiter):
            rows.append(row)
            if len(rows) > MAX_ROWS + 1 or len(row) > MAX_COLS:
                raise ValueError("Не более 1000 строк данных и 32 столбцов")
    else:
        raise ValueError("Допустимы XLSX, CSV, TSV и вставка из Excel")
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows or not any(rows[0]):
        raise ValueError("Таблица пуста; первая строка должна содержать заголовки")
    width = max(map(len, rows))
    for row in rows:
        row.extend([""] * (width - len(row)))
        for value in row:
            if len(value) > MAX_CELL or "\x00" in value or value.lstrip().startswith(("=", "+", "@")):
                raise ValueError("Ячейка содержит формулу, NUL или более 1000 символов")
    return {"headers": rows[0], "rows": rows[1:], "manifest": manifest, "source_sheet": chosen}


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.read(MAX_BYTES * 2))
        result = parse(base64.b64decode(request["data"], validate=True), request["filename"], request.get("sheet"))
        print(json.dumps(result, ensure_ascii=True))
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=True))
