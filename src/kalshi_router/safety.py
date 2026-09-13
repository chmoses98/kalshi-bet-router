"""Guards that keep private account data out of public surfaces."""

from __future__ import annotations

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
