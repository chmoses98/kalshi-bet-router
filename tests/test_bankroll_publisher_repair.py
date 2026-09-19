"""THE BANKROLL PUBLISHER FAILED TEN TIMES AND NOBODY COULD TELL WHY.

Every scheduled run of `publish-bankroll.yml` from 2026-09-18 onward failed
identically. "Read the balance" SUCCEEDED. "Seal it into the destination
repository" exited 1. The reason, buried in the step log:

    DOWNSTREAM_SECRETS_TOKEN:                     <- empty
    KALSHI_API_KEY_ID: ***                        <- masked, i.e. configured
    ::error::DOWNSTREAM_SECRETS_TOKEN is not configured.

A configured Actions secret always masks, so an empty render is proof the
secret does not exist. That is a configuration gap only the repository owner
can close, and no amount of code creates a credential. What code CAN fix is
everything the publisher did around it:

  * it read a live balance from the owner's account before discovering it
    could not deliver it;
  * absent token, wrong scope, wrong repository and an unreachable API were
    one exit code and one sentence;
  * a `204 No Content` was trusted as proof the destination held the secret;
  * the context's SHAPE was validated and its MEANING was not -- a payload
    carrying mark-to-market portfolio value under the right key names would
    have been sealed and shipped.

These tests pin all four, and they pin the property that outranks them:
THE AMOUNT NEVER APPEARS ANYWHERE. Every test here uses one distinctive
balance and greps every surface the publisher can write to for its digits.

The GitHub API is a local stand-in with a REAL libsodium keypair, so the
sealed bytes are decrypted and compared against the context field by field.
"Sealing succeeds" is therefore a decryption this test performs, not a status
code it believes.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import threading
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from nacl import encoding, public

from kalshi_router import cli
from kalshi_router.balance import (
    PUBLISHED_KEYS,
    SOURCE_AUTHENTICATED_BALANCE,
    VALUE_TYPE_AVAILABLE_CASH,
    build_bankroll_context,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish-bankroll.yml"
PUBLISHER = ROOT / "scripts/publish_bankroll_secret.py"

DESTINATION = "chmoses98/edge-finder-api"
SECRET_NAME = "KALSHI_BANKROLL_CONTEXT"

#: One distinctive balance, used everywhere, so a leak anywhere is findable.
BALANCE_CENTS = 731917                         # $7,319.17
BALANCE_DIGITS = ("731917", "7319.17", "7,319.17", "7319")


def _publisher():
    spec = importlib.util.spec_from_file_location("publish_bankroll_secret", PUBLISHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = _publisher()


def _now():
    return datetime.now(tz=timezone.utc)


def _context(**overrides):
    context = build_bankroll_context({"balance": BALANCE_CENTS}, observed_at=_now())
    context.update(overrides)
    return context


# ══════════════════════════════════════════════════════════════════════
# A GitHub Actions-secrets stand-in with a real keypair
# ══════════════════════════════════════════════════════════════════════

class SecretsStub:
    """Serves the three endpoints the publisher calls, and can decrypt.

    The keypair is genuine libsodium, so `sealed` below is the same
    ciphertext GitHub would store and `decrypt()` is the same operation the
    destination's runner performs. Nothing about the sealing is simulated.
    """

    def __init__(self, *, public_key_status=200, put_status=204,
                 verify_status=200, existing_updated_at=None, fail_times=0,
                 freeze_updated_at=False):
        self._private = public.PrivateKey.generate()
        self.public_key_b64 = self._private.public_key.encode(
            encoding.Base64Encoder()).decode()
        self.key_id = "test-key-1"
        self.public_key_status = public_key_status
        self.put_status = put_status
        self.verify_status = verify_status
        self.updated_at = existing_updated_at
        self.fail_times = fail_times
        # Models the write that is accepted and changes nothing.
        self.freeze_updated_at = freeze_updated_at
        self.sealed = None
        self.puts = []
        self.requests = []
        self._server = None

    def decrypt(self):
        assert self.sealed is not None, "nothing was sealed"
        box = public.SealedBox(self._private)
        return json.loads(box.decrypt(b64decode(self.sealed)).decode())

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                stub.requests.append(("GET", path))
                if stub.fail_times > 0:
                    stub.fail_times -= 1
                    return self._send(503, {"message": "try again"})
                if path.endswith("/actions/secrets/public-key"):
                    if stub.public_key_status != 200:
                        return self._send(stub.public_key_status, {"message": "no"})
                    return self._send(200, {"key": stub.public_key_b64,
                                            "key_id": stub.key_id})
                if path.endswith(f"/actions/secrets/{SECRET_NAME}"):
                    if stub.verify_status != 200:
                        return self._send(stub.verify_status, {"message": "no"})
                    return self._send(200, {"name": SECRET_NAME,
                                            "created_at": stub.updated_at,
                                            "updated_at": stub.updated_at})
                return self._send(404, {"message": "not found"})

            def do_PUT(self):  # noqa: N802
                path = urlparse(self.path).path
                stub.requests.append(("PUT", path))
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.puts.append((path, body))
                if stub.put_status in (201, 204):
                    stub.sealed = body.get("encrypted_value")
                    if not stub.freeze_updated_at:
                        stub.updated_at = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
                return self._send(stub.put_status)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def stub():
    server = SecretsStub()
    server.base = server.start()
    try:
        yield server
    finally:
        server.stop()


def run_publisher(argv, *, api_root, token="ghp_test", env=None, cwd=None):
    """The COMMITTED script, in a subprocess, so stdout and stderr are the
    real public surfaces rather than a captured call."""
    environment = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GITHUB_API_ROOT": api_root,
        "DOWNSTREAM_SECRETS_TOKEN": token,
        "PYTHONPATH": str(ROOT / "src"),
    }
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, str(PUBLISHER), *argv],
        capture_output=True, text=True, env=environment, cwd=cwd or str(ROOT),
    )


def write_context(tmp_path, context):
    path = tmp_path / "bankroll.json"
    path.write_text(json.dumps(context), encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# 14. the balance reads, and sealing succeeds
# ══════════════════════════════════════════════════════════════════════

def test_the_balance_read_produces_exactly_the_publishable_context(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    args = cli.build_parser().parse_args(
        ["bankroll", "--out", str(tmp_path / "bankroll.json")])

    class _Client:
        def get_balance(self):
            return {"balance": BALANCE_CENTS, "portfolio_value": 999999,
                    "account_id": "ACCT-LEAK"}

    assert cli._run_bankroll(args, _Client(), out, err) == cli.EXIT_OK
    context = json.loads((tmp_path / "bankroll.json").read_text())
    assert set(context) == PUBLISHED_KEYS
    assert context["bankroll"] == pytest.approx(7319.17)
    assert context["valueType"] == VALUE_TYPE_AVAILABLE_CASH
    assert context["source"] == SOURCE_AUTHENTICATED_BALANCE


def test_sealing_succeeds_and_the_destination_can_decrypt_it_exactly(stub, tmp_path):
    """THE HEADLINE. Not a status code -- the sealed bytes are decrypted
    with the destination's own private key and compared field by field."""
    context = _context()
    path = write_context(tmp_path, context)

    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert stub.puts, "nothing was PUT"
    assert stub.puts[-1][0].endswith(f"/actions/secrets/{SECRET_NAME}")
    assert stub.puts[-1][1]["key_id"] == stub.key_id
    assert stub.decrypt() == context
    assert "sealed" in result.stdout


def test_the_seal_is_verified_against_the_destination_not_assumed(stub, tmp_path):
    path = write_context(tmp_path, _context())
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == 0
    assert ("GET", f"/repos/{DESTINATION}/actions/secrets/{SECRET_NAME}") in stub.requests
    assert "verified:" in result.stdout


def test_an_accepted_write_the_destination_does_not_show_is_not_a_success(stub, tmp_path):
    """A 204 proves a request was accepted, not that anything landed. This
    is the exact shape of 'the publisher reported success for three days
    while the destination had nothing'."""
    stub.verify_status = 404
    path = write_context(tmp_path, _context())
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == publisher.EXIT_UNVERIFIED
    assert "does not show it" in result.stderr or "not readable" in result.stderr


def test_a_secret_whose_timestamp_predates_this_run_is_not_a_success(tmp_path):
    """The write is accepted and the destination's metadata never moves --
    which is a secret that did not land, wearing a 204."""
    server = SecretsStub(freeze_updated_at=True,
                         existing_updated_at="2026-09-01T00:00:00Z")
    base = server.start()
    try:
        path = write_context(tmp_path, _context())
        result = run_publisher(
            ["--context", str(path), "--repo", DESTINATION,
             "--secret-name", SECRET_NAME], api_root=base)

        assert server.puts, "the write never happened"
        assert result.returncode == publisher.EXIT_UNVERIFIED
        assert "BEFORE this run" in result.stderr
    finally:
        server.stop()


def test_skipping_verification_is_possible_but_not_the_default(stub, tmp_path):
    path = write_context(tmp_path, _context())
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION,
         "--secret-name", SECRET_NAME, "--no-verify"], api_root=stub.base)
    assert result.returncode == 0
    assert ("GET", f"/repos/{DESTINATION}/actions/secrets/{SECRET_NAME}") not in stub.requests
    # And the committed workflow does NOT pass it.
    assert "--no-verify" not in WORKFLOW.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
# 15. the amount never appears in public output
# ══════════════════════════════════════════════════════════════════════

def _assert_no_amount(text, where):
    for digits in BALANCE_DIGITS:
        assert digits not in text, f"the balance leaked into {where}: {text!r}"


def test_the_amount_never_reaches_stdout_or_stderr_on_success(stub, tmp_path):
    path = write_context(tmp_path, _context())
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)
    _assert_no_amount(result.stdout, "stdout")
    _assert_no_amount(result.stderr, "stderr")
    assert "***withheld***" in result.stdout


@pytest.mark.parametrize("overrides", [
    {"valueType": "KALSHI_PORTFOLIO_VALUE"},
    {"source": "some_other_source"},
    {"currency": "EUR"},
    {"observedAt": "not-a-timestamp"},
])
def test_the_amount_never_reaches_output_on_a_refusal(stub, tmp_path, overrides):
    """A refusal prints field names and expectations. None of them may
    carry the number, because a refusal is the path that prints most."""
    path = write_context(tmp_path, _context(**overrides))
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == publisher.EXIT_CONFIG
    _assert_no_amount(result.stdout, "stdout")
    _assert_no_amount(result.stderr, "stderr")


def test_a_refusal_message_never_embeds_the_bankroll_value():
    """Driven directly, so every branch of the validator is exercised."""
    for context in (
        _context(bankroll="7319.17"),
        _context(bankroll=float("inf")),
        _context(bankroll=-7319.17),
        _context(bankroll=True),
    ):
        problems = " ".join(publisher.validate_context(context))
        assert problems
        _assert_no_amount(problems, "a validation message")


def test_the_only_thing_carrying_the_amount_is_the_sealed_ciphertext(stub, tmp_path):
    path = write_context(tmp_path, _context())
    run_publisher(["--context", str(path), "--repo", DESTINATION,
                   "--secret-name", SECRET_NAME], api_root=stub.base)

    sealed = stub.puts[-1][1]["encrypted_value"]
    _assert_no_amount(sealed, "the sealed payload (as base64)")
    # And it really is the amount, once decrypted with the private key.
    assert stub.decrypt()["bankroll"] == pytest.approx(7319.17)


def _executable_lines(text):
    """The workflow's executable content, with explanatory comments removed.

    The same approach `tests/test_workflow_safety.py` uses: these assertions
    must test the workflow, not its prose. The comment explaining WHY the
    context goes to RUNNER_TEMP legitimately contains the words `git add`.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))


def test_the_workflow_writes_the_context_outside_the_repository_and_commits_nothing():
    executable = _executable_lines(WORKFLOW.read_text(encoding="utf-8"))
    assert "${RUNNER_TEMP}/bankroll.json" in executable
    for forbidden in ("git add", "git commit", "git push", "upload-artifact",
                      "GITHUB_STEP_SUMMARY"):
        assert forbidden not in executable, forbidden


#: Bankroll figures that are allowed to exist in this repository, because
#: they are illustrations rather than readings. Every one is spelled out
#: here so that a NEW number appearing anywhere in git fails this test --
#: which is the actual invariant, since no test can know the owner's real
#: balance to search for it.
ILLUSTRATIVE_BANKROLL_VALUES = {
    "1234.56",      # docs/BANKROLL_DELIVERY.md -- the schema example
    "7319.17",      # this file's fixture
}


def test_no_committed_file_carries_a_bankroll_figure_that_is_not_an_illustration():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True).stdout.split()
    pattern = re.compile(r'"bankroll"\s*:\s*(-?[0-9][0-9_.eE+-]*)')
    offenders = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.suffix in {".png", ".jpg", ".gz"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for value in pattern.findall(text):
            if value not in ILLUSTRATIVE_BANKROLL_VALUES:
                offenders.append((name, value))
    assert offenders == [], offenders


def test_no_committed_file_is_a_bankroll_CONTEXT_artifact():
    """A stronger, shape-based sweep: a file whose JSON key set IS the
    published context is a delivered bankroll, whatever number it holds."""
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.json"],
                             capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for name in tracked:
        path = ROOT / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict) and set(payload) == PUBLISHED_KEYS:
            offenders.append(name)
    assert offenders == [], offenders


# ══════════════════════════════════════════════════════════════════════
# 16. only available cash may be sealed
# ══════════════════════════════════════════════════════════════════════

def test_portfolio_value_semantics_are_refused_before_sending(stub, tmp_path):
    """Mark-to-market value of open positions is not deployable cash.
    The producer already refuses to READ it; this refuses to SEND it, so a
    future producer change cannot quietly ship the wrong meaning."""
    path = write_context(tmp_path, _context(valueType="KALSHI_PORTFOLIO_VALUE"))
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == publisher.EXIT_CONFIG
    assert "valueType" in result.stderr
    assert stub.puts == [], "a non-cash value type was sent anyway"


def test_the_required_value_type_matches_the_producer_and_the_destination():
    """Three places must agree, and only one of them is in this file."""
    assert publisher.REQUIRED_VALUE_TYPE == VALUE_TYPE_AVAILABLE_CASH
    assert publisher.REQUIRED_SOURCE == SOURCE_AUTHENTICATED_BALANCE


def test_a_context_with_an_extra_key_is_refused(stub, tmp_path):
    path = write_context(tmp_path, _context(accountId="ACCT-LEAK"))
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)
    assert result.returncode == publisher.EXIT_CONFIG
    assert "accountId" in result.stderr
    assert stub.puts == []


def test_every_allowlisted_key_is_required(stub, tmp_path):
    for missing in sorted(PUBLISHED_KEYS):
        context = _context()
        context.pop(missing)
        problems = publisher.validate_context(context)
        assert problems, f"a context missing {missing} was accepted"


# ══════════════════════════════════════════════════════════════════════
# 17. a stale reading is never sealed, and never sizes
# ══════════════════════════════════════════════════════════════════════

def test_a_reading_past_the_destinations_window_is_not_sealed(stub, tmp_path):
    """Sealing it would replace a possibly-usable secret with one the
    destination is guaranteed to refuse."""
    old = (_now() - timedelta(minutes=publisher.DESTINATION_MAX_AGE_MINUTES + 5))
    path = write_context(tmp_path, _context(
        observedAt=old.strftime("%Y-%m-%dT%H:%M:%SZ")))

    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == publisher.EXIT_CONFIG
    assert "window" in result.stderr
    assert stub.puts == []


def test_a_reading_inside_the_window_is_sealed(stub, tmp_path):
    recent = _now() - timedelta(minutes=publisher.DESTINATION_MAX_AGE_MINUTES - 5)
    path = write_context(tmp_path, _context(
        observedAt=recent.strftime("%Y-%m-%dT%H:%M:%SZ")))
    result = run_publisher(
        ["--context", str(path), "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)
    assert result.returncode == 0, result.stderr


def test_a_reading_from_the_future_is_refused_as_a_broken_clock():
    ahead = _now() + timedelta(minutes=30)
    problems = publisher._freshness_problems(ahead.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert problems and "future" in problems[0]


def test_the_publishers_window_matches_the_destinations():
    """`edge-finder-api/lib/bankroll_context.DEFAULT_MAX_AGE_MINUTES`. If
    the destination's window ever changes, this is the tripwire."""
    assert publisher.DESTINATION_MAX_AGE_MINUTES == 30


# ══════════════════════════════════════════════════════════════════════
# The failure mode itself: a missing credential, diagnosed precisely
# ══════════════════════════════════════════════════════════════════════

def test_a_missing_token_is_reported_before_any_account_is_read(stub):
    result = run_publisher(
        ["--preflight-only", "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base, token="")

    assert result.returncode == publisher.EXIT_CREDENTIAL
    assert "DOWNSTREAM_SECRETS_TOKEN is not configured" in result.stderr
    assert "Secrets: Read and write" in result.stderr
    assert stub.requests == [], "the preflight called the API with no token"


@pytest.mark.parametrize("status,expected", [
    (401, "EXIT_CREDENTIAL"),
    (403, "EXIT_CREDENTIAL"),
    (404, "EXIT_CREDENTIAL"),
])
def test_a_rejected_credential_is_distinguished_from_a_missing_one(status, expected):
    server = SecretsStub(public_key_status=status)
    base = server.start()
    try:
        result = run_publisher(
            ["--preflight-only", "--repo", DESTINATION, "--secret-name", SECRET_NAME],
            api_root=base)
        assert result.returncode == getattr(publisher, expected)
        assert "Secrets: Read and write" in result.stderr
    finally:
        server.stop()


def test_a_good_credential_passes_the_preflight_without_reading_an_account(stub):
    result = run_publisher(
        ["--preflight-only", "--repo", DESTINATION, "--secret-name", SECRET_NAME],
        api_root=stub.base)

    assert result.returncode == 0
    assert "credential accepted" in result.stdout
    # ONE request, and it is read-only.
    assert stub.requests == [("GET", f"/repos/{DESTINATION}/actions/secrets/public-key")]
    assert stub.puts == []


def test_a_transient_api_failure_is_retried_rather_than_failing_the_run(tmp_path):
    """One 503 must not turn a fifteen-minute publisher red."""
    server = SecretsStub(fail_times=1)
    base = server.start()
    try:
        path = write_context(tmp_path, _context())
        result = run_publisher(
            ["--context", str(path), "--repo", DESTINATION,
             "--secret-name", SECRET_NAME], api_root=base)
        assert result.returncode == 0, result.stderr
        assert server.decrypt()["valueType"] == VALUE_TYPE_AVAILABLE_CASH
    finally:
        server.stop()


# ══════════════════════════════════════════════════════════════════════
# The workflow wires it in the right order and keeps its boundaries
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step_names(workflow):
    return [step.get("name") for step in workflow["jobs"]["publish"]["steps"]]


def test_the_credential_is_verified_before_the_balance_is_read(workflow):
    """THE ORDERING FIX. Ten runs read a live balance they could not
    deliver; none of them needed to."""
    names = step_names(workflow)
    preflight = next(i for i, n in enumerate(names) if n and "credential" in n.lower())
    read = next(i for i, n in enumerate(names) if n and "Read the balance" in n)
    seal = next(i for i, n in enumerate(names) if n and "Seal it" in n)
    assert preflight < read < seal, names


def test_the_preflight_step_holds_no_kalshi_credential(workflow):
    step = next(s for s in workflow["jobs"]["publish"]["steps"]
                if (s.get("name") or "").lower().startswith("verify the delivery"))
    assert "KALSHI_API_KEY_ID" not in (step.get("env") or {})
    assert "KALSHI_PRIVATE_KEY" not in (step.get("env") or {})
    assert "--preflight-only" in step["run"]


def test_the_workflow_still_uses_a_secrets_token_distinct_from_the_delivery_token():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "DOWNSTREAM_SECRETS_TOKEN" in text
    assert "DOWNSTREAM_REPO_TOKEN" not in text


def test_the_workflow_still_holds_no_write_permission_on_this_repository(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_the_missing_credential_error_names_the_remedy_and_the_consequence():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Secrets: Read and write" in text
    assert "NOTHING was read from the Kalshi account" in text
    assert "no dollar stake sizes" in text


def test_the_workflow_never_prints_or_reads_the_bankroll_field():
    """The reporting step prints the SHAPE. `bankroll` must not appear in
    any expression that could render it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "ctx['bankroll']" not in stripped
        assert "ctx[\"bankroll\"]" not in stripped
