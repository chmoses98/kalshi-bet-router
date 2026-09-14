"""Guards that keep private account data out of public surfaces."""

from __future__ import annotations

import re

import os

from .errors import SensitiveOutputRefused

#: Environment markers set by CI providers.  Sensitive diagnostics refuse to run
#: when any of these is set, so the local-only mode cannot be turned on inside a
#: workflow by adding a flag to the command line.
CI_ENV_MARKERS = (
    "GITHUB_ACTIONS",
    "GITHUB_WORKFLOW",
    "GITHUB_RUN_ID",
    "CI",
    "BUILDKITE",
    "CIRCLECI",
    "GITLAB_CI",
    "JENKINS_URL",
    "TF_BUILD",
)


def detected_ci_markers(env: dict[str, str] | None = None) -> list[str]:
    """Return the CI markers present in the environment."""
    source = os.environ if env is None else env
    return [name for name in CI_ENV_MARKERS if (source.get(name) or "").strip()]


def assert_sensitive_output_allowed(env: dict[str, str] | None = None) -> None:
    """Refuse sensitive diagnostics anywhere that looks automated.

    This is the second half of the local-only guarantee.  The first half is that
    the flag must be typed explicitly; this half is that typing it inside CI is a
    hard error rather than a disclosure.
    """
    markers = detected_ci_markers(env)
    if markers:
        raise SensitiveOutputRefused(
            "Refusing to print sensitive per-fill diagnostics: this looks like an "
            "automated environment (" + ", ".join(markers) + "). "
            "This mode is for a private local terminal only."
        )


#: A JSON schema name we are willing to print in a public log: lowercase words
#: joined by underscores, nothing else.  Deliberately narrow.  A market ticker
#: (``KXMLBGAME-26SEP01-NYY``) fails on case and punctuation; an id, a price, a
#: timestamp and a competition name all fail too.  So the one string-valued
#: diagnostic on the audit report is bounded by construction, not by convention.
SCHEMA_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

#: The value-shape label that may accompany a schema name, e.g. ``list[str]``.
SCHEMA_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,15}(\[[a-z0-9_|]{0,31}\])?$")


def safe_schema_name(key: str, kind: str | None = None) -> str | None:
    """Return ``key`` (optionally ``key:kind``) if it is safe to print, else None.

    Fails closed: anything that is not plainly a schema identifier is dropped
    rather than sanitized, because a partially-scrubbed value is still a value.
    """
    if not isinstance(key, str) or not SCHEMA_NAME_PATTERN.match(key):
        return None
    if kind is None:
        return key
    if not isinstance(kind, str) or not SCHEMA_KIND_PATTERN.match(kind):
        return f"{key}:?"
    return f"{key}:{kind}"
