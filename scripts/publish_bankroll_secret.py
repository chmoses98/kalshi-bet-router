#!/usr/bin/env python3
"""Seal a bankroll context into a destination repository's Actions secret.

THE NUMBER NEVER TOUCHES A PUBLIC SURFACE
-----------------------------------------
Both repositories in this system are public, so a balance that reaches a
log line, a job summary or an uploaded artifact is a balance published to
the internet. A GitHub Actions secret is the one channel here that is not:
it is sealed client-side with libsodium against the destination
repository's Actions public key, the REST API offers no way to read the
plaintext back, and it is decrypted only inside a workflow run of that
repository.

So this script:

  * reads the minimal context (five fields, no account metadata) from a
    file in RUNNER_TEMP;
  * validates it against the same allowlist that produced it, so a future
    edit cannot widen what gets shipped;
  * seals it and PUTs it as one named secret;
  * prints the shape of what it sent and never the amount.

Exit codes: 0 sent, 2 bad input/config, 3 the GitHub API refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_API = 3

API_ROOT = "https://api.github.com"

#: Must match kalshi_router.balance.PUBLISHED_KEYS. Duplicated as a literal
#: on purpose: this is the last checkpoint before the value leaves the
#: runner, and it should fail if the producer ever starts adding fields.
ALLOWED_KEYS = {
    "schemaVersion", "bankroll", "currency", "observedAt", "source", "valueType",
}


def _request(method, url, token, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else {})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="bankroll context JSON written by the CLI")
    parser.add_argument("--repo", required=True, help="destination owner/repo")
    parser.add_argument("--secret-name", required=True)
    parser.add_argument("--token-env", default="DOWNSTREAM_SECRETS_TOKEN")
    args = parser.parse_args(argv)

    import os

    token = (os.environ.get(args.token_env) or "").strip()
    if not token:
        print(f"{args.token_env} is empty", file=sys.stderr)
        return EXIT_CONFIG

    try:
        context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read the bankroll context: {type(exc).__name__}", file=sys.stderr)
        return EXIT_CONFIG

    if not isinstance(context, dict) or set(context) != ALLOWED_KEYS:
        # Names only -- a diff of key sets cannot leak a value.
        print(
            "bankroll context does not match the published allowlist; refusing to send it. "
            f"unexpected={sorted(set(context) - ALLOWED_KEYS)} "
            f"missing={sorted(ALLOWED_KEYS - set(context))}",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    try:
        from nacl import encoding, public
    except ImportError:
        print("pynacl is required to seal a secret", file=sys.stderr)
        return EXIT_CONFIG

    try:
        _status, key = _request(
            "GET", f"{API_ROOT}/repos/{args.repo}/actions/secrets/public-key", token)
    except urllib.error.HTTPError as exc:
        print(
            f"could not fetch {args.repo}'s Actions public key (HTTP {exc.code}). "
            "The token needs 'Secrets: Read and write' on that repository.",
            file=sys.stderr,
        )
        return EXIT_API
    except urllib.error.URLError as exc:
        print(f"could not reach the GitHub API: {type(exc).__name__}", file=sys.stderr)
        return EXIT_API

    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode("utf-8"), encoding.Base64Encoder())
    ).encrypt(json.dumps(context, sort_keys=True).encode("utf-8"))

    try:
        status, _ = _request(
            "PUT",
            f"{API_ROOT}/repos/{args.repo}/actions/secrets/{args.secret_name}",
            token,
            {"encrypted_value": b64encode(sealed).decode("utf-8"), "key_id": key["key_id"]},
        )
    except urllib.error.HTTPError as exc:
        print(f"could not write the secret (HTTP {exc.code})", file=sys.stderr)
        return EXIT_API
    except urllib.error.URLError as exc:
        print(f"could not reach the GitHub API: {type(exc).__name__}", file=sys.stderr)
        return EXIT_API

    # Shape, never amount.
    print(f"sealed {args.secret_name} into {args.repo} (HTTP {status})")
    print(f"  fields sent: {sorted(context)}")
    print(f"  observedAt:  {context['observedAt']}")
    print(f"  valueType:   {context['valueType']}")
    print("  amount:      ***withheld*** (both repositories are public)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
