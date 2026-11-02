"""Bounded clone retry (PR #117): hermetic, no network, no sleeping.

A PATH shim stands in for ``git`` and fails clones on demand ("curl 56 Connection reset", run 41494783124). The
function under test is the REAL ``scripts/clone_retry.sh`` that both workflows source, run inside the same
``for destination ... if ! clone_retry; then failures++; continue; fi`` shape the workflows use.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clone_retry.sh"
WORKFLOWS = [ROOT / ".github" / "workflows" / n for n in ("deliver-wagers.yml", "settle-wagers.yml")]

SHIM = """#!/usr/bin/env bash
# fake git: `git clone --quiet --depth 1 --branch B URL DEST`. FAIL_PLAN is "repo=N,..." = fail first N attempts
# for that repo (N=999 means always). Appends one line per attempt to $SHIM_LOG; a successful clone makes DEST.
url=""; dest=""
for a in "$@"; do url="$dest"; dest="$a"; done
repo="${url##*/}"
echo "clone ${repo}" >> "${SHIM_LOG}"
seen=$(grep -c "^clone ${repo}$" "${SHIM_LOG}")
limit=0
IFS=',' read -ra plan <<< "${FAIL_PLAN:-}"
for item in "${plan[@]}"; do [ "${item%%=*}" = "${repo}" ] && limit="${item##*=}"; done
if [ "${seen}" -le "${limit}" ]; then
  mkdir -p "${dest}"; echo partial > "${dest}/PARTIAL"   # a reset connection leaves debris
  echo "curl: (56) Recv failure: Connection reset by peer" >&2; exit 128
fi
mkdir -p "${dest}"; echo ok > "${dest}/CLONED"
"""

LOOP = """
set -euo pipefail
source "{script}"
failures=0
for sport in MLB NBA NFL; do
  repo="repo-${{sport}}"
  work="${{WORK}}/${{sport}}"
  if ! clone_retry --branch main "https://github.com/o/${{repo}}" "${{work}}"; then
    echo "FAILED ${{sport}}"; failures=$((failures + 1)); continue
  fi
  echo "DELIVERED ${{sport}}" >> "${{WORK}}/delivered.log"
done
echo "failures=${{failures}}"
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(SHIM)
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    nosleep = bin_dir / "nosleep"
    nosleep.write_text('#!/usr/bin/env bash\necho "$1" >> "${SHIM_LOG}.sleeps"\n')
    nosleep.chmod(nosleep.stat().st_mode | stat.S_IEXEC)
    work = tmp_path / "work"
    work.mkdir()
    e = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SHIM_LOG=str(tmp_path / "git.log"),
             CLONE_RETRY_SLEEP=str(nosleep), WORK=str(work))
    return e, tmp_path, work


def run(e, plan=""):
    e = dict(e, FAIL_PLAN=plan)
    return subprocess.run(["bash", "-c", LOOP.format(script=SCRIPT)], env=e, capture_output=True, text=True)


def attempts(tmp_path):
    log = (tmp_path / "git.log").read_text().split()
    return {r: log.count(r) for r in set(log) if r != "clone"}


def test_transient_failure_retries_only_the_failed_destination(env):
    e, tmp, work = env
    r = run(e, "repo-MLB=2")  # MLB resets twice, then succeeds
    assert r.returncode == 0, r.stderr
    assert attempts(tmp) == {"repo-MLB": 3, "repo-NBA": 1, "repo-NFL": 1}
    assert "failures=0" in r.stdout
    assert (work / "delivered.log").read_text().split() == ["DELIVERED", "MLB", "DELIVERED", "NBA", "DELIVERED", "NFL"]


def test_each_retry_starts_from_a_clean_directory(env):
    e, tmp, work = env
    run(e, "repo-MLB=2")
    assert (work / "MLB" / "CLONED").exists()
    assert not (work / "MLB" / "PARTIAL").exists()  # the debris of the failed attempts never survives


def test_backoff_is_bounded_and_exponential(env):
    e, tmp, _ = env
    run(e, "repo-MLB=999")
    assert attempts(tmp)["repo-MLB"] == 4  # four attempts, never more
    assert (tmp / "git.log.sleeps").read_text().split() == ["2", "4", "8"]  # no sleep after the last failure


def test_persistent_failure_fails_only_that_sport_and_the_rest_still_deliver(env):
    e, tmp, work = env
    r = run(e, "repo-MLB=999")
    assert r.returncode == 0, r.stderr  # the loop survives; the caller counts the failure
    assert "FAILED MLB" in r.stdout and "failures=1" in r.stdout
    assert (work / "delivered.log").read_text().split() == ["DELIVERED", "NBA", "DELIVERED", "NFL"]
    assert attempts(tmp) == {"repo-MLB": 4, "repo-NBA": 1, "repo-NFL": 1}


def test_rerun_after_a_transient_failure_is_idempotent(env):
    """The retry only ever touches the checkout. Run the loop twice: every destination is delivered exactly once per
    run, the failed-then-recovered one is not delivered twice within a run, and nothing but checkouts is written."""
    e, tmp, work = env
    run(e, "repo-NBA=1")
    first = (work / "delivered.log").read_text().split()
    assert first.count("NBA") == 1 and sorted(set(first)) == ["DELIVERED", "MLB", "NBA", "NFL"]
    (work / "delivered.log").unlink()
    run(e, "")
    second = (work / "delivered.log").read_text().split()
    assert second == first
    assert sorted(p.name for p in work.iterdir()) == ["MLB", "NBA", "NFL", "delivered.log"]


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_source_the_tested_script_and_never_clone_bare(wf):
    text = wf.read_text()
    assert 'source "${GITHUB_WORKSPACE}/scripts/clone_retry.sh"' in text
    assert "clone_retry() {" not in text  # one definition, in the tested file
    for line in text.splitlines():
        if re.search(r"\bgit clone\b", line) and not line.lstrip().startswith("#"):
            pytest.fail(f"bare git clone in {wf.name}: {line.strip()}")
    calls = re.findall(r"^\s*(if ! )?clone_retry\b", text, re.M)
    assert calls and all(calls), "every clone_retry call must be guarded by `if !`"
