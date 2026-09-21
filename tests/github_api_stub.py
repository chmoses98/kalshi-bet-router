"""A GitHub API stand-in that answers from a real local git repository.

The delivery step now ends by asking GitHub four questions -- is there a pull
request, is it mergeable, is its CI green, and will you merge it -- and acting
on the answers. Testing that against `api.github.com` is not an option, and
testing it against a hand-written dict of canned JSON would test the dict.

So this serves the endpoints `scripts/merge_delivery_pr.py` calls, and answers
them from the BARE REPOSITORY the delivery step actually pushed to:

  * the pull request's head SHA is `git rev-parse` on the pushed branch;
  * merging runs a real ref update, so afterwards the rows are genuinely on
    `main` and a re-run of the delivery really does find them there.

What is faked is the parts a test cannot have: CI conclusions and
mergeability, which are inputs the test sets on purpose to drive each branch
of the gate.

`merge_delivery_pr.py` reads `GITHUB_API_ROOT` from the environment, which is
the only hook needed -- no monkeypatching, no import-time surgery, and the
script under test is the committed one.

ONE DELIBERATE SIMPLIFICATION. A real squash merge creates a NEW commit on
`main` whose tree matches the branch. This fast-forwards `main` to the branch
tip instead. The property under test is "the canonical rows are on main", and
both produce exactly that tree; reproducing GitHub's commit shaping would add
a moving part without adding a property.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class GitHubStub:
    def __init__(self, remote_path, destination, *, branch,
                 base_branch="main",
                 check_runs=(("test", "completed", "success"),),
                 mergeable=True, mergeable_state="clean", draft=False,
                 allow_merge=True, pull_number=218):
        self.remote_path = str(remote_path)
        self.destination = destination
        self.branch = branch
        #: The branch a router pull request TARGETS. `main` for MLB, but not
        #: for every destination: CFB's canonical ledger lives on
        #: `accounting-data`, and a stub that always answered `main` would let
        #: a gate hardcoded to `main` pass a test it should fail.
        self.base_branch = base_branch
        self.check_runs = list(check_runs)
        self.mergeable = mergeable
        self.mergeable_state = mergeable_state
        self.draft = draft
        self.allow_merge = allow_merge
        self.pull_number = pull_number
        self.merged_sha = None
        self.merge_requests = []
        self._server = None
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                status, payload = stub.handle_get(urlparse(self.path))
                self._send(status, payload)

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                status, payload = stub.handle_put(urlparse(self.path), body)
                self._send(status, payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ── the repository is the source of truth ────────────────────────
    def _ref(self, name):
        result = subprocess.run(
            ["git", f"--git-dir={self.remote_path}", "rev-parse", "--verify", "--quiet", name],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() or None

    def branch_sha(self):
        return self._ref(f"refs/heads/{self.branch}")

    def main_sha(self):
        return self._ref(f"refs/heads/{self.base_branch}")

    def _pull_payload(self):
        sha = self.branch_sha()
        if sha is None:
            return None
        return {
            "number": self.pull_number,
            "state": "closed" if self.merged_sha else "open",
            "draft": self.draft,
            "merged": bool(self.merged_sha),
            "head": {"ref": self.branch, "sha": sha,
                     "repo": {"full_name": self.destination}},
            "base": {"ref": self.base_branch},
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
        }

    # ── routing ──────────────────────────────────────────────────────
    def handle_get(self, url):
        parts = [p for p in url.path.split("/") if p]
        # /repos/{owner}/{repo}/...
        if len(parts) < 4 or parts[0] != "repos":
            return 404, {"message": "not found"}
        tail = parts[3:]

        if tail[:1] == ["pulls"] and len(tail) == 1:
            pull = self._pull_payload()
            # The script filters by open state; a merged pull request must
            # stop being listed, exactly as GitHub stops listing it.
            return 200, ([pull] if pull and pull["state"] == "open" else [])

        if tail[:1] == ["pulls"] and len(tail) == 2:
            pull = self._pull_payload()
            return (200, pull) if pull else (404, {"message": "not found"})

        if tail[:1] == ["commits"] and tail[-1:] == ["check-runs"]:
            return 200, {"check_runs": [
                {"name": name, "status": status, "conclusion": conclusion}
                for name, status, conclusion in self.check_runs
            ]}

        if tail[:1] == ["commits"] and tail[-1:] == ["status"]:
            return 200, {"statuses": []}

        return 404, {"message": "not found"}

    def handle_put(self, url, body):
        parts = [p for p in url.path.split("/") if p]
        if parts[-1] != "merge":
            return 404, {"message": "not found"}
        self.merge_requests.append(body)
        if not self.allow_merge:
            return 405, {"message": "merge is blocked"}

        head = self.branch_sha()
        if body.get("sha") and body["sha"] != head:
            # Exactly what GitHub does when the branch moved between the
            # caller's evaluation and its merge call.
            return 409, {"message": "head has changed"}

        subprocess.run(
            ["git", f"--git-dir={self.remote_path}", "update-ref",
             f"refs/heads/{self.base_branch}", head],
            check=True, capture_output=True,
        )
        self.merged_sha = head
        return 200, {"merged": True, "sha": head, "message": "Pull Request merged"}
