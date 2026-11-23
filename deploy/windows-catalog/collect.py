"""Read-only filesystem/schema inventory. No application imports or database writes.

Output is LOCAL evidence, never intended for commit. Subject rows, account rows,
configuration values and connection credentials are not collected.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DEPENDENCIES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
VERSION_TABLES = {"schema_migrations", "alembic_version", "schema_version", "applied_migrations"}


def fingerprint(path):
    path = Path(path)
    raw = path.stat(follow_symlinks=False)
    if path.is_symlink() or getattr(raw, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Reparse point not fingerprinted")
    value = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "bytes": value.st_size, "mtime_ns": value.st_mtime_ns, "sha256": digest.hexdigest()}


def sqlite_metadata(path):
    path = Path(path)
    result = {"path": str(path), "schema": "НЕ УСТАНОВЛЕНО"}
    if path.stat().st_size == 0 or path.name.endswith(".partial"):
        return result | {"state": "empty/incomplete; not opened"}
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        return result | {"state": "live journal/WAL present; immutable schema may lag, not queried"}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)) as connection:
            connection.execute("PRAGMA query_only=ON")
            result["user_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in sorted(tables & VERSION_TABLES):
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                version = next((key for key in ("version_num", "version", "migration_id", "name") if key in columns), None)
                if version:
                    rows = [str(row[0]) for row in connection.execute(f'SELECT "{version}" FROM "{table}" ORDER BY "{version}" LIMIT 2000')]
                    result["schema"] = {"table": table, "count": len(rows), "latest": rows[-1] if rows else None}
            if "launcher_databases" in tables:
                # Registry metadata only. No launcher_users/credential data.
                columns = {row[1] for row in connection.execute('PRAGMA table_info("launcher_databases")')}
                safe = [key for key in ("path", "kind", "status", "schema_version", "last_opened_at") if key in columns]
                names = ",".join('"' + key + '"' for key in safe)
                result["registered_databases"] = [dict(zip(safe, row, strict=True)) for row in connection.execute(f"SELECT {names} FROM launcher_databases")]
            result["state"] = "metadata read using mode=ro&immutable=1"
    except (sqlite3.Error, OSError):
        result["state"] = "not readable as SQLite metadata; not modified"
    return result


def source_version(root):
    root = Path(root)
    if not (root / ".git").exists():
        return {"path": str(root), "commit": "НЕ УСТАНОВЛЕНО"}
    environment = os.environ | {"GIT_OPTIONAL_LOCKS": "0"}
    prefix = ["git", "-c", "safe.directory=" + root.as_posix(), "-C", str(root)]

    def read(*args):
        result = subprocess.run(prefix + list(args), env=environment, capture_output=True, text=True, check=False, timeout=15)
        return result.stdout.strip() if result.returncode == 0 else "НЕ УСТАНОВЛЕНО"

    return {"path": str(root), "commit": read("rev-parse", "HEAD"), "commit_time": read("show", "-s", "--format=%cI"),
            "changed_tracked_files": read("diff", "--name-only", "HEAD"),
            "untracked_files": read("ls-files", "--others", "--exclude-standard")}


def source_fingerprint(root):
    root = Path(root)
    files = {}
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in DEPENDENCIES and name != "tests"
                          and not Path(current, name).is_symlink()]
        for name in names:
            path = Path(current, name)
            if path.suffix in {".py", ".sql"} and not path.is_symlink():
                files[path.relative_to(root).as_posix()] = fingerprint(path)["sha256"]
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"path": str(root), "sha256_manifest": digest, "code_files": files}


def scan(roots):
    folders, databases, protected, errors = [], [], [], []

    def walk(path, depth=0, dependency=False):
        total, count, newest = 0, 0, 0
        try:
            info = path.stat(follow_symlinks=False)
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                errors.append({"path": str(path), "reason": "reparse point not followed"})
                return 0, 0, 0
            with os.scandir(path) as entries:
                for entry in entries:
                    item = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        size, amount, modified = walk(item, depth + 1, dependency or entry.name in DEPENDENCIES)
                        total += size
                        count += amount
                        newest = max(newest, modified)
                    elif entry.is_file(follow_symlinks=False):
                        value = entry.stat(follow_symlinks=False)
                        total += value.st_size
                        count += 1
                        newest = max(newest, value.st_mtime_ns)
                        database = item.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or (".db." in item.name and item.name.endswith(".partial"))
                        if not dependency and database:
                            metadata = sqlite_metadata(item) if value.st_size < 512 * 1024 * 1024 else {"path": str(item), "state": "large DB; metadata not queried"}
                            databases.append(metadata | {"bytes": value.st_size, "mtime_ns": value.st_mtime_ns})
                        if not dependency and (database or item.suffix.lower() in {".cmd", ".bat", ".lnk"}) and value.st_size < 512 * 1024 * 1024:
                            protected.append(fingerprint(item))
        except OSError:
            errors.append({"path": str(path), "reason": "access/stat failed"})
        if depth <= 2:
            folders.append({"path": str(path), "bytes": total, "file_count": count, "newest_mtime_ns": newest,
                            "created_ns": info.st_ctime_ns if 'info' in locals() else None})
        return total, count, newest

    for root in roots:
        walk(Path(root))
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "roots": list(roots), "folders": folders,
            "databases": databases, "protected_files": protected, "errors": errors,
            "privacy": "local metadata only; no subject/account rows or config values"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    destination = Path(args.output)
    if destination.exists():
        raise SystemExit("Refusing to overwrite evidence")
    facts = scan(args.root)
    facts["sources"] = [source_version(root) | source_fingerprint(root) for root in args.source]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(facts, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"folders": len(facts["folders"]), "database_candidates": len(facts["databases"]),
                      "protected_files": len(facts["protected_files"]), "errors": len(facts["errors"])}))


if __name__ == "__main__":
    main()
