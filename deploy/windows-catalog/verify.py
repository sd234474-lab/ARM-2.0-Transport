"""Compare previously recorded legacy files without changing any of them."""

import argparse
import json
from pathlib import Path

from collect import fingerprint


def compare(items):
    result = {"unchanged": [], "changed": [], "missing_or_unreadable": []}
    for original in items:
        try:
            current = fingerprint(original["path"])
            category = "unchanged" if current == original else "changed"
            result[category].append(original["path"])
        except (OSError, ValueError):
            result["missing_or_unreadable"].append(original["path"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    result = compare(before["protected_files"])
    with Path(args.output).open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: len(value) for key, value in result.items()}))
    if result["changed"] or result["missing_or_unreadable"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
