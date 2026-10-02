"""A dependency-free validator for the JSON Schema subset the contract uses.

Supported keywords: ``type`` (string or list, including ``"null"``), ``properties``, ``required``,
``additionalProperties`` (boolean or schema), ``items``, ``enum``, ``const``, ``pattern``,
``format`` (``date-time`` = canonical UTC, ``date``), ``minimum`` / ``maximum`` /
``exclusiveMinimum`` / ``exclusiveMaximum``, ``minLength`` / ``maxLength``, ``minItems``,
``anyOf`` / ``oneOf`` / ``allOf``, ``$ref`` (local ``#/$defs/<name>`` only), ``$defs``.

Anything else in a schema is ignored, and the test suite pins that every keyword the committed
schemas use is one of these, so a schema cannot silently stop validating.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .timeutil import DATE_RE, ISO_UTC_RE

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

SUPPORTED_KEYWORDS = frozenset({
    "$schema", "$id", "$defs", "$ref", "title", "description", "type", "properties", "required",
    "additionalProperties", "items", "enum", "const", "pattern", "format", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "anyOf", "oneOf",
    "allOf", "examples", "default", "deprecated",
})

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    """The document does not conform. ``errors`` lists every problem with a JSON path."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:12]) + (" ..." if len(errors) > 12 else ""))
        self.errors = errors


@lru_cache(maxsize=None)
def load_schema(kind: str) -> dict:
    path = SCHEMA_DIR / f"{kind}.schema.json"
    if not path.exists():
        raise KeyError(f"no schema for kind {kind!r} (looked for {path.name})")
    return json.loads(path.read_text(encoding="utf-8"))


def schema_kinds() -> list[str]:
    return sorted(p.name[: -len(".schema.json")] for p in SCHEMA_DIR.glob("*.schema.json"))


def _resolve_ref(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"only local refs are supported, not {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _check_format(fmt: str, value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        return
    if fmt == "date-time" and not ISO_UTC_RE.match(value):
        errors.append(f"{path}: not a canonical UTC timestamp (YYYY-MM-DDTHH:MM:SS[.f]Z): {value!r}")
    elif fmt == "date" and not DATE_RE.match(value):
        errors.append(f"{path}: not a YYYY-MM-DD date: {value!r}")


def _validate(value: Any, schema: dict, root: dict, path: str, errors: list[str]) -> None:
    if "$ref" in schema:
        _validate(value, _resolve_ref(schema["$ref"], root), root, path, errors)
        return
    if "allOf" in schema:
        for sub in schema["allOf"]:
            _validate(value, sub, root, path, errors)
    if "anyOf" in schema or "oneOf" in schema:
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        matches = 0
        collected: list[list[str]] = []
        for sub in alternatives:
            sub_errors: list[str] = []
            _validate(value, sub, root, path, sub_errors)
            collected.append(sub_errors)
            if not sub_errors:
                matches += 1
        if matches == 0:
            errors.append(f"{path}: matched no alternative ({' | '.join(e[0] for e in collected if e)})")
        elif "oneOf" in schema and matches > 1:
            errors.append(f"{path}: matched {matches} alternatives of oneOf")
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS[t](value) for t in types):
            errors.append(f"{path}: expected {'/'.join(types)}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}, got {value!r}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']!r}")
        if "format" in schema:
            _check_format(schema["format"], value, path, errors)
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {value} >= exclusiveMaximum {schema['exclusiveMaximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        for name, sub in props.items():
            if name in value:
                _validate(value[name], sub, root, f"{path}.{name}", errors)
        extra = schema.get("additionalProperties", True)
        if extra is not True:
            for name in value:
                if name in props:
                    continue
                if extra is False:
                    errors.append(f"{path}: additional property {name!r} is not allowed")
                else:
                    _validate(value[name], extra, root, f"{path}.{name}", errors)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(item, schema["items"], root, f"{path}[{i}]", errors)


def errors_for(value: Any, schema: dict | str) -> list[str]:
    """Every validation error for ``value`` against ``schema`` (a dict or a kind name)."""
    root = load_schema(schema) if isinstance(schema, str) else schema
    errors: list[str] = []
    _validate(value, root, root, "$", errors)
    return errors


def validate(value: Any, schema: dict | str) -> None:
    """Raise :class:`SchemaError` unless ``value`` conforms."""
    errors = errors_for(value, schema)
    if errors:
        raise SchemaError(errors)


def validate_document(document: dict) -> None:
    """Validate an app-facing file by the ``kind`` it declares."""
    if not isinstance(document, dict) or "kind" not in document:
        raise SchemaError(["$: an app document is an object with a 'kind'"])
    validate(document, document["kind"])


def schema_keywords(schema: Any) -> set[str]:
    """Every keyword used anywhere in a schema tree (for the supported-subset test)."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, sub in schema.items():
            if key in ("properties", "$defs"):
                for child in sub.values():
                    found |= schema_keywords(child)
            elif key in ("items", "additionalProperties"):
                found.add(key)
                found |= schema_keywords(sub)
            elif key in ("anyOf", "oneOf", "allOf"):
                found.add(key)
                for child in sub:
                    found |= schema_keywords(child)
            else:
                found.add(key)
    return found
