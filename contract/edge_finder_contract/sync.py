"""Keep the vendored copies honest.

    python -m edge_finder_contract.sync --manifest            rewrite MANIFEST.json for this copy
    python -m edge_finder_contract.sync --check               verify this copy against its MANIFEST.json
    python -m edge_finder_contract.sync --check PATH          compare this copy with the copy at PATH
    python -m edge_finder_contract.sync --copy-to PATH        vendor this copy into PATH (a repo root)

The manifest lists every file of the package (excluding caches and the manifest itself) with its
sha256. Each repository's contract test calls :func:`check` so a drifted vendored copy fails CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
MANIFEST = PACKAGE_DIR / "MANIFEST.json"
IGNORE_DIRS = {"__pycache__"}
IGNORE_NAMES = {"MANIFEST.json"}


def _files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_dir() or any(part in IGNORE_DIRS for part in p.parts) or p.name in IGNORE_NAMES or p.suffix == ".pyc":
            continue
        out.append(p)
    return out


def digests(root: Path = PACKAGE_DIR) -> dict[str, str]:
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in _files(root)}


def write_manifest(root: Path = PACKAGE_DIR) -> dict:
    from . import CONTRACT_VERSION, SCHEMA_VERSION
    manifest = {"schema_version": SCHEMA_VERSION, "contract_version": CONTRACT_VERSION, "files": digests(root)}
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def check(root: Path = PACKAGE_DIR, other: Path | None = None) -> list[str]:
    """Problems between ``root`` and its manifest, or between ``root`` and ``other``."""
    mine = digests(root)
    if other is None:
        manifest_path = root / "MANIFEST.json"
        if not manifest_path.exists():
            return ["no MANIFEST.json"]
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    else:
        expected = digests(Path(other))
    problems = []
    for name in sorted(set(mine) | set(expected)):
        if name not in mine:
            problems.append(f"missing here: {name}")
        elif name not in expected:
            problems.append(f"not in the reference: {name}")
        elif mine[name] != expected[name]:
            problems.append(f"differs: {name}")
    return problems


def copy_to(repo_root: Path, root: Path = PACKAGE_DIR) -> Path:
    target = Path(repo_root) / "contract" / "edge_finder_contract"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(root, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", action="store_true")
    ap.add_argument("--check", nargs="?", const=True, default=None)
    ap.add_argument("--copy-to", default=None)
    a = ap.parse_args(argv)
    if a.manifest:
        m = write_manifest()
        print(f"manifest written: {len(m['files'])} files")
    if a.check is not None:
        other = None if a.check is True else Path(a.check) / "contract" / "edge_finder_contract"
        problems = check(other=other)
        for p in problems:
            print(p)
        print("contract copy OK" if not problems else f"{len(problems)} problem(s)")
        if problems:
            return 1
    if a.copy_to:
        print(copy_to(Path(a.copy_to)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
