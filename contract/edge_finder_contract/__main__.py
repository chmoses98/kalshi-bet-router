"""``python -m edge_finder_contract <command>``

    validate <file.json | app_root>...      validate documents by kind, or a published app root
    verify-explorer <app_root>              re-validate a published explorer/ tree (graph + capabilities)
    packet <app_root> game <event_id>       build the handicap packet for one event (JSON on stdout)
    packet <app_root> slate <start> <end>   ... for every event in a UTC window
    packet <app_root> tray <tray.json>      ... for a research tray
        options: --text (clipboard rendering instead of JSON), --protocol <id>, --max-chars <n>
    protocols                               list the handicap protocols shipped with the contract
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .publish import verify_published
from .validate import SchemaError, validate_document


def _validate(paths: list[str]) -> int:
    from .research import verify_explorer

    failures = 0
    for name in paths:
        path = Path(name)
        if path.is_dir():
            problems = verify_published(path)
            if (path / "explorer" / "index.json").exists():
                problems += verify_explorer(path)
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


def _packet(argv: list[str]) -> int:
    from . import packet as P

    text = "--text" in argv
    argv = [a for a in argv if a != "--text"]
    protocol = None
    max_chars = P.DEFAULT_MAX_CHARS
    if "--protocol" in argv:
        i = argv.index("--protocol")
        protocol = argv[i + 1]
        del argv[i:i + 2]
    if "--max-chars" in argv:
        i = argv.index("--max-chars")
        max_chars = int(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) < 3:
        print(__doc__)
        return 2
    root, scope = Path(argv[0]), argv[1].lower()
    if scope == "game":
        pk = P.build(app_root=root, scope_kind="GAME", event_id=argv[2], protocol_id=protocol, max_chars=max_chars)
    elif scope == "slate":
        pk = P.build(app_root=root, scope_kind="SLATE", window_start=argv[2], window_end=argv[3], protocol_id=protocol,
                     max_chars=max_chars)
    elif scope == "tray":
        tray = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        pk = P.build(app_root=root, scope_kind="CUSTOM", tray_doc=tray, protocol_id=protocol, max_chars=max_chars)
    else:
        print(__doc__)
        return 2
    sys.stdout.write(P.render_text(pk) if text else json.dumps(pk, indent=2, sort_keys=True) + "\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "validate":
        return _validate(argv[1:])
    if len(argv) == 2 and argv[0] == "verify-explorer":
        from .research import verify_explorer

        problems = verify_explorer(Path(argv[1]))
        print(f"{argv[1]}: {'OK' if not problems else problems}")
        return 1 if problems else 0
    if argv and argv[0] == "packet":
        return _packet(argv[1:])
    if argv == ["protocols"]:
        from .packet import protocol_ids

        print("\n".join(protocol_ids()))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
