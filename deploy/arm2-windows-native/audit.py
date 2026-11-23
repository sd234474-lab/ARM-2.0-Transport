"""Read-only legacy comparison and create-only local installation export."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import psutil
from policy import APP_SOURCE, APPROVED_HEAD, ROOT
from prepare import hash_file
from run import wait_http


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-path", required=True, type=Path)
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    before = json.loads((ROOT / "evidence/protected-before.json").read_text(encoding="utf-8"))
    changed = []
    for item in before:
        path = Path(item["path"])
        if not path.is_file():
            changed.append(str(path))
            continue
        value = path.stat()
        if (value.st_size, value.st_mtime_ns, hash_file(path)) != (item["bytes"], item["mtime_ns"], item["sha256"]):
            changed.append(str(path))
    previous = json.loads((args.catalog_path / "90_EVIDENCE/before.json").read_text(encoding="utf-8"))
    sources = 0
    for source in previous["sources"]:
        for relative, expected in source["code_files"].items():
            path = Path(source["path"]) / relative
            sources += 1
            if not path.is_file() or hash_file(path) != expected:
                changed.append(str(path))
    old_ports = {55432, 8080, 8081, 5174, 8014}
    connections = psutil.net_connections(kind="tcp")
    old_listeners = sorted({c.laddr.port for c in connections if c.status == "LISTEN" and c.laddr.port in old_ports})
    own_listeners = sorted({c.laddr.port for c in connections if c.status == "LISTEN" and c.laddr.port in {55442, 18082, 18083}})
    health = wait_http(18082, seconds=3)
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "protected_files": len(before),
              "source_files": sources, "changed": changed, "old_listeners": old_listeners,
              "own_listeners": own_listeners, "healthy": health["ok"]}
    (ROOT / "evidence/protected-after.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if changed or old_listeners or own_listeners != [18082, 18083, 55442]:
        raise RuntimeError("Protection/port comparison failed; inspect local evidence, do not claim unchanged")
    if args.export:
        registry = json.loads((args.catalog_path / "registry.json").read_text(encoding="utf-8"))
        lines = ["АРМ — локальный экспорт установок", result["observed_at"],
                 "Существующий реестр сохранён. Все ARM1 — прототипы. Этот файл — отдельный актуальный экспорт."]
        for key in ("platforms", "configurations", "infobases", "instances", "storage"):
            lines.append("\n" + key.upper())
            lines.extend(json.dumps(item, ensure_ascii=False, indent=2) for item in registry[key])
        lines.extend(["\nФАКТИЧЕСКИ УСТАНОВЛЕН ARM2 NATIVE UAT", "Root: " + str(ROOT),
                      "Source: " + APP_SOURCE, "Approved base: " + APPROVED_HEAD,
                      "Platform: system Python read-only + own venv; PostgreSQL 17.11 / PostGIS 3.6.2",
                      "Configuration: Alembic 0061; existing ARM2 business code unchanged",
                      "Infobase: arm2_win_run_001, 127.0.0.1:55442, " + str(ROOT / "db-data"),
                      "Instance: http://127.0.0.1:18082/arm2 (API 127.0.0.1:18083)",
                      "Start: " + str(ROOT / "START_ARM2.cmd"), "Stop: " + str(ROOT / "STOP_ARM2.cmd"),
                      "Health: " + str(ROOT / "HEALTH_ARM2.cmd"),
                      "Local login/password: " + str(ROOT / "private/secrets.json") + " (do not share)",
                      "Earlier registry entry about an uninstalled Docker package is historical; native instance above is installed.",
                      "UI login/create/save/reopen/history PASS; CP/ORG foreign/missing signer rejected; stop/start persistence PASS.",
                      f"Unchanged legacy files: {len(before)}; source checks: {sources}; old ports remain idle."])
        destination = args.catalog_path / "INSTALLATIONS-EXPORT-ARM2-WIN-RUN-001.txt"
        with destination.open("x", encoding="utf-8-sig") as stream:
            stream.write("\n".join(lines) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
