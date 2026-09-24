"""Destination profiles: activation, per-destination facts, and refusal.

*** THE DEFECT THIS FILE GUARDS AGAINST ***
Production knew a destination in five places -- a repository map, a workflow's
inline importer command, a ledger-branch literal, a containment prefix and a
mergeable-path set -- and every one of them was MLB's answer. The consequence
was not a crash: CFB simply could not be routed by the scheduled job, while the
one-time backfill workflow routed it fine because it carried its own `case`
statement naming all four facts.

So the table is one object, and these tests read it the way every consumer
does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kalshi_router.automerge import (
    ledger_branch_for,
    mergeable_paths_for,
    router_branch_for,
)
from kalshi_router.delivery_branch import uncommittable
from kalshi_router.destination import DESTINATION_REPOS, destination_repo_for
from kalshi_router.destinations import (
    PROFILES,
    UnknownDestinationError,
    describe,
    profile_for,
    render_command,
)
from kalshi_router.production import ROW_BUILDERS
from kalshi_router.sports import Sport
from kalshi_router.wager import SPORTS_WITH_AN_IMPORTER

ROOT = Path(__file__).resolve().parents[1]
PROFILE_SCRIPT = ROOT / "scripts/destination_profile.py"


def run_profile(*args):
    return subprocess.run(
        [sys.executable, str(PROFILE_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ------------------------------------------------------- what is routed


def test_cfb_is_activated_in_production():
    """The activation itself, asserted rather than implied by absence."""
    assert Sport.CFB in PROFILES
    assert destination_repo_for("CFB") == "chmoses98/cfb-edge-finder"
    assert DESTINATION_REPOS[Sport.CFB] == "chmoses98/cfb-edge-finder"


def test_a_sport_with_no_profile_is_refused_not_defaulted():
    for sport in ("TENNIS", "CRICKET", ""):
        with pytest.raises(UnknownDestinationError):
            profile_for(sport)
        assert destination_repo_for(sport) is None


def test_nfl_is_activated_with_its_ledger_on_handicap_data_and_its_importer_on_main():
    """NFL's vocabulary existed from the backfill onward, and for a week and a
    half it was not routed -- during which every NFL order the owner placed was
    refused as "no destination importer" and counted as correct behaviour."""
    assert "NFL" in ROW_BUILDERS and Sport.NFL in PROFILES
    nfl = profile_for("NFL")
    assert (nfl.repo, nfl.ledger_branch, nfl.code_branch) == (
        "chmoses98/nfl-edge-finder", "handicap-data", "main")
    wager = " ".join(nfl.wager_importer)
    assert "{code}/scripts/handicap/import_routed_wagers.py" in wager
    # the week comes from the REAL schedule, fetched by the destination
    assert "--allow-schedule-download" in nfl.wager_importer
    assert "--receipts-out" in nfl.wager_importer
    assert "--receipts-out" in nfl.settlement_importer
    assert not nfl.requires_season
    # handicap-data has no .github/, so its own validator supplies the verdict
    assert nfl.ledger_branch_runs_ci is False and nfl.ledger_validator is not None
    assert "validate_routed_ledger.py" in " ".join(nfl.ledger_validator)


def test_nfls_mergeable_files_are_exactly_the_minted_record_shapes():
    from kalshi_router.automerge import is_mergeable_path, ledger_pathspecs_for

    ok = ("data/imported_wagers/2026/week_02/routed-0123456789abcdef01234567.json",
          "data/wager_settlements/2026/week_02/stl-0123456789abcdef01234567.json")
    bad = ("data/imported_wagers/2026/week_02/manual-1.json",
           "data/imported_wagers/2026/week_02/routed-0123.json",
           "data/imported_wagers/2026/routed-0123456789abcdef01234567.json",
           "data/recommendations/2026/week_02/x.json",
           "README.md",
           "data/imported_wagers/2026/week_02/routed-0123456789abcdef01234567.json.bak")
    for path in ok:
        assert is_mergeable_path("NFL", path), path
    for path in bad:
        assert not is_mergeable_path("NFL", path), path
    assert ledger_pathspecs_for("NFL") == ("data/imported_wagers/", "data/wager_settlements/",
                                           "data/wager_settlement_amendments/")
    assert is_mergeable_path(
        "NFL", "data/wager_settlement_amendments/2026/week_02/amd-0123456789abcdef01234567.json")
    assert not is_mergeable_path("NFL", "data/wager_settlement_amendments/2026/week_02/amd-x.json")
    assert ledger_pathspecs_for("MLB") == ("data/edgelab/bets/bets.jsonl",)
    assert not is_mergeable_path("TENNIS", ok[0])


def test_every_routed_sport_has_a_row_builder():
    """The reverse direction, and the one that would land a wager with no
    price: a destination declared routable with no way to speak its ledger's
    vocabulary."""
    for sport in PROFILES:
        assert sport.value in ROW_BUILDERS, f"{sport.value} is routed with no row shape"


def test_the_shadow_path_and_production_agree_on_what_is_routable():
    assert SPORTS_WITH_AN_IMPORTER == frozenset(PROFILES)


# ------------------------------------------------- the per-destination facts


def test_cfbs_ledger_is_on_its_data_branch_and_its_importer_is_on_main():
    """The fact the old, flat destination map could not express -- and the
    reason production could not route CFB at all."""
    profile = profile_for("CFB")
    assert profile.ledger_branch == "accounting-data"
    assert profile.code_branch == "main"
    assert ledger_branch_for("CFB") == "accounting-data"
    assert ledger_branch_for("MLB") == "main"


def test_each_destination_runs_its_own_importer():
    mlb = " ".join(profile_for("MLB").wager_importer)
    cfb = " ".join(profile_for("CFB").wager_importer)
    assert "import_bet_batch.py" in mlb
    assert "import_routed_wagers.py" in cfb
    assert mlb != cfb


def test_cfb_requires_a_season_and_mlb_does_not():
    """A College Football season spans two calendar years, so the ledger file
    is named rather than inferred."""
    assert profile_for("CFB").requires_season
    assert "--season" in profile_for("CFB").wager_importer
    assert not profile_for("MLB").requires_season


def test_mlb_has_no_settlement_importer_and_says_why():
    """A settlement pushed to a destination that settles its own bets would be
    a second authority on one fact."""
    assert profile_for("MLB").settlement_importer is None
    assert describe("MLB")["settles_its_own"] is True
    assert any("settles its own" in note for note in profile_for("MLB").notes)


def test_cfb_has_a_settlement_importer():
    assert profile_for("CFB").settlement_importer is not None
    assert describe("CFB")["settles_its_own"] is False


def test_the_committable_prefixes_are_the_destinations_own():
    assert profile_for("MLB").committable_prefixes == ("data/",)
    assert profile_for("CFB").committable_prefixes == ("wagers/", "settlements/")


def test_containment_uses_those_prefixes():
    """A `data/` containment rule would refuse every row CFB writes."""
    staged = ["wagers/2026.jsonl", "settlements/2026.jsonl", "__pycache__/x.pyc"]
    assert uncommittable(staged, profile_for("CFB").committable_prefixes) == (
        "__pycache__/x.pyc",
    )
    assert uncommittable(staged, profile_for("MLB").committable_prefixes) == (
        "__pycache__/x.pyc",
        "settlements/2026.jsonl",
        "wagers/2026.jsonl",
    )


def test_containment_with_no_prefixes_is_refused_rather_than_permissive():
    """The one answer a containment check must never give is "everything"."""
    with pytest.raises(ValueError, match="permit every path"):
        uncommittable(["anything"], ())


def test_the_mergeable_paths_are_exact_and_per_destination():
    assert mergeable_paths_for("MLB") == {"data/edgelab/bets/bets.jsonl"}
    cfb = mergeable_paths_for("CFB")
    assert "wagers/2026.jsonl" in cfb
    assert "settlements/2026.jsonl" in cfb
    # Exact paths, never a prefix: a file the importer starts writing later is
    # not automatically safe to land unread.
    assert all(path.endswith(".jsonl") for path in cfb)


def test_an_unknown_sports_mergeable_set_is_empty_so_the_gate_refuses():
    """Empty makes every changed file unexpected, which is the correct answer
    for a destination nobody has described."""
    assert mergeable_paths_for("TENNIS") == frozenset()


def test_every_destination_gets_a_router_owned_branch_name():
    for sport in PROFILES:
        assert router_branch_for(sport.value).startswith("kalshi-router/")


# --------------------------------------------------------- the renderer


def test_a_rendered_command_fills_every_placeholder():
    argv = render_command(
        profile_for("CFB").wager_importer,
        payload="/t/p.json",
        work="/t/work",
        code="/t/code",
        receipts="/t/r.json",
        season=2026,
    )
    assert "/t/code/scripts/import_routed_wagers.py" in argv
    assert "2026" in argv
    assert not any("{" in part for part in argv)


def test_a_missing_season_is_refused_before_anything_is_fetched():
    """A `{season}` that reached a real command line as an empty string would
    be rejected by the importer's own `type=int` -- eventually, after a clone
    and an import. Refusing here names the missing value instead."""
    with pytest.raises(UnknownDestinationError, match="needs a value"):
        render_command(
            profile_for("CFB").wager_importer,
            payload="/t/p.json",
            work="/t/work",
            code="/t/code",
            receipts="/t/r.json",
            season=None,
        )


# ------------------------------------------------ the shell's own reader


def test_the_profile_script_lists_exactly_what_is_routed():
    result = run_profile("--list")
    assert result.returncode == 0
    assert sorted(result.stdout.split()) == sorted(s.value for s in PROFILES)


def test_the_profile_script_refuses_an_unknown_destination():
    result = run_profile("TENNIS")
    assert result.returncode == 2
    assert not result.stdout.strip()
    assert "refuses rather than defaulting" in result.stderr


@pytest.mark.parametrize("sport", sorted(s.value for s in PROFILES))
def test_the_profile_script_prints_the_same_facts_the_library_holds(sport):
    result = run_profile(sport)
    assert result.returncode == 0
    assert json.loads(result.stdout) == describe(sport)


def test_the_profile_script_prints_a_list_field_one_per_line():
    """A shell reads this with `mapfile`; a space-joined string would break the
    first time a path had a space in it."""
    result = run_profile("CFB", "--field", "committable_prefixes")
    assert result.stdout.splitlines() == ["wagers/", "settlements/"]


def test_the_profile_script_prints_booleans_as_shell_words():
    assert run_profile("CFB", "--field", "requires_season").stdout.strip() == "true"
    assert run_profile("MLB", "--field", "requires_season").stdout.strip() == "false"


def test_the_profile_script_renders_an_importer_one_argument_per_line():
    result = run_profile(
        "CFB", "--importer", "wager",
        "--payload", "/t/p.json", "--work", "/t/w", "--code", "/t/c",
        "--receipts", "/t/r.json", "--season", "2026",
    )
    assert result.returncode == 0
    argv = result.stdout.splitlines()
    assert argv[0] == "python"
    assert "/t/c/scripts/import_routed_wagers.py" in argv
    assert "2026" in argv


def test_the_profile_script_refuses_a_settlement_importer_that_does_not_exist():
    result = run_profile("MLB", "--importer", "settlement")
    assert result.returncode == 2
    assert "settles its own bets" in result.stderr


def test_the_profile_script_never_opens_a_payload():
    """It takes a payload PATH, to put in a command it prints. It never reads
    the file, so there is nothing in it that could reach a public log --
    asserted structurally rather than argued.

    Scanned over the CODE only: the script's own prose explains which words it
    must never print, so a scan that read the docstring would fail on the
    explanation."""
    source = PROFILE_SCRIPT.read_text()
    assert "--payload" in source  # it takes the path...
    executable = "".join(source.split('"""')[::2])
    executable = "\n".join(
        line for line in executable.splitlines() if not line.strip().startswith("#")
    )
    for banned in ("json.load", "open(", "read_text", "readlines"):
        assert banned not in executable, f"the profile script reads a file ({banned})"


# ------------------------------------------------------ the observation period
#
# CFB has never completed a real delivery. The end-to-end path is proven by a
# dry run and by tests driving the committed bash, which is not the same as
# having watched it land. So the first real batches are delivered and left for
# a person.


def test_cfb_starts_held_for_observation():
    assert profile_for("CFB").auto_merge is False


def test_mlb_keeps_merging():
    """Proven in production over many deliveries; nothing here changes it."""
    assert profile_for("MLB").auto_merge is True


def test_holding_the_merge_is_the_only_thing_withheld():
    """An observation period must not be a weaker gate. Everything that makes
    a CFB batch verifiable stays exactly as it was."""
    cfb = profile_for("CFB")
    assert cfb.ledger_validator, "the validator still runs"
    assert cfb.committable_prefixes, "delivery is still confined to the ledger paths"
    assert cfb.mergeable_paths, "the diff is still constrained"
    assert cfb.row_identity_field, "rows are still identified"


def test_auto_merge_defaults_to_on_for_a_new_destination():
    """A future destination is not silently held; holding is a stated choice."""
    import dataclasses

    from kalshi_router.destinations import DestinationProfile

    field = next(f for f in dataclasses.fields(DestinationProfile) if f.name == "auto_merge")
    assert field.default is True



def test_nfl_receives_v2_settlement_economics_and_cfb_stays_v1_until_it_can_amend():
    assert profile_for("NFL").settlement_economics == "router-settlement-economics.v2"
    assert profile_for("CFB").settlement_economics == "router-settlement-economics.v1"
