"""Render a LOCAL navigation catalogue. No application launcher is generated."""

import argparse
import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
UNKNOWN = "НЕ УСТАНОВЛЕНО"
SECTIONS = {
    "platforms": ("Платформы / исполняемые версии", ["id", "name", "path", "version", "runtime", "state"]),
    "configurations": ("Конфигурации / правила и схемы", ["id", "name", "platform", "format", "version", "compatibility"]),
    "infobases": ("Информационные базы", ["id", "name", "kind", "path", "schema", "configuration", "environment", "state"]),
    "instances": ("Экземпляры запуска", ["id", "name", "platform", "configuration", "infobases", "launcher", "url", "state"]),
    "storage": ("Релизы, копии и хранение", ["id", "path", "role", "size", "state", "evidence"]),
    "changes": ("План переносов — НЕ ВЫПОЛНЕН", ["id", "from", "to", "users", "preconditions", "verification", "rollback"]),
}
LABELS = {"id": "Код", "name": "Название", "path": "Физический путь", "version": "Версия",
          "runtime": "Среда выполнения", "state": "Статус", "platform": "Платформа", "format": "Формат",
          "compatibility": "Совместимость", "kind": "Тип", "schema": "Схема", "configuration": "Конфигурация",
          "environment": "Среда / назначение", "infobases": "Базы", "launcher": "Старый launcher",
          "url": "URL / порт", "role": "Назначение по фактам", "size": "Размер снимка", "evidence": "Основание",
          "from": "Откуда", "to": "Куда (предложение)", "users": "Что использует путь",
          "preconditions": "До согласования", "verification": "Как проверить", "rollback": "Как вернуть"}


def validate(registry):
    if registry.get("schema_version") != 1 or registry.get("all_prototypes") is not True:
        raise ValueError("Explicit prototype classification/schema required")
    ids = set()
    for section in SECTIONS:
        for row in registry.get(section, []):
            identifier = row.get("id", "")
            if not re.fullmatch(r"[a-z][a-z0-9-]*", identifier) or identifier in ids:
                raise ValueError("Invalid/duplicate catalogue ID")
            ids.add(identifier)
            if set(row) - set(SECTIONS[section][1]):
                raise ValueError("Unexpected fields: commands/secrets do not belong in registry")
    platforms = {row["id"] for row in registry["platforms"]}
    configurations = {row["id"] for row in registry["configurations"]}
    infobases = {row["id"] for row in registry["infobases"]}
    for row in registry["configurations"]:
        if row["platform"] not in platforms:
            raise ValueError("Unknown configuration platform")
    for row in registry["instances"]:
        if row["platform"] not in platforms or row["configuration"] not in configurations:
            raise ValueError("Unknown instance platform/configuration")
        if not isinstance(row["infobases"], list) or set(row["infobases"]) - infobases:
            raise ValueError("Unknown instance infobase")
    for entry in registry.get("navigation", []):
        if set(entry) != {"category", "id", "target"} or not re.fullmatch(r"[a-z][a-z0-9-]*", entry["id"]):
            raise ValueError("Invalid navigation entry")
        if entry["category"] not in {"01_PLATFORMS", "02_CONFIGURATIONS", "03_INFOBASES", "04_INSTANCES", "05_RELEASES_BACKUPS"}:
            raise ValueError("Invalid navigation category")
        target = PureWindowsPath(entry["target"])
        if not target.is_absolute() or target.drive.startswith("\\") or ".." in target.parts:
            raise ValueError("Only explicit local directory targets permitted")


def text(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value if value not in (None, "") else UNKNOWN)


def write_new(path, content):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def render(registry, facts, output):
    validate(registry)
    output = Path(output)
    if output.exists():
        raise FileExistsError("Refusing to overwrite a catalogue or installation")
    output.mkdir(parents=True)
    for name in ("catalog.css", "catalog.js"):
        shutil.copyfile(HERE / name, output / name)
    write_new(output / "registry.json", json.dumps(registry, ensure_ascii=False, indent=2))
    (output / "90_EVIDENCE").mkdir()
    write_new(output / "90_EVIDENCE" / "before.json", json.dumps(facts, ensure_ascii=False, indent=2))
    for category in ("01_PLATFORMS", "02_CONFIGURATIONS", "03_INFOBASES", "04_INSTANCES", "05_RELEASES_BACKUPS"):
        (output / category).mkdir()
        write_new(output / category / "README.txt", "Это только навигация. Ярлыки ведут в существующие папки.\nДанные и программы не перемещались. Файлы БД не запускать двойным щелчком.\n")
    navigation = []
    blocks = []
    for section, (title, columns) in SECTIONS.items():
        navigation.append(f'<a href="#{section}">{html.escape(title)}</a>')
        rows = []
        for row in registry.get(section, []):
            cells = []
            for column in columns:
                value = text(row.get(column))
                escaped = html.escape(value, quote=True)
                copy = f'<button type="button" class="copy" data-copy="{escaped}">Копировать путь</button>' if column in {"path", "launcher"} and PureWindowsPath(value).is_absolute() else ""
                cells.append(f'<td>{escaped}{copy}</td>')
            rows.append('<tr>' + ''.join(cells) + '</tr>')
        header = ''.join(f'<th scope="col">{LABELS[column]}</th>' for column in columns)
        blocks.append(f'<section id="{section}"><h2>{title}</h2><div class="table"><table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')
    summary = html.escape(registry.get("summary", UNKNOWN))
    observed_value = facts.get("observed_at", UNKNOWN)
    try:
        observed_value = datetime.fromisoformat(observed_value).astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m.%Y %H:%M:%S UTC+05:00")
    except ValueError:
        pass
    observed = html.escape(observed_value)
    document = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'"><title>АРМ — каталог прототипов</title><link rel="stylesheet" href="catalog.css"></head><body>
<aside><div class="brand">АРМ<span>Каталог прототипов</span></div><nav aria-label="Разделы">{''.join(navigation)}</nav><p>Только навигация.<br>Программы не запускаются.</p></aside><main><header><span class="tag">Все базы — прототипы · рабочих баз нет</span><h1>Что установлено на этом Windows</h1><p>{summary}</p><p class="muted">Снимок: {observed}. Версия платформы, схема базы и выбранный экземпляр — разные сведения.</p></header>
<div class="flow"><div>1 · Платформа<small>Что исполняется</small></div><div>2 · Конфигурация<small>Правила и схема</small></div><div>3 · База<small>Где живут данные</small></div><div>4 · Экземпляр<small>Как выбирают базу</small></div></div>
<div class="notice">Каталог не заменяет старый Центр запуска и не назначает базу по умолчанию. Здесь нет кнопок запуска/миграции/удаления. Для неоднозначных связей указано «НЕ УСТАНОВЛЕНО». Сначала найдите строку и проверьте пути; прежние ярлыки сохранены.</div>
<label class="search">Найти по названию, версии, коду или пути<input id="search" type="search" placeholder="Например: 22 миграции, TEST, p-launcher"></label><div id="copy-status" role="status" aria-live="polite"></div>
{''.join(blocks)}<footer>Навигационные ярлыки — в папках 01–05 рядом с этим файлом. Полный локальный реестр — registry.json; evidence — 90_EVIDENCE. Реальные БД, конфиги и старые программы сюда не копировались. Обновление каталога — отдельная новая версия, без перезаписи старого снимка.</footer></main><script src="catalog.js"></script></body></html>'''
    write_new(output / "index.html", document)
    write_new(output / "READ_ME_FIRST.txt", "АРМ — каталог прототипов\n\nОткройте index.html или новый ярлык ARM - Catalog.\nВсе базы — прототипы, рабочих нет (со слов владельца).\nКаталог ничего не запускает и не меняет. Старые ярлыки и пути сохранены.\nПапки 01–05 содержат только навигационные ссылки на старые папки.\nДанные остаются на исходных местах. План переносов — только предложение, не выполнен.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    render(json.loads(Path(args.registry).read_text(encoding="utf-8")),
           json.loads(Path(args.facts).read_text(encoding="utf-8")), args.output)
    print("Navigation catalogue staged; no installations changed")


if __name__ == "__main__":
    main()
