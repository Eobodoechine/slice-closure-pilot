import json
import os
import re
import subprocess
import textwrap
import sys
import pytest


def _locate_gate():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "reality_gate.py"),
        os.path.join(os.path.dirname(here), "gates", "reality_gate.py"),
    ):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("reality_gate.py not found relative to %s" % here)


GATE = _locate_gate()


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("baseline\n")
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


def _load_gate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("reality_gate_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE_SOURCE = "GATE_VERSION = 1\n"

CONTRACT_BOUND = "expect_definition: some_name\n"
CONTRACT_UNBOUND = "# intentional\n"


def _base_sha(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"],
                          capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------------------
# classify: lanes
# ---------------------------------------------------------------------------

def test_classify_slice_is_normal(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "pilot_app.py", "def x():\n    pass\n", "slice")

    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "slice" and code == 0


def test_classify_gate_maintenance_is_authorized_for_judgement(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate bump")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-maintenance" and code == 0


def test_classify_ordinary_tests_only_is_a_slice_not_maintenance(tmp_path):
    """An ordinary test file is product code, not the gate's exam. Classifying
    all of tests/** as gate surface is what refused the canonical slice shape."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "tests/test_x.py", "def test_ok():\n    pass\n", "tests")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "slice" and code == 0


def test_classify_gate_own_tests_are_maintenance(tmp_path):
    """tests/test_gate_* DO pin the judge's verdicts, so they stay gate surface."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "tests/test_gate_hollow_slice.py",
                  "def test_ok():\n    pass\n", "gate matrix")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-maintenance" and code == 0
    assert out["gated"] == ["tests/test_gate_hollow_slice.py"]


def test_classify_code_plus_its_own_tests_is_a_slice(tmp_path):
    """The canonical loop output -- a symbol and the tests for it, exactly the
    shape of merged PR #5 and of the pilot's positive control PR #7. Under the
    all-of-tests/** rule these classified as gate-change-mixed-with-code and
    were refused outright, which made the gate unusable for ordinary work."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _commit(repo, "src/pilot_gate.py", "def validate():\n    pass\n", "slice")
    sha = _commit(repo, "tests/test_pilot_gate.py",
                  "def test_validate():\n    pass\n", "slice tests")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "slice" and code == 0
    assert out["changed"] == ["src/pilot_gate.py", "tests/test_pilot_gate.py"]
    assert out["gated"] == []


@pytest.mark.parametrize("path", [
    "conftest.py",
    "tests/conftest.py",
    "src/deep/conftest.py",
    "tests/__init__.py",
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
    "tox.ini",
])
def test_classify_pytest_control_files_are_gate_surface(tmp_path, path):
    """These decide WHAT pytest collects, so they decide what the head suite and
    the base negative matrix actually assert -- i.e. they reach the gate's
    verdict without touching gates/**. Narrowing the gate surface to
    gates/** + tests/test_gate_* left them classified as ordinary slices, which
    let a `slice` PR gut every later maintenance run's evidence."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, path, "# pytest control\n", "control file")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-maintenance" and code == 0
    assert out["gated"] == [path]


@pytest.mark.parametrize("path", [
    "src/pyproject.toml",
    "packages/web/setup.cfg",
    "sub/pytest.ini",
    "vendor/tox.ini",
])
def test_classify_non_root_ini_files_are_NOT_gate_surface(tmp_path, path):
    """The recall direction. pytest reads these from the rootdir, so a copy in a
    subpackage does not steer the run the gate performs -- measured: with
    sub/pyproject.toml, sub/pytest.ini and tests/pytest.ini all present, a
    root `pytest -q` still collects everything and reports no configfile.
    Matching the basename at any depth would refuse an ordinary
    'add a dependency and use it' PR as gate-change-mixed-with-code. Nothing
    pinned this, so the root anchoring could regress silently."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, path, "# not the rootdir config\n", "nested config")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "slice" and code == 0
    assert out["gated"] == []


def test_classify_tests_dir_ini_is_gate_surface(tmp_path):
    """tests/pytest.ini DOES take effect once pytest is handed a tests/-rooted
    path argument (measured: 78 collected -> 0), which a test_cmd repoint could
    introduce -- maintain only checks that the test_cmd class is PRESENT, never
    what it says."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "tests/pytest.ini", "[pytest]\n", "tests-rooted config")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-maintenance" and code == 0


def test_classify_conftest_mixed_with_code_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _commit(repo, "tests/conftest.py", "collect_ignore_glob = ['test_gate_*']\n", "conftest")
    sha = _commit(repo, "pilot_app.py", "def x():\n    pass\n", "code")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-change-mixed-with-code" and code == 1


def test_classify_gate_tests_mixed_with_code_is_still_refused(tmp_path):
    """The narrowed surface must stay fail-closed in the direction that matters."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _commit(repo, "tests/test_gate_hollow_slice.py",
            "def test_ok():\n    pass\n", "matrix")
    sha = _commit(repo, "pilot_app.py", "def x():\n    pass\n", "code")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-change-mixed-with-code" and code == 1


def test_classify_mixed_gate_with_code_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate bump")
    sha = _head(repo)
    _commit(repo, "pilot_app.py", "def x():\n    pass\n", "slice")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", _head(repo), "--base-ref", base)
    assert out["mode"] == "gate-change-mixed-with-code" and code == 1


def test_classify_workflow_touch_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, ".github/workflows/other.yml", "name: x\n", "wf")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "workflow-touch" and code == 1


def test_classify_fails_closed_on_malformed_base_ref(tmp_path):
    repo = _init_repo(tmp_path)
    sha = _head(repo)
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", "--output=/tmp/e")
    assert out["mode"] == "unreadable" and out["reason"] == "classify-bad-base-ref"
    assert code == 1


# ---------------------------------------------------------------------------
# maintain: the base gate authorizing the head gate
# ---------------------------------------------------------------------------

def test_maintain_gate_green_on_equal_version_with_bound_contract(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    sha = _commit(repo, "gates/contract.yml", CONTRACT_BOUND, "contract")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is True and code == 0
    assert out["checks"]["version-monotonic"] is True


def test_maintain_higher_version_passes(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "base v1")
    _commit(repo, "gates/contract.yml", CONTRACT_BOUND, "base contract")
    sha = _commit(repo, "gates/reality_gate.py",
                  "GATE_VERSION = 2\n", "gate v2")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is True and code == 0
    assert out["versions"] == {"base": 1, "head": 2}


def test_maintain_refuses_downgrade(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "gates/reality_gate.py",
                   "GATE_VERSION = 2\n", "base v2")
    sha = _commit(repo, "gates/reality_gate.py",
                  "GATE_VERSION = 1\n", "gate v1")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["reason"] == "maintenance-blocked:bump"
    assert out["checks"]["version-monotonic"] is False


def test_maintain_refuses_gate_that_does_not_parse(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "base v1")
    sha = _commit(repo, "gates/reality_gate.py", "def broken(:", "gate")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["reason"] == "maintenance-unparseable"


def test_maintain_refuses_unbound_head_contract(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate")
    _commit(repo, "gates/contract.yml", CONTRACT_BOUND, "base contract")
    sha = _commit(repo, "gates/contract.yml", CONTRACT_UNBOUND, "empty contract")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["reason"] == "maintenance-blocked:contract"
    assert out["checks"]["head-contract-binding"] is False


# ---------------------------------------------------------------------------
# maintain: assertion-class monotonicity (the exam may be repointed, not gutted)
# ---------------------------------------------------------------------------

# Assembled, never written literally -- the fixture must not itself satisfy a
# substring assertion it is being used to test.
_TAMPER_SUBSTRING = "def "

CONTRACT_DEFINITION = 'slice_id: "S-1"\nexpect_definition: "normalize_pilot_payload"\ntest_cmd: "python3 -m pytest -q"\n'
CONTRACT_REPOINTED = 'slice_id: "S-2"\nexpect_definition: "another_symbol"\ntest_cmd: "python3 -m pytest -q"\n'
CONTRACT_TAMPERED = 'slice_id: "S-1"\nexpect_substring: "%s"\ntest_cmd: "python3 -m pytest -q"\n' % _TAMPER_SUBSTRING


def test_maintain_allows_an_honest_contract_repoint(tmp_path):
    """Changing WHICH symbol the contract pins is ordinary maintenance."""
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml", CONTRACT_REPOINTED, "repoint")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is True and code == 0
    assert out["checks"]["assertion-classes-monotonic"] is True


def test_maintain_refuses_dropping_an_assertion_class(tmp_path):
    """The standing T2 exam-tamper: expect_definition replaced by a trivially
    satisfied expect_substring. It classifies as gate-maintenance and every
    other maintain check passes, so only class-monotonicity catches it."""
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml", CONTRACT_TAMPERED, "T2 tamper")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["reason"] == "maintenance-blocked:contract-weakened"
    assert out["checks"]["assertion-classes-monotonic"] is False
    assert out["dropped_assertion_classes"] == ["expect_definition"]
    # the checks it does NOT trip -- this is why "bound" alone was not enough
    assert out["checks"]["head-contract-binding"] is True
    assert out["checks"]["version-monotonic"] is True


def test_maintain_refuses_dropping_test_cmd_while_keeping_definition(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml",
                  'expect_definition: "normalize_pilot_payload"\n', "drop test_cmd")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["dropped_assertion_classes"] == ["test_cmd"]


def test_maintain_allows_adding_an_assertion_class(tmp_path):
    """Monotonic means no DROPS; strengthening is always allowed."""
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml",
                   'expect_definition: "normalize_pilot_payload"\n', "base contract")
    sha = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "add test_cmd")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is True and code == 0
    assert "dropped_assertion_classes" not in out


def test_maintain_ignores_commented_out_assertions(tmp_path):
    """A commented `# expect_definition:` line must not count as pinning it --
    otherwise the tamper could 'keep' the class by commenting it."""
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml",
                  '# expect_definition: "normalize_pilot_payload"\n'
                  'expect_substring: "%s"\ntest_cmd: "python3 -m pytest -q"\n'
                  % _TAMPER_SUBSTRING,
                  "comment it out")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["dropped_assertion_classes"] == ["expect_definition"]


# Commenting the line out is only ONE member of the evasion class. Each of these
# keeps text after `expect_definition:` while yaml.safe_load -- the parser that
# actually builds the gate's assertions -- yields nothing. A line scan read every
# one of them as "still pinned"; the suite gave that property ZERO coverage
# (deleting the non-empty-value requirement entirely left the suite green).
EMPTY_SHAPES = {
    "empty-string": 'expect_definition: ""\n',
    "empty-single": "expect_definition: ''\n",
    "explicit-null": "expect_definition: null\n",
    "tilde-null": "expect_definition: ~\n",
    "bare-key": "expect_definition:\n",
    "trailing-comment-only": "expect_definition:   # dropped in this PR\n",
    "nested-under-another-key": 'notes:\n  expect_definition: "normalize_pilot_payload"\n',
    "duplicate-key-last-empty": 'expect_definition: "normalize_pilot_payload"\nexpect_definition: ""\n',
    "block-scalar-mention": 'notes: |\n  expect_definition: "normalize_pilot_payload"\n',
}


@pytest.mark.parametrize("shape", sorted(EMPTY_SHAPES))
def test_maintain_refuses_every_spelling_of_an_emptied_assertion(tmp_path, shape):
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml",
                  EMPTY_SHAPES[shape] + 'test_cmd: "python3 -m pytest -q"\n',
                  "empty it via %s" % shape)
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1, \
        "%s evaded assertion-class monotonicity" % shape
    assert out["dropped_assertion_classes"] == ["expect_definition"]


def _workflow_consumer_classes(doc):
    """What the GATE will actually treat as pinned, computed with the LITERAL
    expressions lifted out of .github/workflows/slice-closure-gate.yml.

    Deliberately extracted, not reimplemented. The previous version of this
    oracle restated the rule in its own words and got it wrong in the same
    direction as the code it was checking (`str(0).strip()` is truthy, but the
    workflow's `(0 or '').strip()` is ''), so it asserted the fail-open was
    correct and stayed green. An oracle derived from the implementation cannot
    catch the implementation. This one breaks if anyone edits the workflow."""
    here = os.path.dirname(os.path.abspath(__file__))
    wf = os.path.join(os.path.dirname(here), ".github", "workflows",
                      "slice-closure-gate.yml")
    text = open(wf, encoding="utf-8").read()
    found = set()
    for key, var in (("expect_substring", "sub"),
                     ("expect_definition", "dfn"),
                     ("test_cmd", "cmd")):
        ms = re.findall(r"^\s*%s = (scalar\(%r\))\s*$" % (var, key), text, re.M)
        # Uniqueness, not just presence. `assert ms` alone catches a renamed or
        # reformatted line, but not a SECOND live copy earlier in the file: the
        # oracle would bind to the stale one and pass vacuously while the real
        # expression drifted.
        assert len(ms) == 1, \
            "expected exactly 1 workflow expression for %s, found %d" % (var, len(ms))
        try:
            value = eval(ms[0], {"__builtins__": {}}, {"scalar": _wf_scalar(doc)})  # noqa: S307
        except _WorkflowRefuses:
            # The workflow refuses the whole contract here, so nothing is pinned.
            return set()
        if value:
            found.add(key)
    return found


class _WorkflowRefuses(Exception):
    """The workflow's contract step would sys.exit(1) on this document."""


def _wf_scalar(doc):
    """The workflow's own `scalar()` helper, lifted from the YAML so the oracle
    tracks it. Extracted rather than reimplemented: the previous oracle restated
    the rule in its own words, got it wrong the same way the code did, and
    certified the bug."""
    here = os.path.dirname(os.path.abspath(__file__))
    wf = os.path.join(os.path.dirname(here), ".github", "workflows",
                      "slice-closure-gate.yml")
    body = open(wf, encoding="utf-8").read()
    m = re.search(r"^(\s*)def scalar\(key\):\n(.*?)(?=\n\1[a-zA-Z]|\n\n)", body,
                  re.M | re.S)
    assert m, "workflow's scalar() helper not found -- did the contract step change?"
    src = textwrap.dedent(m.group(0))

    def sys_exit(_code=0):
        raise _WorkflowRefuses()

    ns = {"c": doc, "sys": type("s", (), {"exit": staticmethod(sys_exit)}),
          "print": lambda *a, **k: None, "isinstance": isinstance, "type": type}
    exec(compile(src, "<workflow>", "exec"), ns)  # noqa: S102
    return ns["scalar"]


# Falsy non-strings: `_assertion_classes` once counted int/float as pinned, so
# each of these reopened the emptied-assertion exploit one respelling later.
FALSY_SHAPES = {
    "int-zero": "expect_definition: 0\n",
    "float-zero": "expect_definition: 0.0\n",
    "negative-zero": "expect_definition: -0.0\n",
    "hex-zero": "expect_definition: 0x0\n",
    "tagged-int-zero": "expect_definition: !!int 0\n",
    "false": "expect_definition: false\n",
    "whitespace-only": 'expect_definition: "   "\n',
}


@pytest.mark.parametrize("shape", sorted(FALSY_SHAPES))
def test_maintain_refuses_falsy_non_string_assertions(tmp_path, shape):
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml",
                  FALSY_SHAPES[shape] + 'test_cmd: "python3 -m pytest -q"\n',
                  "empty it via %s" % shape)
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1, \
        "%s evaded assertion-class monotonicity" % shape
    assert out["dropped_assertion_classes"] == ["expect_definition"]


def test_assertion_classes_agrees_with_the_workflow_that_consumes_it(tmp_path):
    """The invariant behind all of the above: whatever maintain believes the
    contract pins must equal what the workflow will actually assert on. If these
    two disagree, maintain is authorizing a contract the gate will not enforce."""
    yaml = pytest.importorskip("yaml")
    gate = _load_gate_module()
    bodies = (list(EMPTY_SHAPES.values()) + list(FALSY_SHAPES.values()) + [
        CONTRACT_DEFINITION, CONTRACT_REPOINTED, CONTRACT_TAMPERED,
        CONTRACT_BOUND, CONTRACT_UNBOUND, "", "not-a-mapping\n",
        'expect_definition: 1\n', 'expect_definition: true\n',
        'expect_definition: [a, b]\n', 'expect_definition: {a: b}\n',
        'expect_definition: &a "x"\nexpect_substring: *a\n',
        'base: &b {expect_definition: "x"}\nmerged:\n  <<: *b\n',
        'expect_definition: !!str ""\n',
        # Exotic non-string types. These are where the two CAN legitimately
        # differ -- see the subset/equality split below.
        'expect_definition: !!binary aGk=\n',
        'expect_definition: !!set {a: null}\n',
        'expect_definition: 2026-08-09\n',
        'expect_definition: yes\n', 'expect_definition: off\n',
    ])
    for body in bodies:
        classes, reason = gate._assertion_classes(body.encode())
        try:
            doc = yaml.safe_load(body)
        except yaml.YAMLError:
            assert classes is None and reason == "maintenance-contract-unparseable", body
            continue
        if doc is None:
            assert classes == set(), body
            continue
        if not isinstance(doc, dict):
            assert classes is None, body
            continue
        expected = _workflow_consumer_classes(doc)

        # SAFETY (must hold for every input): maintain may never believe a class
        # is pinned that the gate will not actually enforce. The reverse -- being
        # stricter than the consumer -- is safe: maintain sees the class as
        # dropped and blocks. `!!binary aGk=` is exactly that case (b'hi' is
        # truthy to the workflow, not a str here), which is why this is a subset
        # relation and not equality.
        assert classes <= expected, \
            "%r -> maintain pins %r the workflow will NOT enforce" % (
                body, classes - expected)

        # FIDELITY: for the only shapes a real contract uses -- string values --
        # the two must agree exactly, or maintain is judging a different document.
        if all(not isinstance(doc.get(k), (bytes, bool, int, float, list, dict))
               and doc.get(k) is not None or doc.get(k) is None
               for k in gate.CONTRACT_BINDING_KEYS):
            assert classes == expected, \
                "%r -> maintain sees %r, the workflow will use %r" % (
                    body, classes, expected)
        assert reason is None


# ---------------------------------------------------------------------------
# The contract is DATA, never script. A gate-maintenance PR that ADDS an
# assertion class reads as strengthening -- classify, maintain, the base
# negative matrix and the head suite all pass it -- so a contract value is
# attacker-controlled input to every later run.
# ---------------------------------------------------------------------------

def test_no_contract_output_is_interpolated_into_a_run_body(tmp_path):
    """`${{ }}` is substituted into the script's SOURCE TEXT before bash parses
    it. With `expect_substring: 'x" ] && exit 0 # '` the verdict step rendered as

        [ -n "x" ] && exit 0 # " ] && ARGS+=(--expect-substring "x" ] && exit 0 # ")

    and exited 0 with reality_gate.py never invoked: a green required check and
    no gate run at all. Contract values must reach a step through env: only."""
    here = os.path.dirname(os.path.abspath(__file__))
    wf_dir = os.path.join(os.path.dirname(here), ".github", "workflows")
    offenders = []
    for name in sorted(os.listdir(wf_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        text = open(os.path.join(wf_dir, name), encoding="utf-8").read()
        # Walk run: blocks by indentation and flag any contract interpolation.
        lines = text.splitlines()
        in_run, run_indent = False, 0
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if in_run and stripped and indent <= run_indent:
                in_run = False
            if re.match(r"^run:\s*\|", stripped):
                in_run, run_indent = True, indent
                continue
            if in_run and re.search(r"\$\{\{\s*steps\.contract\.outputs\.", line):
                offenders.append("%s:%d: %s" % (name, i, stripped))
    assert not offenders, (
        "contract values interpolated into shell source (use env: instead):\n  "
        + "\n  ".join(offenders))


def test_maintain_refuses_a_contract_value_containing_a_newline(tmp_path):
    """A newline forges extra step outputs through the GITHUB_OUTPUT writer.
    Refusing here means such a contract cannot LAND, not merely that one run
    breaks."""
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml",
                  'expect_definition: "normalize_pilot_payload"\n'
                  'expect_substring: "a\\ndfn=forged"\n', "newline injection")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["reason"] == "maintenance-contract-unsafe-value"


def test_maintain_refuses_when_the_contract_cannot_be_parsed(tmp_path):
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml", "expect_definition: [unclosed\n", "broken")
    code, out = _run_gate("maintain", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["passed"] is False and code == 1
    assert out["reason"] == "maintenance-contract-unparseable"


def test_maintain_refuses_rather_than_degrading_when_pyyaml_is_absent(tmp_path):
    """Reading the contract with a weaker parser would be a silent fallback --
    exactly how the first version of this check went fail-open. Refuse instead."""
    repo = _init_repo(tmp_path)
    _commit(repo, "gates/reality_gate.py", GATE_SOURCE, "gate v1")
    base = _commit(repo, "gates/contract.yml", CONTRACT_DEFINITION, "base contract")
    sha = _commit(repo, "gates/contract.yml", CONTRACT_TAMPERED, "T2 tamper")

    shim = tmp_path / "noyaml"
    shim.mkdir()
    (shim / "yaml.py").write_text("raise ImportError('pyyaml unavailable')\n")
    env = dict(os.environ, PYTHONPATH=str(shim))
    r = subprocess.run([sys.executable, GATE, "maintain", "--repo", str(repo),
                        "--commit", sha, "--base-ref", base],
                       capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert r.returncode == 1
    assert out["passed"] is False and out["reason"] == "maintenance-no-yaml"