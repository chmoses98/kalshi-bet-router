#!/usr/bin/env python3
"""Pin / check / vendor ``contract/standalone/execution_evidence_ledger.py``.

    python scripts/vendor_execution_evidence.py --pin                 rewrite the sha256 pin after an intentional change
    python scripts/vendor_execution_evidence.py --check PATH_TO_REPO  verify a destination's vendored copy is identical
    python scripts/vendor_execution_evidence.py --copy-to PATH_TO_REPO  copy the module into scripts/accounting/

The destination copy is byte-identical to the router's; the router's test pins the digest, so a drifted copy is
detectable from either side.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "contract" / "standalone" / "execution_evidence_ledger.py"
PIN = SRC.with_suffix(".sha256")
DEST = Path("scripts") / "accounting" / "execution_evidence_ledger.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pin", action="store_true")
    g.add_argument("--check")
    g.add_argument("--copy-to")
    a = ap.parse_args()
    if a.pin:
        PIN.write_text(f"{digest(SRC)}  execution_evidence_ledger.py\n")
        print("pinned", digest(SRC)[:16])
        return 0
    if a.check:
        target = Path(a.check) / DEST
        if not target.exists():
            print("MISSING in destination", file=sys.stderr)
            return 1
        ok = digest(target) == digest(SRC)
        print("identical" if ok else "DRIFT")
        return 0 if ok else 1
    target = Path(a.copy_to) / DEST
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, target)
    print("copied", digest(target)[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
