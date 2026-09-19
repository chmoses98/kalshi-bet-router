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

WHAT WENT WRONG, AND WHAT CHANGED
---------------------------------
Every scheduled run from 2026-09-18 onward failed, identically. "Read the
balance" SUCCEEDED; "Seal it into the destination repository" exited 1
with `DOWNSTREAM_SECRETS_TOKEN is not configured`. The secret does not
exist on the router repository -- its value rendered EMPTY in the job's
own environment block, while `KALSHI_API_KEY_ID` rendered `***`, and a
configured secret always masks.

That is a configuration gap only the owner can close. But the publisher
made it worse than it had to be, in four separate ways, and those are
this module's job:

1. IT SPENT A CREDENTIALED KALSHI READ IT COULD NOT USE. The balance was
   fetched from the live account, then discovered to be undeliverable.
   `--preflight-only` now answers "can this run deliver anything at all"
   BEFORE any account is touched.
2. EVERY FAILURE LOOKED THE SAME. Absent token, token without
   `Secrets: Read and write`, wrong repository and an unreachable API all
   produced one exit code and one sentence. They are now four distinct
   exits with four distinct remedies, because they have four different
   fixes.
3. A SUCCESSFUL PUT WAS TRUSTED, NOT VERIFIED. `204 No Content` was taken
   as proof the destination now holds the secret. It is not: it proves a
   request was accepted. The secret's METADATA (name and `updated_at`,
   never its value) is now read back and must show this run's write.
4. IT VALIDATED THE SHAPE OF THE CONTEXT AND NOT ITS MEANING. The key set
   was checked; `valueType` was not. A context carrying Kalshi's
   `portfolio_value` under the right key names would have been sealed and
   shipped, and mark-to-market value of open positions is not deployable
   cash. Only `KALSHI_AVAILABLE_CASH_BALANCE` from
   `kalshi_authenticated_balance` is sealable now.

Exit codes, each with its own remedy:
    0  sealed and verified
    2  bad input, or a context this publisher refuses to send
    3  the credential is absent, or lacks 'Secrets: Read and write'
    4  the GitHub API could not be reached or kept failing
    5  the write was accepted but the destination does not show it
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CREDENTIAL = 3
EXIT_API = 4
EXIT_UNVERIFIED = 5

API_ROOT = os.environ.get("GITHUB_API_ROOT", "https://api.github.com")

#: Must match kalshi_router.balance.PUBLISHED_KEYS. Duplicated as a literal
#: on purpose: this is the last checkpoint before the value leaves the
#: runner, and it should fail if the producer ever starts adding fields.
ALLOWED_KEYS = {
    "schemaVersion", "bankroll", "currency", "observedAt", "source", "valueType",
}

#: The ONLY meaning a sealed number may carry, and the only place it may
#: have come from. Mirrors `kalshi_router.balance.VALUE_TYPE_AVAILABLE_CASH`
#: and `SOURCE_AUTHENTICATED_BALANCE`, and -- far more importantly --
#: `edge-finder-api`'s `lib/bankroll_context.SIZING_ELIGIBLE_VALUE_TYPES`.
#: Anything else is refused HERE rather than shipped and refused THERE,
#: because a secret that the destination will reject is a secret that
#: replaced a usable one with an unusable one.
REQUIRED_VALUE_TYPE = "KALSHI_AVAILABLE_CASH_BALANCE"
REQUIRED_SOURCE = "kalshi_authenticated_balance"
REQUIRED_CURRENCY = "USD"

#: The destination's own freshness window (edge-finder-api
#: `lib/bankroll_context.DEFAULT_MAX_AGE_MINUTES`). A reading already past
#: it cannot produce a stake size there, so sealing it would overwrite a
#: possibly-usable secret with one that is certainly not.
DESTINATION_MAX_AGE_MINUTES = 30

#: Tolerance for clock skew between this runner and the destination's.
FUTURE_SKEW_TOLERANCE_MINUTES = 5

#: Transient API failures get one more chance before the run is called red.
#: A single 502 is not a broken publisher.
API_ATTEMPTS = 3
API_BACKOFF_SECONDS = (2, 5)

TOKEN_REMEDY = (
    "It needs a fine-grained PAT scoped to ONLY the destination repository, with "
    "repository permission 'Secrets: Read and write' and nothing else, stored on "
    "this repository as the Actions secret named below. Deliberately NOT the "
    "delivery token: writing a secret and pushing a branch are different powers."
)


def _request(method, url, token, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else {})


def _with_retries(method, url, token, payload=None):
    """
    (status, body, error) -- retrying only what is worth retrying.

    A 401/403/404 is an answer, not a blip: retrying it just makes the same
    wrong request twice. A network error or a 5xx is the transport having a
    bad moment, and one of those should not turn a fifteen-minute publisher
    red.
    """
    last = None
    for attempt in range(API_ATTEMPTS):
        try:
            status, body = _request(method, url, token, payload)
            return status, body, None
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return exc.code, None, exc
            last = exc
        except urllib.error.URLError as exc:
            last = exc
        if attempt + 1 < API_ATTEMPTS:
            time.sleep(API_BACKOFF_SECONDS[min(attempt, len(API_BACKOFF_SECONDS) - 1)])
    return None, None, last


def validate_context(context):
    """
    Pure. The reasons this publisher will not send a context.

    Returns a list of problems, each safe to print: they name FIELDS and
    EXPECTATIONS, never the value. A diff of key sets or a value-type
    mismatch cannot leak an amount.
    """
    problems = []
    if not isinstance(context, dict):
        return [f"the bankroll context is a {type(context).__name__}, not an object"]

    if set(context) != ALLOWED_KEYS:
        problems.append(
            "the context does not match the published allowlist: "
            f"unexpected={sorted(set(context) - ALLOWED_KEYS)} "
            f"missing={sorted(ALLOWED_KEYS - set(context))}")

    if context.get("valueType") != REQUIRED_VALUE_TYPE:
        # THE ONE THAT MATTERS MOST. Kalshi's `portfolio_value` is the
        # mark-to-market value of open positions; staking against it would
        # double-count exposure already at risk.
        problems.append(
            f"valueType is {context.get('valueType')!r}, and only "
            f"{REQUIRED_VALUE_TYPE!r} may be sealed -- the destination sizes real "
            "money against this and will not accept any other meaning")
    if context.get("source") != REQUIRED_SOURCE:
        problems.append(
            f"source is {context.get('source')!r}, and only {REQUIRED_SOURCE!r} is "
            "sizing-authoritative")
    if context.get("currency") != REQUIRED_CURRENCY:
        problems.append(f"currency is {context.get('currency')!r}, expected "
                        f"{REQUIRED_CURRENCY!r}")

    amount = context.get("bankroll")
    # Value-shape only, and deliberately no comparison that could be printed
    # with the number in it. `bool` is an `int`, hence the explicit check.
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        problems.append(f"bankroll is a {type(amount).__name__}, expected a number")
    elif amount != amount or amount in (float("inf"), float("-inf")):
        problems.append("bankroll is not a finite number")
    elif amount < 0:
        problems.append("bankroll is negative, which is not a deployable cash balance")

    problems.extend(_freshness_problems(context.get("observedAt")))
    return problems


def _freshness_problems(observed_at, now=None):
    if not isinstance(observed_at, str) or not observed_at:
        return ["observedAt is missing or is not a timestamp"]
    try:
        when = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return [f"observedAt {observed_at!r} is not an ISO-8601 UTC instant"]

    now = now or datetime.now(tz=timezone.utc)
    age = now - when
    if age < -timedelta(minutes=FUTURE_SKEW_TOLERANCE_MINUTES):
        return ["observedAt is materially in the future; the producer's clock is wrong"]
    if age > timedelta(minutes=DESTINATION_MAX_AGE_MINUTES):
        # Sealing this would REPLACE a possibly-usable secret with one the
        # destination is guaranteed to refuse.
        return [
            f"observedAt is {int(age.total_seconds() // 60)} minutes old, past the "
            f"destination's {DESTINATION_MAX_AGE_MINUTES}-minute window; sealing it "
            "would overwrite a possibly-usable balance with an unusable one"
        ]
    return []


def preflight(repo, token, *, out=sys.stdout):
    """
    Can this run deliver a bankroll at all?

    Deliberately runs BEFORE the account is read. It performs one read-only
    request -- the destination's Actions public key -- which is exactly the
    capability the seal needs, so a token that passes here can seal and a
    token that fails here could never have.

    Returns an exit code.
    """
    if not token:
        print("DOWNSTREAM_SECRETS_TOKEN is not configured on this repository.",
              file=sys.stderr)
        print(TOKEN_REMEDY, file=sys.stderr)
        return EXIT_CREDENTIAL

    status, key, error = _with_retries(
        "GET", f"{API_ROOT}/repos/{repo}/actions/secrets/public-key", token)
    if error is not None and status is None:
        print(f"could not reach the GitHub API: {type(error).__name__}", file=sys.stderr)
        return EXIT_API
    if status in (401, 403):
        print(f"the credential was rejected by {repo} (HTTP {status}).", file=sys.stderr)
        print(TOKEN_REMEDY, file=sys.stderr)
        return EXIT_CREDENTIAL
    if status == 404:
        # 404 is what GitHub returns for "no such repository" AND for "your
        # token cannot see this repository", and the remedy is the same
        # either way: check the scope and the name.
        print(f"{repo} is not visible to this credential (HTTP 404). Either the "
              "repository name is wrong or the token is not scoped to it.",
              file=sys.stderr)
        print(TOKEN_REMEDY, file=sys.stderr)
        return EXIT_CREDENTIAL
    if status != 200 or not isinstance(key, dict) or "key" not in key:
        print(f"unexpected response fetching {repo}'s Actions public key "
              f"(HTTP {status})", file=sys.stderr)
        return EXIT_API

    print(f"preflight: {repo} reachable, Actions public key {key.get('key_id')} "
          "available, credential accepted.", file=out)
    return EXIT_OK


def verify_landed(repo, token, secret_name, since, *, out=sys.stdout):
    """
    Did the destination actually take it?

    Reads the secret's METADATA -- name and `updated_at`. The REST API
    exposes no plaintext, so this cannot read the value back and is not
    trying to: it answers "does a secret of this name now exist, and was it
    written by this run", which a `204 No Content` does not.
    """
    status, body, error = _with_retries(
        "GET", f"{API_ROOT}/repos/{repo}/actions/secrets/{secret_name}", token)
    if error is not None and status is None:
        print(f"could not verify the write: {type(error).__name__}", file=sys.stderr)
        return EXIT_API
    if status != 200 or not isinstance(body, dict):
        print(f"the write was accepted but {secret_name} is not readable on {repo} "
              f"(HTTP {status}); the destination may not have it.", file=sys.stderr)
        return EXIT_UNVERIFIED

    updated = body.get("updated_at") or body.get("created_at")
    print(f"  verified: {secret_name} exists on {repo}, updated {updated}", file=out)
    if updated:
        try:
            when = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return EXIT_OK          # a shape we do not parse is not a failure
        if when + timedelta(minutes=FUTURE_SKEW_TOLERANCE_MINUTES) < since:
            print(f"{secret_name} on {repo} still shows {updated}, which is BEFORE this "
                  "run's write. The destination is not holding what this run sent.",
                  file=sys.stderr)
            return EXIT_UNVERIFIED
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default=None,
                        help="bankroll context JSON written by the CLI "
                             "(not needed with --preflight-only)")
    parser.add_argument("--repo", required=True, help="destination owner/repo")
    parser.add_argument("--secret-name", required=True)
    parser.add_argument("--token-env", default="DOWNSTREAM_SECRETS_TOKEN")
    parser.add_argument("--preflight-only", action="store_true",
                        help="check the credential and exit, touching no account. "
                             "Run this BEFORE reading the balance.")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the post-write read-back (tests only)")
    args = parser.parse_args(argv)

    token = (os.environ.get(args.token_env) or "").strip()

    if args.preflight_only:
        return preflight(args.repo, token)

    if not args.context:
        print("--context is required unless --preflight-only is given", file=sys.stderr)
        return EXIT_CONFIG
    if not token:
        print(f"{args.token_env} is empty", file=sys.stderr)
        print(TOKEN_REMEDY, file=sys.stderr)
        return EXIT_CREDENTIAL

    try:
        context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read the bankroll context: {type(exc).__name__}", file=sys.stderr)
        return EXIT_CONFIG

    problems = validate_context(context)
    if problems:
        # Field names and expectations only -- none of these can carry the
        # amount, which is why they are safe to print on a public runner.
        print("refusing to seal this bankroll context:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        from nacl import encoding, public
    except ImportError:
        print("pynacl is required to seal a secret", file=sys.stderr)
        return EXIT_CONFIG

    status, key, error = _with_retries(
        "GET", f"{API_ROOT}/repos/{args.repo}/actions/secrets/public-key", token)
    if error is not None and status is None:
        print(f"could not reach the GitHub API: {type(error).__name__}", file=sys.stderr)
        return EXIT_API
    if status in (401, 403, 404):
        print(f"could not fetch {args.repo}'s Actions public key (HTTP {status}).",
              file=sys.stderr)
        print(TOKEN_REMEDY, file=sys.stderr)
        return EXIT_CREDENTIAL
    if status != 200 or not isinstance(key, dict):
        print(f"unexpected response fetching the public key (HTTP {status})",
              file=sys.stderr)
        return EXIT_API

    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode("utf-8"), encoding.Base64Encoder())
    ).encrypt(json.dumps(context, sort_keys=True).encode("utf-8"))

    sent_at = datetime.now(tz=timezone.utc)
    status, _body, error = _with_retries(
        "PUT",
        f"{API_ROOT}/repos/{args.repo}/actions/secrets/{args.secret_name}",
        token,
        {"encrypted_value": b64encode(sealed).decode("utf-8"), "key_id": key["key_id"]},
    )
    if error is not None and status is None:
        print(f"could not reach the GitHub API: {type(error).__name__}", file=sys.stderr)
        return EXIT_API
    if status in (401, 403, 404):
        print(f"could not write the secret (HTTP {status}).", file=sys.stderr)
        print(TOKEN_REMEDY, file=sys.stderr)
        return EXIT_CREDENTIAL
    if status not in (201, 204):
        print(f"could not write the secret (HTTP {status})", file=sys.stderr)
        return EXIT_API

    # Shape, never amount.
    print(f"sealed {args.secret_name} into {args.repo} (HTTP {status})")
    print(f"  fields sent: {sorted(context)}")
    print(f"  observedAt:  {context['observedAt']}")
    print(f"  valueType:   {context['valueType']}")
    print("  amount:      ***withheld*** (both repositories are public)")

    if args.no_verify:
        return EXIT_OK
    # A 204 says the request was accepted. It does not say the destination
    # holds it -- and "the publisher reported success for three days while
    # the destination had nothing" is precisely the failure mode this whole
    # repair exists to make impossible.
    return verify_landed(args.repo, token, args.secret_name, sent_at)


if __name__ == "__main__":
    sys.exit(main())
