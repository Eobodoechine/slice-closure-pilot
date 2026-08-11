"""Negative cases for the slice-closure gate: verdicts the gate MUST refuse to give.

Why this file exists
--------------------
The gate's existing coverage asserted verdicts the gate *does* produce. Every case
was a positive one, so a gate that could never fail would have passed the whole
suite. The highest-value missing case is the hollow slice: a commit that mentions
the pinned symbol but defines nothing, with a green test suite.

That is not hypothetical. Branch `pilot/t3-hollow` was written to be blocked, and
the gate passed it. Its only line containing the pinned string was its own
docstring, stating that the file "deliberately does NOT contain the
contract-pinned symbol `def validate_pilot_input`". The disclaimer satisfied the
check it was disclaiming.

FIXTURE RULE (learned from exactly that): a hollow fixture's own text -- including
comments, docstrings, and the commit message -- must be written so it does not
accidentally satisfy the assertion under test. `_SYMBOL` is assembled at runtime
below so this module's source never literally contains the pinned string.
"""
import json
import os
import subprocess
import sys

import pytest

def _locate_gate():
    """reality_gate.py sits beside this file in the loop harness, and one level up
    under gates/ in the pilot repo. Resolve both so the three copies of this file
    can stay byte-identical."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "reality_gate.py"),
        os.path.join(os.path.dirname(here), "gates", "reality_gate.py"),
    ):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("reality_gate.py not found relative to %s" % here)


GATE = _locate_gate()

# Assembled, never written literally -- see FIXTURE RULE above.
_SYMBOL = "validate_pilot" + "_input"
_DEF_SUBSTRING = "def " + _SYMBOL

PASSING_SUITE = "python3 -m unittest discover -q"


def _git(repo, *args, when=None):
    """`when` pins author+committer date. `git merge-base` tie-breaks between
    multiple bases by date, so a criss-cross fixture built with wall-clock times
    picks a different base depending on how fast the suite runs -- which made
    one test pass alone and fail in a full run."""
    env = None
    if when is not None:
        env = dict(os.environ,
                   GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=env)


def _init_repo(tmp_path):
    """A repo whose test suite genuinely passes and that defines nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "test_baseline.py").write_text(
        "import unittest\n\n\n"
        "class BaselineTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, path, body, message):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _head(repo)


def _run_gate(*args):
    r = subprocess.run([sys.executable, GATE, *args],
                       capture_output=True, text=True)
    return r.returncode, json.loads(r.stdout)


def test_update_branch_merge_commit_uses_pr_range_for_substring(tmp_path):
    """An update-branch merge must retain feature-side text evidence.

    ``git show`` on the merge omits the marker under combined-diff semantics;
    the merge-base-to-head PR delta contains it. --base-ref must select that
    latter truth.
    """
    repo = _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    _git(repo, "switch", "-c", "feature")
    _commit(repo, "feature.txt", "PR_RANGE_MARKER\n", "feature marker")

    _git(repo, "switch", base_branch)
    base_tip = _commit(repo, "base.txt", "base advanced\n", "advance base")

    _git(repo, "switch", "feature")
    _git(repo, "merge", "--no-edit", base_branch)
    merge_head = _head(repo)

    tip_patch = subprocess.run(
        ["git", "-C", str(repo), "show", "--format=", merge_head],
        check=True, capture_output=True, text=True,
    ).stdout
    range_patch = subprocess.run(
        ["git", "-C", str(repo), "diff", base_tip, merge_head, "--"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "+PR_RANGE_MARKER" not in tip_patch
    assert "+PR_RANGE_MARKER" in range_patch

    rc, out = _run_gate(
        "check", "--repo", str(repo), "--commit", merge_head,
        "--base-ref", base_tip,
        "--expect-substring", "PR_RANGE_MARKER",
    )
    assert rc == 0, out
    assert out["checks"]["substring-present"] is True


# ---------------------------------------------------------------------------
# The hollow slice, in the two shapes that actually occurred
# ---------------------------------------------------------------------------

HOLLOW_SHAPES = {
    # A stub that names what it intends to add but adds nothing.
    "comment": "# TODO: %s -- not implemented yet\n" % _DEF_SUBSTRING,
    # The literal t3-hollow shape: a docstring DISCLAIMING the symbol.
    "docstring": '"""This module deliberately does NOT contain `%s`."""\n'
                 % _DEF_SUBSTRING,
    # The symbol as inert data rather than code.
    "string_literal": 'INTENDED_API = "%s"\n' % _DEF_SUBSTRING,
}


@pytest.mark.parametrize("shape", sorted(HOLLOW_SHAPES))
def test_hollow_slice_passes_substring_check_documenting_the_hole(shape, tmp_path):
    """CHARACTERIZATION, not an endorsement: --expect-substring green-lights all
    three hollow shapes. This is the defect --expect-definition exists to close;
    it is pinned here so that if anyone ever hardens the substring path, this
    test fails loudly and forces the docs to be updated with it."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", HOLLOW_SHAPES[shape], "hollow slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-substring", _DEF_SUBSTRING,
                          "--test-cmd", PASSING_SUITE)

    assert out["checks"]["substring-present"] is True
    assert out["passed"] is True and code == 0


@pytest.mark.parametrize("shape", sorted(HOLLOW_SHAPES))
def test_hollow_slice_is_blocked_by_definition_check(shape, tmp_path):
    """THE POINT OF THIS FILE. Same commits, same passing suite -- the gate must
    refuse, because nothing was defined."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", HOLLOW_SHAPES[shape], "hollow slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _SYMBOL,
                          "--test-cmd", PASSING_SUITE)

    assert out["checks"]["definition-present"] is False
    assert "definition-absent" in out["reasons"]
    assert out["passed"] is False and code == 1


@pytest.mark.parametrize("shape", sorted(HOLLOW_SHAPES))
def test_expect_file_does_not_rescue_the_substring_check(shape, tmp_path):
    """--expect-file is NOT a mitigation for this. It narrows which text is
    searched, then still does a raw `in` -- so a comment in the pinned file
    satisfies it exactly as easily."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", HOLLOW_SHAPES[shape], "hollow slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-substring", _DEF_SUBSTRING,
                          "--expect-file", "pilot_app.py",
                          "--test-cmd", PASSING_SUITE)

    assert out["checks"]["substring-present"] is True
    assert out["passed"] is True and code == 0


# ---------------------------------------------------------------------------
# The definition check must still pass real work (no false NEGATIVES)
# ---------------------------------------------------------------------------

REAL_DEFINITIONS = {
    "plain": "def %s(value):\n    return bool(value)\n" % _SYMBOL,
    "async": "async def %s(value):\n    return bool(value)\n" % _SYMBOL,
    "class": "class %s:\n    pass\n" % _SYMBOL,
    "with_decorator": "import functools\n\n\n@functools.cache\n"
                      "def %s(value):\n    return bool(value)\n" % _SYMBOL,
}

# Positions that LOOK like a definition but never bind an importable module
# attribute. An earlier version of this check used ast.walk(), which counted all
# of them; it now reads tree.body only. Each of these was a live bypass.
NOT_MODULE_LEVEL = {
    # A method is not the module-level symbol the contract names. This is a SPEC
    # DECISION, recorded explicitly: pin the class name if the class is the
    # deliverable. An earlier revision of this suite asserted the opposite.
    "method": "class Validator:\n    def %s(self, value):\n"
              "        return bool(value)\n" % _SYMBOL,
    "if_false": "if False:\n    def %s(value):\n        return value\n" % _SYMBOL,
    "nested": "def _outer():\n    def %s(value):\n        return value\n" % _SYMBOL,
    "type_checking": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
                     "    def %s(value):\n        return value\n" % _SYMBOL,
}


@pytest.mark.parametrize("shape", sorted(REAL_DEFINITIONS))
def test_real_definition_passes(shape, tmp_path):
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", REAL_DEFINITIONS[shape], "real slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _SYMBOL,
                          "--test-cmd", PASSING_SUITE)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


def test_definition_found_in_any_touched_py_file_without_expect_file(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "helpers.py", REAL_DEFINITIONS["plain"], "real slice")

    _, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                       "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True


def test_expect_file_scopes_the_definition_search(tmp_path):
    """Defined, but not in the pinned file -> blocked. This is what --expect-file
    is genuinely good for once the check is AST-based."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "helpers.py", REAL_DEFINITIONS["plain"], "real slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _SYMBOL,
                          "--expect-file", "pilot_app.py")

    assert out["checks"]["definition-present"] is False
    assert out["passed"] is False and code == 1


# ---------------------------------------------------------------------------
# Fail-closed behaviours
# ---------------------------------------------------------------------------

def test_non_python_file_fails_closed_rather_than_guessing(tmp_path):
    """No fuzzy fallback for languages the parser does not cover -- fuzziness is
    the defect being closed, so an unsupported language must refuse, not guess."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "app.js", "function %s(v) { return !!v; }\n" % _SYMBOL,
                  "js slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-absent" in out["reasons"]
    assert code == 1


def test_unparseable_python_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", "def %s(  <<<syntax error\n" % _SYMBOL,
                  "broken slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-unparseable" in out["reasons"]
    assert code == 1


def test_expect_definition_rejects_a_def_prefixed_value(tmp_path):
    """Guards the migration footgun: copying the old contract's
    'def <name>' value straight into expect_definition must be a usage error,
    not a silently-never-matching check."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "real slice")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _DEF_SUBSTRING)

    assert code == 2
    assert out["error"] == "usage_error"


def test_expect_definition_alone_satisfies_the_verify_binding_requirement(tmp_path):
    """verify must accept --expect-definition as a binding check on its own,
    exactly as it accepts --expect-substring."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "real slice")
    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "product": "p", "done_sentence": "d", "updated": None,
        "items": [{"id": "SLICE-PILOT-001", "title": "t",
                   "status": "open", "verified": False}],
    }))

    code, out = _run_gate("verify", "--status-json", str(status),
                          "--item", "SLICE-PILOT-001",
                          "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _SYMBOL)

    assert code == 0 and out["passed"] is True
    assert json.loads(status.read_text())["items"][0]["verified"] is True


def test_hollow_slice_never_writes_verified(tmp_path):
    """The write path, not just the verdict: a hollow slice must leave
    verified:false on disk."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", HOLLOW_SHAPES["docstring"], "hollow slice")
    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "product": "p", "done_sentence": "d", "updated": None,
        "items": [{"id": "SLICE-PILOT-001", "title": "t",
                   "status": "open", "verified": False}],
    }))

    code, out = _run_gate("verify", "--status-json", str(status),
                          "--item", "SLICE-PILOT-001",
                          "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _SYMBOL,
                          "--test-cmd", PASSING_SUITE)

    assert code == 1 and out["passed"] is False
    item = json.loads(status.read_text())["items"][0]
    assert item["verified"] is False
    assert item.get("status") != "fixed"


# ---------------------------------------------------------------------------
# Bypasses found by an independent verifier pass. Each of these was PASSING
# against the first version of --expect-definition. They are the reason the
# check now reads module-level bindings only AND requires the change under test
# to have introduced the symbol.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", sorted(NOT_MODULE_LEVEL))
def test_non_module_level_definitions_are_blocked(shape, tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "pilot_app.py", NOT_MODULE_LEVEL[shape], "looks defined")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--base-ref", base, "--expect-definition", _SYMBOL,
                          "--test-cmd", PASSING_SUITE)

    assert out["checks"]["definition-present"] is False
    assert out["passed"] is False and code == 1


def test_touching_a_file_that_already_defines_it_cannot_claim_it(tmp_path):
    """THE severest bypass of the first revision: a commit adding ZERO
    definitions verified green just by touching a file that already defined the
    symbol -- verbatim the failure this check exists to prevent."""
    repo = _init_repo(tmp_path)
    _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "the slice lands")
    base = _head(repo)
    sha = _commit(repo, "pilot_app.py",
                  REAL_DEFINITIONS["plain"] + "\n# harmless trailing comment\n",
                  "touch it, define nothing")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--base-ref", base, "--expect-definition", _SYMBOL,
                          "--test-cmd", PASSING_SUITE)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert out["passed"] is False and code == 1


def test_definition_only_in_a_test_file_is_not_a_landed_capability(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "tests/test_slice.py", REAL_DEFINITIONS["plain"],
                  "define it in a test")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert out["passed"] is False and code == 1


def test_expect_file_cannot_make_the_check_commit_independent(tmp_path):
    """An earlier revision needed a special "was this path touched?" guard here,
    because pinning expect_file in a repo already defining the symbol made the
    check permanently true. Comparing against the merge base removes the need for
    that guard entirely: pre-existing is pre-existing, whatever the commit did."""
    repo = _init_repo(tmp_path)
    _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "the slice lands")
    base = _head(repo)
    sha = _commit(repo, "notes.md", "unrelated docs\n", "docs only")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--base-ref", base, "--expect-definition", _SYMBOL,
                          "--expect-file", "pilot_app.py")

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_non_ascii_filename_is_not_invisible(tmp_path):
    """git C-quotes non-ASCII paths by default ("src/caf\\303\\251.py"), which
    then fails a .py suffix test and silently drops the file. A real definition
    there must still count."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "café.py", REAL_DEFINITIONS["plain"], "unicode path")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


def test_base_ref_spans_the_whole_range_not_just_the_tip(tmp_path):
    """A multi-commit PR that lands the symbol in an earlier commit and tips
    with a docs tweak must still pass -- the gate judges base..head."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "the slice lands")
    tip = _commit(repo, "notes.md", "changelog\n", "docs tweak on top")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", tip,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


def test_without_base_ref_the_parent_commit_is_the_baseline(tmp_path):
    """Documents what omitting --base-ref actually does, so CI is never tempted
    to drop it.

    This replaces a test that asserted a semantic which no longer exists ("the
    tip commit alone is diffed"). There is no path diffing now, and its
    assertions -- definition-present False, exit 1 -- were satisfied by the
    symbol being pre-existing, not by the behaviour it claimed to pin. It would
    have stayed green whatever happened to that behaviour. Assert the reason
    token, which is what actually discriminates.
    """
    repo = _init_repo(tmp_path)
    # Symbol lands, then a later commit touches something else entirely.
    _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "the slice lands")
    tip = _commit(repo, "notes.md", "changelog\n", "unrelated commit on top")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", tip,
                          "--expect-definition", _SYMBOL)

    # Baseline is tip^, where the symbol already exists -> pre-existing, and the
    # token proves that is WHY, not merely that it blocked.
    assert "definition-preexisting" in out["reasons"]
    assert out["checks"]["definition-present"] is False
    assert code == 1


def test_decorator_rebinding_is_a_known_limit_not_a_silent_pass(tmp_path):
    """CHARACTERIZATION of an acknowledged limit. `@_kill def foo` leaves
    foo is None at runtime, but statically a module-level def exists, so the
    check passes. Only executing the module could prove otherwise. Pinned so the
    limit stays visible and documented rather than being discovered as a
    surprise."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    body = ("def _kill(f):\n    return None\n\n\n@_kill\n"
            "def %s(value):\n    return value\n" % _SYMBOL)
    sha = _commit(repo, "pilot_app.py", body, "decorated to None")

    _, out = _run_gate("check", "--repo", str(repo), "--commit", sha,
                       "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True


def test_verify_also_rejects_a_def_prefixed_value(tmp_path):
    """cmd_verify has its own copy of the isidentifier guard. A mutation pass
    found cmd_check's copy covered and this one not."""
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "real slice")
    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "product": "p", "done_sentence": "d", "updated": None,
        "items": [{"id": "SLICE-PILOT-001", "title": "t",
                   "status": "open", "verified": False}],
    }))

    code, out = _run_gate("verify", "--status-json", str(status),
                          "--item", "SLICE-PILOT-001",
                          "--repo", str(repo), "--commit", sha,
                          "--expect-definition", _DEF_SUBSTRING)

    assert code == 2 and out["error"] == "usage_error"
    assert json.loads(status.read_text())["items"][0]["verified"] is False


# ---------------------------------------------------------------------------
# Round-2 verifier findings. The per-path range-diff approach these came from is
# gone; the check now compares whole-tree presence at the MERGE BASE vs head.
# ---------------------------------------------------------------------------

def test_stale_branch_plus_rename_on_base_cannot_claim_the_symbol(tmp_path):
    """S1, the worst of them: reachable in ordinary CI with no adversary. An
    unrebased branch, plus a routine rename on the base, made the branch's stale
    copy of the defining file look newly added -- so a PR that defines nothing
    verified green. Two-dot base..head is a tree-to-tree diff; what a PR proposes
    is the merge base."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "symbol already exists")
    fork = _head(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    head = _commit(repo, "notes.py", "notes = 1\n", "PR that defines nothing")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "mv", "src/real.py", "src/renamed.py")
    _git(repo, "commit", "-qm", "base renames the definer")
    base = _head(repo)
    assert fork != base

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_a_pure_rename_does_not_land_the_symbol(tmp_path):
    """S2: a commit with literally 0 insertions and 0 deletions scored as having
    introduced the symbol, because `before` was looked up at the same path -- and
    that path did not exist at the base."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "symbol already exists")
    base = _head(repo)
    _git(repo, "mv", "src/real.py", "src/renamed.py")
    _git(repo, "commit", "-qm", "pure rename")
    head = _head(repo)

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_unparseable_base_blob_fails_closed_not_open(tmp_path):
    """S3: an unreadable base was treated as "the file did not exist, so this is
    new". A commit that only removed a stray conflict marker could claim the
    symbol. "I could not determine it" must never read as "it is new"."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/b.py",
            REAL_DEFINITIONS["plain"] + "<<<<<<< HEAD\n", "base has a marker")
    base = _head(repo)
    head = _commit(repo, "src/b.py", REAL_DEFINITIONS["plain"], "remove the marker")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-unparseable" in out["reasons"]
    assert code == 1


@pytest.mark.parametrize("bad", ["", "   ", "--output=/tmp/should_not_exist"])
def test_malformed_base_ref_is_rejected_not_silently_ignored(bad, tmp_path):
    """S4/S5: an empty --base-ref fell through to a HEAD-relative diff and an
    index read, and failed OPEN. A leading-dash value was parsed by git as an
    option -- `--output=` really did create a file."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "real slice")
    assert base

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", bad, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-bad-base-ref" in out["reasons"]
    assert code == 1
    assert not os.path.exists("/tmp/should_not_exist")


def test_module_level_del_unbinds_the_symbol(tmp_path):
    """Attack 12. Previously passed while the README claimed it was blocked --
    a documentation claim the code contradicted. A module-level ast.Delete is
    decidable in the same pass, so now it genuinely is blocked."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, "pilot_app.py",
                   REAL_DEFINITIONS["plain"] + "del %s\n" % _SYMBOL,
                   "define then delete")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert code == 1


NON_PRODUCTION_PATHS = [
    "tests/test_slice.py",      # directory branch
    "__tests__/thing.py",       # directory branch, alternate name
    "docs/example.py",          # docs
    "examples/demo.py",         # examples
    "test_slice.py",            # filename branch, prefix
    "slice_test.py",            # filename branch, suffix
    "conftest.py",              # pytest fixture module
]


@pytest.mark.parametrize("path", NON_PRODUCTION_PATHS)
def test_definitions_outside_production_code_do_not_count(path, tmp_path):
    """Each path is listed separately on purpose: a single fixture under
    tests/test_slice.py satisfies BOTH the directory and the filename branch, so
    either half could be deleted with the suite still green. A mutation pass
    found exactly that."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, path, REAL_DEFINITIONS["plain"], "define it off to the side")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False, path
    assert code == 1


@pytest.mark.parametrize("path", ["src/testing/factories.py",
                                  "mypkg/testing/fakes.py",
                                  "src/testkit.py"])
def test_shipped_packages_named_testing_are_production_code(path, tmp_path):
    """Regression against a false negative this check introduced: excluding any
    path segment named `test`/`testing` also excluded shipped packages
    (django.test, pytest plugins, mypkg/testing/) -- real, legitimate layouts.

    Note the boundary: `src/test_helpers.py` is NOT here, and is correctly
    excluded. `test_*.py` is pytest's own default collection pattern, so such a
    module really would be collected as a test. The directory segment was the
    over-reach; the filename pattern is not."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, path, REAL_DEFINITIONS["plain"], "shipped testing helper")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True, path
    assert out["passed"] is True and code == 0


@pytest.mark.parametrize("use_base_ref", [True, False])
def test_a_plain_new_definition_is_found(use_base_ref, tmp_path):
    """The control. Deliberately parameterised over both code paths.

    This is the test that catches a whole-check outage. The candidate pre-filter
    is a `git grep -E` regex, and POSIX ERE does NOT support `\\b` -- a version
    using it matched nothing and exited 1, which is indistinguishable from "no
    candidates", so the gate silently blocked EVERYTHING. Every adversarial case
    still "passed" because they all expect a block. Only a positive control
    separates "correctly refusing" from "broken and refusing everything".
    """
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "real slice")

    args = ["check", "--repo", str(repo), "--commit", head,
            "--expect-definition", _SYMBOL]
    if use_base_ref:
        args += ["--base-ref", base]

    code, out = _run_gate(*args)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


@pytest.mark.parametrize("use_base_ref", [True, False])
def test_non_ascii_filename_found_on_both_code_paths(use_base_ref, tmp_path):
    """The quotePath fix had coverage only on the --base-ref path; a mutation
    pass reverted the other branch with the suite still green."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, "café.py", REAL_DEFINITIONS["plain"], "unicode path")

    args = ["check", "--repo", str(repo), "--commit", head,
            "--expect-definition", _SYMBOL]
    if use_base_ref:
        args += ["--base-ref", base]

    code, out = _run_gate(*args)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


def test_merge_base_not_base_tip(tmp_path):
    """Pins the merge-base fix specifically. A mutation pass caught that the
    stale-branch test above passes either way -- whole-tree comparison already
    blocks that fixture regardless of which base is used, so it never actually
    exercised merge-base.

    The discriminating case: the base tip DELETES the definer. Comparing against
    the tip then says "absent at base", so a stale branch still carrying the old
    copy looks like it introduced the symbol and verifies green. The merge base
    (the fork point) still has it, which is the truth -- this branch wrote
    nothing.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "fork point has it")
    _git(repo, "checkout", "-q", "-b", "feature")
    head = _commit(repo, "notes.py", "notes = 1\n", "PR defines nothing")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "rm", "-q", "src/real.py")
    _git(repo, "commit", "-qm", "base tip deletes the definer")
    tip = _head(repo)

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", tip, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


# ---------------------------------------------------------------------------
# Round-3 verifier findings. The candidate scan is now enumerated from the TREE
# (ls-tree + cat-file), never `git grep`, so nothing in the working tree can
# influence what is scanned at the base.
# ---------------------------------------------------------------------------

def test_pr_supplied_gitattributes_cannot_hide_the_base(tmp_path):
    """N1, the worst finding of round 3: attacker-controlled and end-to-end.

    `git grep <tree-ish>` reads .gitattributes from the WORKING TREE, which in
    CI is the PR head. A PR shipping two lines marking the base's definer
    `binary` made `-I` drop it from the base scan, so an already-landed symbol
    looked newly introduced -- and it survived the protected-path step, since
    .gitattributes is not a protected path.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "symbol already landed")
    base = _head(repo)
    (repo / ".gitattributes").write_text("src/real.py binary\n")
    head = _commit(repo, "src/reclaim.py", REAL_DEFINITIONS["plain"],
                   "re-declare it, with attributes hiding the base")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_symbol_at_any_merge_base_counts_as_preexisting(tmp_path):
    """N2: a criss-cross history has more than one merge base, and
    `git merge-base` without -a returns an arbitrary one. Picking the base that
    happens not to define the symbol made an already-landed function look new.

    Dates are pinned: merge-base tie-breaks by date, so an unpinned fixture
    picks a different base depending on how fast the suite runs.
    """
    repo = _init_repo(tmp_path)
    root = _head(repo)
    _git(repo, "checkout", "-q", "-b", "A")
    (repo / "src_a.py").write_text(REAL_DEFINITIONS["plain"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "A defines it", when="2001-01-01T00:00:01")
    a1 = _head(repo)
    _git(repo, "checkout", "-q", "-b", "B", root)
    (repo / "b.py").write_text("b = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "B does not", when="2001-01-01T00:00:02")
    b1 = _head(repo)
    _git(repo, "checkout", "-q", "A")
    _git(repo, "merge", "-q", "--no-edit", b1, when="2001-01-01T00:00:03")
    a2 = _head(repo)
    _git(repo, "checkout", "-q", "B")
    _git(repo, "merge", "-q", "--no-edit", a1, when="2001-01-01T00:00:04")
    (repo / "redeclare.py").write_text(REAL_DEFINITIONS["plain"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "re-declare", when="2001-01-01T00:00:05")
    head = _head(repo)

    bases = subprocess.run(["git", "-C", str(repo), "merge-base", "-a", a2, head],
                           capture_output=True, text=True).stdout.split()
    assert len(bases) > 1, "fixture must produce a criss-cross"

    # The fixture must DISCRIMINATE: plain `merge-base` has to return a base
    # that does NOT define the symbol, or consulting only the first base would
    # block anyway and this test would pin the loop rather than the -a flag.
    # A mutation pass caught exactly that.
    plain = subprocess.run(["git", "-C", str(repo), "merge-base", a2, head],
                           capture_output=True, text=True).stdout.strip()
    plain_has = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", "%s:src_a.py" % plain],
        capture_output=True).returncode == 0
    assert not plain_has, "plain merge-base must return the NON-defining base"
    assert a1 and b1

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", a2, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_backslash_joined_definition_at_base_is_seen(tmp_path):
    """N3: the old regex pre-filter missed `def \\` + newline + name, which the
    AST accepts. Missing it at the BASE is a fail-open -- an unseen base
    definition reads as newly introduced."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py",
            "def \\\n %s(value):\n    return value\n" % _SYMBOL,
            "base defines it, backslash-joined")
    base = _head(repo)
    head = _commit(repo, "src/copy.py", REAL_DEFINITIONS["plain"], "copy-paste it")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_shallow_repo_names_its_own_cause(tmp_path):
    """N4: an availability failure that presents as "every slice is hollow".
    The workflow's own `git fetch --depth=1` shallowed the checkout, after which
    merge-base has no ancestry. It must say so rather than report a generic
    unreadable."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "landed")
    missing_base = _head(repo)
    _commit(repo, "later.py", "later = 1\n", "later")

    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=1", "file://%s" % repo,
                    str(shallow)], capture_output=True, text=True)
    assert subprocess.run(["git", "-C", str(shallow), "rev-parse",
                           "--is-shallow-repository"], capture_output=True,
                          text=True).stdout.strip() == "true"

    code, out = _run_gate("check", "--repo", str(shallow), "--commit", "HEAD",
                          "--base-ref", missing_base,
                          "--expect-definition", _SYMBOL)

    assert "definition-shallow-repo" in out["reasons"]
    assert out["passed"] is False and code == 1


def test_expect_file_does_not_scope_the_base_lookup(tmp_path):
    """Surviving mutant 3. The existing expect_file test had the symbol at base
    INSIDE the pinned path, so scoping the base by expect_file was invisible to
    it. Discriminating case: base defines it elsewhere, head copies it into the
    pinned path."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/old_home.py", REAL_DEFINITIONS["plain"], "base defines it elsewhere")
    base = _head(repo)
    head = _commit(repo, "pilot_app.py", REAL_DEFINITIONS["plain"], "copy into pinned path")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL,
                          "--expect-file", "pilot_app.py")

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


def test_unreadable_base_tree_fails_closed(tmp_path):
    """Surviving mutant 5. A failed tree read returning [] reads as "no
    definitions anywhere" -- and at the BASE that means "therefore new".

    An earlier version of this test passed a 40-zero base ref, which fails at
    merge-base and never reaches the tree read at all, so it pinned nothing.
    This corrupts the base commit's TREE object: merge-base still resolves
    (it only needs commit objects) while ls-tree genuinely fails.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "base defines it")
    base = _head(repo)
    head = _commit(repo, "src/copy.py", REAL_DEFINITIONS["plain"], "copy it")

    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "%s^{tree}" % base],
                          capture_output=True, text=True).stdout.strip()
    obj = repo / ".git" / "objects" / tree[:2] / tree[2:]
    if not obj.exists():                       # packed rather than loose
        pytest.skip("base tree object is packed; cannot corrupt it in place")
    obj.unlink()

    assert subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", base],
                          capture_output=True).returncode != 0
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "-a", base, head],
                          capture_output=True).returncode == 0

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-unreadable" in out["reasons"]
    assert code == 1


def test_tests_directory_branch_is_pinned_independently(tmp_path):
    """Surviving mutant 12. Every prior fixture under tests/ was ALSO named
    test_*.py, so it satisfied both branches of the exclusion and either half
    could be deleted with the suite green. This file is excluded by its
    DIRECTORY alone."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, "tests/helpers.py", REAL_DEFINITIONS["plain"],
                   "define it in a test helper")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert code == 1


@pytest.mark.parametrize("path", ["mypkg/docs/api.py", "src/examples/gallery.py",
                                  "src/testing/factories.py"])
def test_nested_docs_and_examples_are_production_code(path, tmp_path):
    """N5: excluding any path segment named docs/examples/tests moved the E4
    false negative rather than removing it. Only TOP-LEVEL directories are
    excluded -- a shipped `mypkg/docs/api.py` is real code."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, path, REAL_DEFINITIONS["plain"], "shipped subpackage")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True, path
    assert out["passed"] is True and code == 0


def test_unreadable_base_blob_fails_closed(tmp_path):
    """Found by my own mutation pass, same class as the verifier's mutant 5 and
    in the same function: a blob listed by ls-tree but unreadable was SKIPPED.
    At the base that silently drops a definition, and a dropped base definition
    reads as "newly introduced"."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "base defines it")
    base = _head(repo)
    head = _commit(repo, "src/copy.py", REAL_DEFINITIONS["plain"], "copy it")

    blob = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "%s:src/real.py" % base],
        capture_output=True, text=True).stdout.strip()
    obj = repo / ".git" / "objects" / blob[:2] / blob[2:]
    if not obj.exists():
        pytest.skip("base blob is packed; cannot corrupt it in place")
    obj.unlink()

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-unreadable" in out["reasons"]
    assert code == 1


# ---------------------------------------------------------------------------
# Round-4 verifier findings: two fail-opens on paths the rewrite did not cover.
# Neither is PR-author-controlled, but both are "could not determine" reading
# as "newly introduced", which is the failure this check exists to prevent.
# ---------------------------------------------------------------------------

def test_shallow_clone_without_base_ref_cannot_verify(tmp_path):
    """The shallow guard existed only on the --base-ref path. Without that flag,
    `<sha>^` fails to resolve on a shallow clone, bases becomes empty, and the
    empty-bases path -- which exists so a ROOT commit can pass -- returned True.
    Proven through `verify`, the subcommand that owns the write."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/a.py", REAL_DEFINITIONS["plain"], "symbol lands here")
    _commit(repo, "later.py", "later = 1\n", "unrelated commit on top")

    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=1", "file://%s" % repo,
                    str(shallow)], capture_output=True)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "product": "p", "done_sentence": "d", "updated": None,
        "items": [{"id": "S", "title": "t", "status": "open", "verified": False}],
    }))

    code, out = _run_gate("verify", "--status-json", str(status), "--item", "S",
                          "--repo", str(shallow), "--commit", "HEAD",
                          "--expect-definition", _SYMBOL)

    assert "definition-shallow-repo" in out["reasons"]
    assert out["passed"] is False and code == 1
    assert json.loads(status.read_text())["items"][0]["verified"] is False


def test_genuine_root_commit_still_passes_without_base_ref(tmp_path):
    """The control for the fix above: a real root commit HAS nothing before it,
    and must not be swept up by the unresolvable-parent guard."""
    repo = tmp_path / "root"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "only.py").write_text(REAL_DEFINITIONS["plain"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "root commit")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", "HEAD",
                          "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


@pytest.mark.parametrize("odd", ['we\\ird', 'we"ird', 'we\tird'])
def test_c_quoted_paths_are_not_dropped_from_the_scan(odd, tmp_path):
    """`core.quotePath=false` suppresses quoting of NON-ASCII bytes only. Git
    still C-quotes a path containing a backslash, double-quote, tab or newline,
    which then ends in `.py"` and silently leaves the scan -- and a dropped BASE
    definition reads as "not there". Same class as the non-ASCII drop fixed a
    round earlier; `-z` covers the half the flag does not."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/%s.py" % odd, REAL_DEFINITIONS["plain"], "base defines it")
    base = _head(repo)
    quoted = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", base],
                            capture_output=True, text=True).stdout
    assert '"' in quoted, "fixture must actually provoke C-quoting"

    head = _commit(repo, "src/copy.py", REAL_DEFINITIONS["plain"], "copy-paste it")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


# Only separators that are VALID Python. `def` + a bare newline is a syntax
# error, so it fails closed as unparseable -- correct, but it tests the parser,
# not the needle.
@pytest.mark.parametrize("sep", ["\t", "  ", "\t \t"])
def test_weakest_needle_invariant_is_pinned(sep, tmp_path):
    """The byte pre-filter is `b"def"`/`b"class"` precisely because anything
    narrower can miss a real definition -- and missing one at the BASE is a
    fail-open. A mutation pass showed narrowing it to `b"def "` left the suite
    green while reintroducing exactly that, so the invariant needs its own
    fixture: whitespace between `def` and the name that is not a single space."""
    repo = _init_repo(tmp_path)
    _commit(repo, "src/real.py", "def%s%s(value):\n    return value\n" % (sep, _SYMBOL),
            "base defines it with unusual spacing")
    base = _head(repo)
    head = _commit(repo, "src/copy.py", REAL_DEFINITIONS["plain"], "copy-paste it")

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert "definition-preexisting" in out["reasons"]
    assert code == 1


# ---------------------------------------------------------------------------
# Round-5 verifier finding. A surviving mutant had been pointing at this for two
# rounds (delete the tree-entry filter, suite stays green) and was written off
# as harmless both times. Surviving mutants are findings, not noise.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    "def %s(value):\n    return value\n" % _SYMBOL,   # link target IS python
    "class %s:\n    pass\n" % _SYMBOL,
])
def test_symlink_named_py_cannot_satisfy_the_check(target, tmp_path):
    """`git ls-tree` reports a symlink's TYPE as `blob`; only its MODE (120000)
    distinguishes it. Its "content" is the link target string -- so
    `ln -s "$(printf 'def foo(v):\\n    return v\\n')" foo.py` hands ast a
    module-level definition while the checked-out path is dangling and raises
    FileNotFoundError on import. One shell command, no privileged access, and it
    survives the protected-path step. Filter on mode, not type."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    os.symlink(target, repo / "src" / "fake.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "land it via a symlink")
    head = _head(repo)

    entry = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", head],
                           capture_output=True, text=True).stdout
    assert "120000 blob" in entry, "fixture must actually produce a symlink entry"

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is False
    assert out["passed"] is False and code == 1


def test_a_real_file_with_the_same_content_still_passes(tmp_path):
    """Control for the mode filter: tightening it must not block regular files.
    Executable mode counts too -- 100755 is a normal file."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head = _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"], "a real file")
    os.chmod(repo / "src" / "real.py", 0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "make it executable")
    head = _head(repo)

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert out["checks"]["definition-present"] is True
    assert out["passed"] is True and code == 0


def test_unreadable_ancestry_is_not_nothing_existed_before(tmp_path):
    """Kills the mutant that flipped `_is_root_commit`'s error return to True,
    turning "cannot read ancestry" back into "nothing existed before".

    Fabricates a commit whose parent LINE names a missing object: it resolves as
    a commit, the repo is not shallow, and rev-list fails. The verifier refuted
    my earlier claim that this branch was unreachable -- it is live defence.
    """
    repo = _init_repo(tmp_path)
    head_real = _commit(repo, "src/a.py", REAL_DEFINITIONS["plain"], "real")
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "%s^{tree}" % head_real],
                          capture_output=True, text=True).stdout.strip()
    raw = "tree %s\nparent %s\nauthor T <t@t.t> 0 +0000\ncommitter T <t@t.t> 0 +0000\n\nfabricated\n" % (
        tree, "1" * 40)
    fake = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit",
                           "-w", "--stdin", "--literally"],
                          input=raw, capture_output=True, text=True).stdout.strip()
    assert fake, "fixture must create the fabricated commit"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
                          capture_output=True, text=True).stdout.strip() == "false"

    code, out = _run_gate("check", "--repo", str(repo), "--commit", fake,
                          "--expect-definition", _SYMBOL)

    assert out["passed"] is False and code == 1
    assert "definition-unreadable" in out["reasons"]


def test_non_utf8_path_does_not_crash_the_gate(tmp_path):
    """`text=True` decodes strictly, so one .py path with non-UTF-8 bytes raised
    an uncaught UnicodeDecodeError out of every call site. Fail-closed, but a
    traceback is not a verdict.

    Two fixture traps, both of which made an earlier version test nothing while
    passing: (1) macOS stores a latin-1 e-acute written from Python as valid
    UTF-8, so the path must be added through the INDEX, not the filesystem;
    (2) `git add -A` then stages a DELETION of that entry, because it exists in
    the index but not on disk -- so the non-UTF-8 commit must come LAST.
    """
    repo = _init_repo(tmp_path)
    base = _head(repo)
    head_with_symbol = _commit(repo, "src/real.py", REAL_DEFINITIONS["plain"],
                               "real slice")
    assert head_with_symbol

    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=b"y = 1\n", capture_output=True).stdout.decode().strip()
    rc = subprocess.run(
        [b"git", b"-C", str(repo).encode(), b"update-index", b"--add",
         b"--cacheinfo", b"100644," + blob.encode() + b",src/caf\xe9.py"],
        capture_output=True).returncode
    assert rc == 0, "fixture must add a raw non-UTF-8 path"
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "non-utf8 path"],
                   capture_output=True)
    head = _head(repo)

    # -z, which is what the gate uses: plain ls-tree C-quotes the bytes as
    # `\351` rather than emitting them raw, so asserting on it would pass while
    # testing nothing.
    raw = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", head],
                         capture_output=True).stdout
    assert b"\xe9" in raw, "fixture must actually put raw bytes in the tree"

    code, out = _run_gate("check", "--repo", str(repo), "--commit", head,
                          "--base-ref", base, "--expect-definition", _SYMBOL)

    assert isinstance(out, dict) and "checks" in out     # a verdict, not a crash
    assert out["checks"]["definition-present"] is True
    assert code == 0
