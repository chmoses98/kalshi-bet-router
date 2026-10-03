"""Atomic publication of ``app/latest``.

The rule: ``latest`` is always a complete, internally consistent publication from ONE run, or
it is the previous such publication. A failed build never leaves a half-written tree, and never
destroys the last-known-good one. ``health.json`` sits beside the payload and is written on
every attempt, success or failure, so the UI can show "payload from 14:20, refresh at 15:05
FAILED" rather than either lying or going blank.

Mechanics
    1. every document is validated against its schema;
    2. the bundle passes :func:`integrity.check_bundle`;
    3. the manifest is built from the serialized bytes (sha256, byte count, item count);
    4. everything is written to a staging directory beside ``latest`` (same filesystem);
    5. each file is moved into ``latest`` with ``os.replace`` -- manifest LAST, so a reader
       that sees the new manifest sees new files;
    6. files from the previous publication that the new one does not name are removed
       (event detail files of events that left the board), AFTER the manifest. The ``explorer/``
       tree is never touched here: :func:`research.publish_explorer` owns it, publishes it after this
       step, and keeps its own last-known-good tree (its index names the v1 run it was built against).

Step 5 is per-file, so a reader racing a publish can see a new events.json with an old manifest
for a few milliseconds; the manifest's sha256 per file lets it detect that and re-read. In a git
commit both land together, which is how the app actually reads.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import SCHEMA_VERSION, ids
from .integrity import check_bundle, check_manifest
from .timeutil import now_utc, to_iso
from .validate import SchemaError, validate, validate_document

COLLECTION_KINDS = ("events", "markets", "model_prices", "recommendations", "theses", "wagers",
                    "settlements", "runs")
MANIFEST_NAME = "manifest.json"
HEALTH_NAME = "health.json"
EXPLORER_DIR = "explorer"  # owned by research.publish_explorer; never pruned here


def dumps(document: dict, *, compact: bool = True) -> str:
    if compact:
        return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PublishError(RuntimeError):
    """The bundle was not published. ``problems`` says why; ``latest`` is untouched."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:10]) + (" ..." if len(problems) > 10 else ""))
        self.problems = problems


def build_manifest(*, sport: str, run_id: str, generated_at: object, documents: dict[str, str],
                   parsed: dict[str, dict], source_repo: str, source_branch: str, commit_sha: object,
                   model_version: object, status: str, freshness: dict | None, warnings: list[str],
                   counts: dict | None = None) -> dict:
    files = {}
    for name, text in documents.items():
        doc = parsed[name]
        files[name] = {"path": f"{name}.json" if not name.startswith("event_detail/") else f"{name}.json",
                       "kind": doc["kind"], "sha256": sha256_text(text), "bytes": len(text.encode("utf-8")),
                       "count": len(doc["items"]) if "items" in doc else None}
    all_counts = {name: len(parsed[name]["items"]) for name in documents if "items" in parsed[name]}
    all_counts.update(counts or {})
    manifest = {
        "schema_version": SCHEMA_VERSION, "kind": "manifest", "sport": ids.normalize_sport(sport),
        "run_id": run_id, "generated_at": to_iso(generated_at),
        "commit_sha": None if commit_sha is None else str(commit_sha),
        "model_version": None if model_version is None else str(model_version),
        "status": status, "source_repo": source_repo, "source_branch": source_branch,
        "files": files, "counts": all_counts, "freshness": freshness or {}, "warnings": list(warnings),
    }
    validate(manifest, "manifest")
    return manifest


def validate_bundle(documents: dict[str, dict]) -> list[str]:
    """Schema-validate every document and check cross references. Returns problems."""
    problems: list[str] = []
    for name, doc in documents.items():
        try:
            validate_document(doc)
        except SchemaError as exc:
            problems.extend(f"{name}: {e}" for e in exc.errors[:20])
    bundle = {name: doc for name, doc in documents.items() if name in COLLECTION_KINDS}
    problems.extend(check_bundle(bundle))
    return problems


def publish(*, root: Path, sport: str, run_id: str, generated_at: object, documents: dict[str, dict],
            source_repo: str, source_branch: str, commit_sha: object = None, model_version: object = None,
            status: str = "SUCCESS", freshness: dict | None = None, warnings: list[str] | None = None,
            health: dict | None = None, compact: bool = True) -> dict:
    """Publish ``documents`` (kind -> document; event detail under ``event_detail/<event_id>``) into
    ``root`` atomically. Returns the manifest. Raises :class:`PublishError` without touching ``root``
    when anything is inconsistent."""
    root = Path(root)
    warnings = list(warnings or [])
    problems = validate_bundle(documents)
    for name, doc in documents.items():
        if doc.get("run_id") != run_id:
            problems.append(f"{name}: run_id {doc.get('run_id')} != {run_id}")
    if problems:
        raise PublishError(problems)
    texts = {name: dumps(doc, compact=compact) for name, doc in documents.items()}
    manifest = build_manifest(sport=sport, run_id=run_id, generated_at=generated_at, documents=texts,
                              parsed=documents, source_repo=source_repo, source_branch=source_branch,
                              commit_sha=commit_sha, model_version=model_version, status=status,
                              freshness=freshness, warnings=warnings)
    mproblems = check_manifest(manifest, documents, {n: sha256_text(t) for n, t in texts.items()})
    if mproblems:
        raise PublishError(mproblems)
    texts[MANIFEST_NAME[:-5]] = dumps(manifest, compact=False)
    if health is not None:
        validate(health, "health")
        texts[HEALTH_NAME[:-5]] = dumps(health, compact=False)

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(root.parent)))
    try:
        for name, text in texts.items():
            target = staging / f"{name}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        previous = ({str(p.relative_to(root)) for p in root.rglob("*.json") if p.relative_to(root).parts[0] != EXPLORER_DIR}
                    if root.exists() else set())
        ordered = [n for n in texts if n not in ("manifest", "health")] + [n for n in ("health", "manifest") if n in texts]
        for name in ordered:
            src = staging / f"{name}.json"
            dst = root / f"{name}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
        written = {f"{n}.json" for n in texts}
        for stale in sorted(previous - written):
            (root / stale).unlink(missing_ok=True)
        for sub in sorted((p for p in root.rglob("*") if p.is_dir() and p.relative_to(root).parts[0] != EXPLORER_DIR),
                          reverse=True):
            if not any(sub.iterdir()):
                sub.rmdir()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return manifest


def write_health_only(root: Path, health: dict) -> Path:
    """On a failed build: leave the payload alone, replace only health.json (atomically)."""
    validate(health, "health")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".health-", suffix=".json", dir=str(root))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(dumps(health, compact=False))
    target = root / HEALTH_NAME
    os.replace(tmp, target)
    return target


def read_manifest(root: Path) -> dict | None:
    path = Path(root) / MANIFEST_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def verify_published(root: Path) -> list[str]:
    """Re-validate a published tree: manifest, every named file, digests, cross references."""
    root = Path(root)
    manifest = read_manifest(root)
    if manifest is None:
        return ["no manifest.json"]
    problems: list[str] = []
    documents: dict[str, dict] = {}
    digests: dict[str, str] = {}
    for name, entry in manifest.get("files", {}).items():
        path = root / entry["path"]
        if not path.exists():
            problems.append(f"{name}: file {entry['path']} missing")
            continue
        text = path.read_text(encoding="utf-8")
        digests[name] = sha256_text(text)
        try:
            documents[name] = json.loads(text)
        except json.JSONDecodeError:
            problems.append(f"{name}: not JSON")
    problems.extend(validate_bundle(documents))
    problems.extend(check_manifest(manifest, documents, digests))
    health_path = root / HEALTH_NAME
    if health_path.exists():
        try:
            validate(json.loads(health_path.read_text(encoding="utf-8")), "health")
        except SchemaError as exc:
            problems.extend(f"health: {e}" for e in exc.errors[:5])
    else:
        problems.append("no health.json")
    return problems


def now_iso() -> str:
    return to_iso(now_utc())
