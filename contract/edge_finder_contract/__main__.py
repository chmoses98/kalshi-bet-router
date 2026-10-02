"""``python -m edge_finder_contract validate <file.json>``: validate an app document by its kind."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .publish import verify_published
from .validate import SchemaError, validate_document


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "validate":
        failures = 0
        for name in argv[1:]:
            path = Path(name)
            if path.is_dir():
                problems = verify_published(path)
                print(f"{path}: {'OK' if not problems else problems}")
                failures += bool(problems)
                continue
            try:
                validate_document(json.loads(path.read_text(encoding="utf-8")))
                print(f"{path}: OK")
            except SchemaError as exc:
                failures += 1
                print(f"{path}: {exc.errors[:5]}")
        return 1 if failures else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
