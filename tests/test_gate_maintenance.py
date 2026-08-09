import json
import os
import subprocess
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


def test_classify_tests_only_is_maintenance(tmp_path):
    repo = _init_repo(tmp_path)
    base = _head(repo)
    sha = _commit(repo, "tests/test_x.py", "def test_ok():\n    pass\n", "tests")
    code, out = _run_gate("classify", "--repo", str(repo),
                          "--commit", sha, "--base-ref", base)
    assert out["mode"] == "gate-maintenance" and code == 0


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