#!/usr/bin/env python3
"""reality_gate.py -- the structural "verified" writer (closes the false-success gap).

The loop team's #1 failure is "false success": an agent reports done/fixed/committed
while the real git/filesystem state disagrees. Existing gates are instructional, not
structural. This tool makes verification structural and atomic: `verify` cannot write
`verified:true` without (a) a real, non-empty commit, (b) at least one supplied
item-binding check, and (c) every requested check passing -- all inside the same
invocation that performs the write.

Honest guarantee scope: this defends against honest hallucinated completion (an agent
that believes it finished but produced no real commit, no passing test, and whose
expected substring genuinely is not in the diff). It is NOT unfakeable against a
deliberately fraudulent caller -- the tool is invoked by the party it constrains, so
the semantic genuineness of `--expect-substring` and the correspondence between
`--commit`/`--item` are the caller's responsibility. State the guarantee precisely as:
no `verified:true` without a real commit plus a supplied check that passed in the same
call.

Convention matched from commit_diff_reread.py: stdlib-only, manual `sys.argv` parsing,
`json.dumps`/`json.dump` output, documented exit codes, GIT_TIMEOUT=30.

Binding checks -- `--expect-substring` vs `--expect-definition`:
  --expect-substring is a RAW TEXT match and is satisfied by any occurrence,
  including one inside a comment, a docstring, or a string literal. It cannot
  distinguish "the symbol was defined" from "the symbol was mentioned", and
  --expect-file does not change that (it only narrows which text is searched).
  Prefer --expect-definition, which parses the committed blob with `ast` and
  requires a real MODULE-LEVEL def/async def/class binding that the change under
  test introduced. Keep --expect-substring only for assertions that genuinely are
  about text (a config value, a version string).

  --base-ref sets what "the change under test" means: with it, the range
  base..commit (what a PR proposes); without it, just that one commit. CI should
  always pass it -- otherwise a multi-commit PR is judged on its tip alone.

Subcommands:
  check --repo <path> --commit <hash|HEAD>
        [--expect-substring <str>] [--expect-definition <identifier>]
        [--expect-file <relpath>] [--base-ref <ref>]
        [--test-cmd <cmd>] [--test-timeout N]
      Read-only. Runs ground-truth checks and prints a JSON result. Writes nothing.
      Imposes no binding-check requirement. Exit 0 iff passed, else 1.

  verify --status-json <path> --item <id-or-exact-title>
         --repo <path> --commit <hash|HEAD>
         [--expect-substring <str>] [--expect-definition <identifier>]
         [--expect-file <relpath>] [--base-ref <ref>]
         [--test-cmd <cmd>] [--test-timeout N] [--log <path>] [--now <iso8601>]
      Runs the SAME checks as `check`, then -- iff they pass -- atomically marks the
      located item verified. REQUIRES at least one binding check (--expect-substring,
      --expect-definition, and/or --test-cmd). Exit 0 on pass-and-write, 1 on
      check-fail-and-downgrade, 2 on usage/lookup/bad-status errors.

  init-status --path <path> --product <name> --done <sentence>
      Writes a status.json skeleton (empty items) iff --path does not exist. Never
      clobbers an existing file (exit 2). Exit 0 on create.

Subcommands:
  check, verify, init-status (above) plus:

  classify --repo <path> --commit <hash> --base-ref <ref>
      Read-only. Computes which risk-ladder lane a PR falls into, from GIT
      DATA ONLY (unforgeable by the PR). Prints {"mode": <token>,
      "changed": [...]}. Modes:
        slice              -- no gate/workflow paths touched (normal)
        gate-maintenance   -- only gates/** and/or tests/**, nothing else
        gate-change-mixed-with-code -- gate paths mixed with other paths (refused)
        workflow-touch     -- any .github/workflows/** changed (refused, ceremony)
      Exit 0 iff mode is slice or gate-maintenance, else 1.

  maintain --repo <path> --commit <hash> --base-ref <ref>
      Maintenance-mode judgement: the OLD (base) gate authorizing the NEW
      (head) gate. Checks, all fail-closed:
        head-gate-parses        -- AST-parse of head gates/reality_gate.py
        version-monotonic       -- head GATE_VERSION >= base GATE_VERSION
                                  ("version N authorizes only >= N")
        head-contract-binding   -- the head gates/contract.yml pins at least one
                                  of expect_definition/expect_substring/test_cmd
      Prints a JSON result. Exit 0 iff all pass, else 1.
      The head test suite and the BASE negative matrix are run by the
      workflow itself (they are pytest invocations), not here.

Exit codes:
  0  passed (check) / verified written (verify) / created (init-status)
  1  check failed (check/verify): a requested check did not pass
  2  usage error, item not found/ambiguous, bad status.json, or existing init path.
     Named reason tokens appear in the stdout JSON: "no-binding-check",
     "test-timeout", "bad-status-json", "substring-absent",
     "definition-absent",
     "definition-preexisting" (already defined at a merge base -- this change
     did not land it), "definition-bad-base-ref", "definition-shallow-repo"
     (no ancestry to compute a merge base from -- fix the checkout depth),
     "definition-unparseable", "definition-unreadable".
"""
import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

GIT_TIMEOUT = 30
DEFAULT_TEST_TIMEOUT = 600

# GATE_VERSION -- the gate judges its own upgrades. The running gate (from the
# base branch, under pull_request_target) refuses to authorize a head gate whose
# GATE_VERSION is lower than its own (anti-downgrade, minimal form: "version N
# authorizes only >= N"). Bump this when the gate's verdict contract changes.
GATE_VERSION = 1


# ---------------------------------------------------------------------------
# Serialization (pinned -- makes writes deterministic)
# ---------------------------------------------------------------------------

def _dump_atomic(path: str, obj) -> None:
    """Write obj to path with the pinned serializer, atomically (temp + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp_path, path)


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with a trailing Z, seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# git helpers (read-only)
# ---------------------------------------------------------------------------

def _git(repo: str, *args) -> Optional[subprocess.CompletedProcess]:
    """Run `git -C <repo> <args...>` with a timeout. Returns the CompletedProcess,
    or None on any failure (git missing, timeout, OS error) -- never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # Decode permissively: `text=True` uses strict UTF-8, so a single .py path
    # with non-UTF-8 bytes raised an uncaught UnicodeDecodeError out of every
    # call site. Blob CONTENT is read by sha, not by path, so a replaced path
    # character cannot corrupt what gets parsed.
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _resolve_commit(repo: str, commit: str) -> Optional[str]:
    """Resolve a commit-ish (e.g. HEAD or a short hash) to its full 40-char sha,
    or None if it cannot be resolved to a commit."""
    r = _git(repo, "rev-parse", "--verify", "%s^{commit}" % commit)
    if r is None or r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha if len(sha) == 40 else None


def _commit_is_real(repo: str, sha: str) -> bool:
    """True iff the commit changed at least one file (non-empty diffstat). A phantom
    `git commit --allow-empty` yields an empty stat -> False."""
    r = _git(repo, "show", sha, "--stat", "--format=")
    if r is None or r.returncode != 0:
        return False
    return bool(r.stdout.strip())


def _added_lines(repo: str, sha: str) -> Optional[List[str]]:
    """Return the content of the patch's ADDED lines (leading '+' stripped, the
    '+++' file header excluded). None on git failure. Rename-only or binary diffs
    naturally yield no added content lines."""
    r = _git(repo, "show", sha)
    if r is None or r.returncode != 0:
        return None
    added = []
    for line in r.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


def _substring_present(repo: str, sha: str, substring: str,
                       expect_file: Optional[str]) -> bool:
    """Whether `substring` is present per spec semantics.

    --expect-file: match against that path's COMMITTED BLOB (git show <sha>:<path>),
    decoding bytes utf-8 with errors="replace".
    Otherwise: match within a SINGLE added patch line's content (no cross-line span,
    leading '+' already stripped)."""
    if expect_file is not None:
        r = subprocess_git_bytes(repo, "%s:%s" % (sha, expect_file))
        if r is None:
            return False
        text = r.decode("utf-8", errors="replace")
        return substring in text
    added = _added_lines(repo, sha)
    if not added:
        return False
    return any(substring in line for line in added)


def _is_non_production_path(path: str) -> bool:
    """Paths whose contents are not shipped capability. A symbol that exists only
    in a test, a doc, or an example is not a landed slice.

    Deliberately NARROW on directory names: an earlier revision excluded any
    segment named `test`/`testing`, which wrongly blocked shipped packages like
    `src/testing/factories.py` (django.test, pytest plugins, mypkg/testing/ are
    all real layouts).
    """
    parts = path.split("/")
    # TOP-LEVEL only. Matching any segment made shipped subpackages -- a real
    # `mypkg/docs/api.py`, `src/examples/gallery.py` -- invisible to the check,
    # which is a false negative, not safety.
    if len(parts) > 1 and parts[0] in ("tests", "__tests__", "docs", "examples"):
        return True
    base = parts[-1]
    if base == "conftest.py":
        return True
    return base.startswith("test_") or base.endswith("_test.py")


def _python_blobs_at(repo: str, ref: str) -> Optional[List[Tuple[str, str]]]:
    """(blob_sha, path) for every .py file in the tree at `ref`. None on failure.

    Enumerated from the TREE, never with `git grep`. `git grep <tree-ish>` reads
    .gitattributes from the WORKING TREE, which in CI is the PR head -- so a PR
    could ship two lines of .gitattributes marking the base's files `binary`,
    have `-I` drop them from the base scan, and make an already-landed symbol
    look newly introduced. Nothing about the head tree may influence what is
    scanned at the base.
    """
    # -z, not core.quotePath=false: that flag only suppresses quoting of
    # NON-ASCII bytes. Git still C-quotes a path containing a newline, tab,
    # double-quote, or backslash, which then ends in `.py"` and silently drops
    # out of the scan -- and a dropped base definition reads as "not there".
    r = _git(repo, "ls-tree", "-r", "-z", ref)
    if r is None or r.returncode != 0:
        return None
    out: List[Tuple[str, str]] = []
    for line in r.stdout.split("\0"):
        if not line:
            continue
        meta, _, path = line.partition("\t")
        if not path.endswith(".py"):
            continue
        parts = meta.split()
        # Filter on MODE, not object type. A symlink's git type is also "blob",
        # and its content is the LINK TARGET STRING -- so `ln -s "$(printf
        # 'def foo(v):\n    return v\n')" foo.py` gives ast a module-level
        # definition to parse while the checked-out file is dangling and raises
        # FileNotFoundError on import. One shell command, no privileged access.
        # Regular files only: 100644 and 100755.
        if len(parts) >= 3 and parts[0] in ("100644", "100755"):
            out.append((parts[2], path))
    return out


def _read_blobs(repo: str, shas: List[str]) -> Optional[Dict[str, bytes]]:
    """Read many blobs in ONE `git cat-file --batch`, keyed by sha. None on
    failure. Batching keeps the whole-tree scan to two git calls per ref."""
    if not shas:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "cat-file", "--batch"],
            input="\n".join(shas).encode() + b"\n",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    data = proc.stdout
    blobs: Dict[str, bytes] = {}
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        header = data[pos:nl].split()
        pos = nl + 1
        if len(header) != 3:
            return None          # "<sha> missing" or malformed -> fail closed
        try:
            size = int(header[2])
        except ValueError:
            return None
        blobs[header[0].decode()] = data[pos:pos + size]
        pos += size + 1          # trailing newline
    return blobs


def _module_level_defs_from_source(blob: bytes) -> Optional[set]:
    """Module-level def/async def/class names bound by `blob`, minus any a
    module-level `del` unbinds. None if it does not parse as Python."""
    try:
        tree = ast.parse(blob.decode("utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return None
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.discard(target.id)
    return names


def _defines_at(repo: str, ref: str, name: str,
                expect_file: Optional[str]) -> Tuple[Optional[bool], Optional[str]]:
    """Whether `name` is a module-level definition in production code at `ref`.
    (None, reason) when it cannot be determined -- callers must fail closed on it,
    since "I could not read it" is not "it was not there"."""
    entries = _python_blobs_at(repo, ref)
    if entries is None:
        return None, "definition-unreadable"
    if expect_file is not None:
        entries = [(sha, p) for sha, p in entries if p == expect_file]
    else:
        entries = [(sha, p) for sha, p in entries
                   if not _is_non_production_path(p)]

    blobs = _read_blobs(repo, [sha for sha, _ in entries])
    if blobs is None:
        return None, "definition-unreadable"

    # Pre-filter on BYTES already read from the tree, so nothing outside the
    # tree can affect it. Deliberately the weakest possible filter that cannot
    # produce a false negative: a module-level def/class binding REQUIRES the
    # literal token `def` or `class` in the source, whatever the whitespace,
    # line endings, or backslash continuations around the name. Anything
    # narrower re-creates the pre-filter-vs-AST divergence that let a
    # backslash-joined definition at the base go unseen -- a fail-OPEN, since
    # an unseen base definition reads as "newly introduced".
    needles = (b"def", b"class")
    for sha, path in entries:
        blob = blobs.get(sha)
        if blob is None:
            return None, "definition-unreadable"
        if not any(n in blob for n in needles):
            continue
        defs = _module_level_defs_from_source(blob)
        if defs is None:
            # Present but unparseable. Cannot prove presence OR absence here.
            return None, "definition-unparseable"
        if name in defs:
            return True, None
    return False, None


def _is_root_commit(repo: str, sha: str) -> bool:
    """True iff `sha` genuinely has no parents. Only meaningful once shallowness
    has been ruled out -- a grafted tip in a shallow clone also reports none."""
    r = _git(repo, "rev-list", "--parents", "-n", "1", sha)
    if r is None or r.returncode != 0:
        return False
    return len(r.stdout.split()) == 1


def _is_shallow(repo: str) -> bool:
    r = _git(repo, "rev-parse", "--is-shallow-repository")
    return r is not None and r.returncode == 0 and r.stdout.strip() == "true"


def _merge_bases(repo: str, base_ref: str, sha: str) -> List[str]:
    """ALL merge bases. A criss-cross history has more than one, and
    `git merge-base` without -a returns an arbitrary one -- pick the base that
    happens not to define the symbol and an already-landed function looks new."""
    r = _git(repo, "merge-base", "-a", base_ref, sha)
    if r is None or r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _definition_present(repo: str, sha: str, name: str,
                        expect_file: Optional[str],
                        base_ref: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Whether the change under test ITSELF landed `name` as a module-level
    definition: absent from production code at the merge base, present at head.

    Compares WHOLE-TREE presence at two commits rather than diffing per path.
    Per-path range diffing was the wrong primitive and leaked two ways -- an
    unrebased branch whose base had renamed the defining file scored that file's
    stale copy as newly introduced, and a pure rename (0 insertions, 0 deletions)
    counted as landing the symbol. Tree presence has no such edge: if it existed
    anywhere in production code at the merge base, it was not landed here.

    The MERGE BASE, not the base tip: `base..head` is a tree-to-tree diff, while
    what a PR proposes is `base...head`. Using the tip is what let a stale branch
    claim a symbol it never wrote.

    Unreadable or unparseable inputs fail CLOSED. "I could not determine it" must
    never read as "it is new".

    KNOWN LIMIT: a decorator that rebinds the name (`@_kill def foo` leaving
    `foo is None`) still satisfies this. The check is "a module-level def/class
    statement binds this name", which is decidable; "importing the module yields
    a callable" is not, and no sound static rule separates a nulling decorator
    from `@functools.cache`.
    """
    if base_ref is not None:
        if not base_ref.strip() or base_ref.startswith("-"):
            return False, "definition-bad-base-ref"
        bases = _merge_bases(repo, base_ref, sha)
        if not bases:
            # A shallow clone has no ancestry to compute a merge base from, so
            # EVERY PR would block. Name it, rather than reporting a generic
            # unreadable -- the symptom is "the gate says every slice is
            # hollow", and the cause is the checkout, not the code.
            if _is_shallow(repo):
                return False, "definition-shallow-repo"
            return False, "definition-unreadable"
    else:
        parent = _resolve_commit(repo, "%s^" % sha)
        if parent is None:
            # A genuine root commit really has nothing before it. A parent we
            # cannot SEE is a different thing entirely, and must never read as
            # "nothing existed before" -- that is the fail-open this check
            # exists to prevent. Order matters: a shallow clone's grafted tip
            # looks parentless to rev-list, so test shallowness FIRST.
            if _is_shallow(repo):
                return False, "definition-shallow-repo"
            # Reachable: a commit whose parent LINE names a missing object
            # resolves as a commit, is not shallow, and fails rev-list -- so
            # this is live defence, not dead code. (An earlier comment here
            # claimed it was unreachable; that was wrong. Deleting the parent
            # object is not enough, because `<sha>^` is read from the child.)
            if not _is_root_commit(repo, sha):
                return False, "definition-unreadable"
            bases = []
        else:
            bases = [parent]

    head_has, reason = _defines_at(repo, sha, name, expect_file)
    if head_has is None:
        return False, reason
    if not head_has:
        return False, "definition-absent"
    if not bases:
        return True, None

    # Base side is deliberately NOT scoped by expect_file: a symbol that merely
    # moved into the pinned path was not landed by this change. Pre-existing at
    # ANY merge base counts.
    for base in bases:
        base_has, reason = _defines_at(repo, base, name, None)
        if base_has is None:
            return False, reason
        if base_has:
            return False, "definition-preexisting"
    return True, None


def subprocess_git_bytes(repo: str, spec: str) -> Optional[bytes]:
    """`git -C repo show <spec>` returning raw bytes (for binary-safe blob reads),
    or None on failure."""
    try:
        r = subprocess.run(
            ["git", "-C", repo, "show", spec],
            capture_output=True, timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _test_passes(repo: str, cmd: str, timeout: int) -> Tuple[bool, Optional[str]]:
    """Run cmd via shell in cwd=repo, reading the REAL returncode directly (never
    through a pipe). Returns (passed, reason). A TimeoutExpired -> (False,
    "test-timeout")."""
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=repo,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "test-timeout"
    except OSError as e:
        return False, "test-error: %s" % e
    return r.returncode == 0, None


# ---------------------------------------------------------------------------
# Shared check runner (no divergence between `check` and `verify`)
# ---------------------------------------------------------------------------

def run_checks(repo: str, commit: str, expect_substring: Optional[str],
               expect_file: Optional[str], test_cmd: Optional[str],
               test_timeout: int,
               expect_definition: Optional[str] = None,
               base_ref: Optional[str] = None) -> Dict:
    """Run the ground-truth checks and return the result dict:
      {"passed": bool, "commit": <sha-or-None>,
       "checks": {"commit-is-real": bool, "substring-present": bool/None,
                  "definition-present": bool/None, "test-passes": bool/None},
       "reasons": [<token>, ...]}
    `passed` is the logical AND over the checks that actually ran."""
    reasons: List[str] = []
    sha = _resolve_commit(repo, commit)

    if sha is None:
        commit_real = False
        reasons.append("commit-unresolved")
    else:
        commit_real = _commit_is_real(repo, sha)
        if not commit_real:
            reasons.append("empty-commit")

    substring_check: Optional[bool] = None
    if expect_substring is not None:
        if sha is None:
            substring_check = False
        else:
            substring_check = _substring_present(repo, sha, expect_substring,
                                                 expect_file)
        if substring_check is False:
            reasons.append("substring-absent")

    definition_check: Optional[bool] = None
    if expect_definition is not None:
        if sha is None:
            definition_check, def_reason = False, "definition-absent"
        else:
            definition_check, def_reason = _definition_present(
                repo, sha, expect_definition, expect_file, base_ref)
        if definition_check is False:
            reasons.append(def_reason or "definition-absent")

    test_check: Optional[bool] = None
    if test_cmd is not None:
        test_check, test_reason = _test_passes(repo, test_cmd, test_timeout)
        if test_reason is not None:
            reasons.append(test_reason)
        elif test_check is False:
            reasons.append("test-failed")

    ran = [v for v in (commit_real, substring_check, definition_check, test_check)
           if v is not None]
    passed = all(ran)

    return {
        "passed": passed,
        "commit": sha,
        "checks": {
            "commit-is-real": commit_real,
            "substring-present": substring_check,
            "definition-present": definition_check,
            "test-passes": test_check,
        },
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# argv parsing (manual, commit_diff_reread.py style)
# ---------------------------------------------------------------------------

def _parse_flags(rest: List[str]) -> Optional[Dict[str, str]]:
    """Parse `--key value` pairs into a dict. Returns None on a malformed flag
    (a --key with no following value, or a bare non-flag token)."""
    opts: Dict[str, str] = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok.startswith("--"):
            return None
        key = tok[2:]
        if i + 1 >= len(rest):
            return None
        opts[key] = rest[i + 1]
        i += 2
    return opts


def _usage_error(message: str) -> int:
    print(json.dumps({"passed": False, "error": "usage_error", "reason": message}))
    return 2


# ---------------------------------------------------------------------------
# PR lane classification (maintenance mode): which risk-ladder lane a PR
# falls into, computed from GIT DATA ONLY so the PR cannot forge it.
# ---------------------------------------------------------------------------

def _changed_paths(repo: str, base_ref: str, sha: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """All paths changed relative to EVERY merge base (union), sorted.
    Consult every base, matching _definition_present's philosophy: picking an
    arbitrary one lets a criss-cross history undercount. Returns (paths, None)
    or (None, reason) fail-closed (shallowness named, anything else unreadable)."""
    if not base_ref.strip() or base_ref.startswith("-"):
        return None, "classify-bad-base-ref"
    bases = _merge_bases(repo, base_ref, sha)
    if not bases:
        if _is_shallow(repo):
            return None, "classify-shallow-repo"
        return None, "classify-unreadable"
    changed = set()
    for base in bases:
        r = _git(repo, "diff", "--name-only", base, sha)
        if r is None or r.returncode != 0:
            return None, "classify-unreadable"
        for ln in r.stdout.splitlines():
            p = ln.strip()
            if p:
                changed.add(p)
    return sorted(changed), None


_GATE_TREES = ("gates/", "tests/")
_WORKFLOW_TREE = ".github/workflows/"


def _classify_lane(changed: List[str]) -> Tuple[str, List[str], List[str], List[str]]:
    """Bucket changed paths and decide the lane: workflow-touch (ceremony,
    always running red before the bypass actor merges), gate-maintenance
    (gates/** and/or tests/** and nothing else), or the two refusal states --
    gate-change-mixed-with-code and slice (normal)."""
    workflows = [p for p in changed if p.startswith(_WORKFLOW_TREE)]
    gated = [p for p in changed
             if p.startswith(_GATE_TREES[0]) or p.startswith(_GATE_TREES[1])]
    other = [p for p in changed if p not in workflows and p not in gated]
    if workflows:
        return "workflow-touch", workflows, gated, other
    if gated and other:
        return "gate-change-mixed-with-code", workflows, gated, other
    if gated:
        return "gate-maintenance", workflows, gated, other
    return "slice", workflows, gated, other


# ---------------------------------------------------------------------------
# Maintenance judgement: the OLD (base) gate authorizing the NEW (head) gate.
# ---------------------------------------------------------------------------

CONTRACT_BINDING_KEYS = ("expect_definition", "expect_substring", "test_cmd")


def _module_level_version(blob: bytes, name: str) -> Tuple[Optional[int], bool]:
    """Extract the module-level `name = <int>` (or `name: int = <int>`) from a
    Python blob by AST. Returns (value, parsed_ok). A missing assignment is
    (None, True) -- the file parsed but never sets the version; an unparseable
    file is (None, False)."""
    try:
        tree = ast.parse(blob.decode("utf-8", errors="replace"))
    except SyntaxError:
        return None, False
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == name \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, int):
            return node.value.value, True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name \
                and node.value is not None \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, int):
            return node.value.value, True
    return None, True


def _contract_is_bound(blob: bytes) -> bool:
    """The contract text pins a non-empty value for at least one of
    expect_definition / expect_substring / test_cmd. Deliberately a plain
    line scan (stdlib-only here; the workflow's pyyaml step does the full
    parse) - but it is fail-closed: a weird YAML shape I cannot confidently
    read reads as unbound, never as bound."""
    text = blob.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for key in CONTRACT_BINDING_KEYS:
            if stripped.startswith(key + ":") and stripped[len(key) + 1:].strip():
                return True
    return False


# ---------------------------------------------------------------------------
# Subcommand: check
# ---------------------------------------------------------------------------

def cmd_check(rest: List[str]) -> int:
    opts = _parse_flags(rest)
    if opts is None or "repo" not in opts or "commit" not in opts:
        return _usage_error("check requires --repo and --commit")

    expect_substring = opts.get("expect-substring")
    expect_definition = opts.get("expect-definition")
    expect_file = opts.get("expect-file")
    base_ref = opts.get("base-ref")
    test_cmd = opts.get("test-cmd")

    if expect_file is not None and expect_substring is None \
            and expect_definition is None:
        return _usage_error(
            "--expect-file requires --expect-substring or --expect-definition")
    if expect_definition is not None and not expect_definition.isidentifier():
        return _usage_error(
            "--expect-definition must be a bare identifier (e.g. 'foo', not 'def foo')")

    try:
        test_timeout = int(opts.get("test-timeout", DEFAULT_TEST_TIMEOUT))
    except ValueError:
        return _usage_error("--test-timeout must be an integer")

    result = run_checks(opts["repo"], opts["commit"], expect_substring,
                        expect_file, test_cmd, test_timeout,
                        expect_definition=expect_definition, base_ref=base_ref)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------

def _find_item(items: List[Dict], key: str) -> Tuple[str, Optional[Dict]]:
    """Locate an item by id, else by exact title.
    Returns (status, item) where status is "ok", "missing", or "ambiguous"."""
    by_id = [it for it in items if isinstance(it, dict) and it.get("id") == key]
    if len(by_id) == 1:
        return "ok", by_id[0]
    if len(by_id) > 1:
        return "ambiguous", None

    by_title = [it for it in items if isinstance(it, dict) and it.get("title") == key]
    if len(by_title) == 1:
        return "ok", by_title[0]
    if len(by_title) > 1:
        return "ambiguous", None
    return "missing", None


def cmd_verify(rest: List[str]) -> int:
    opts = _parse_flags(rest)
    required = ("status-json", "item", "repo", "commit")
    if opts is None or any(k not in opts for k in required):
        return _usage_error("verify requires --status-json, --item, --repo, --commit")

    expect_substring = opts.get("expect-substring")
    expect_definition = opts.get("expect-definition")
    expect_file = opts.get("expect-file")
    base_ref = opts.get("base-ref")
    test_cmd = opts.get("test-cmd")
    log = opts.get("log")
    now = opts.get("now")

    if expect_file is not None and expect_substring is None \
            and expect_definition is None:
        return _usage_error(
            "--expect-file requires --expect-substring or --expect-definition")
    if expect_definition is not None and not expect_definition.isidentifier():
        return _usage_error(
            "--expect-definition must be a bare identifier (e.g. 'foo', not 'def foo')")

    # Binding requirement (SECURITY): commit-is-real alone can never verify.
    if expect_substring is None and expect_definition is None and test_cmd is None:
        print(json.dumps({
            "passed": False,
            "error": "no-binding-check",
            "reason": "no-binding-check: verify requires --expect-substring, "
                      "--expect-definition, and/or --test-cmd",
        }))
        return 2

    try:
        test_timeout = int(opts.get("test-timeout", DEFAULT_TEST_TIMEOUT))
    except ValueError:
        return _usage_error("--test-timeout must be an integer")

    status_path = opts["status-json"]

    # Load status.json, fail-closed on any parse/read error (no write, no traceback).
    try:
        with open(status_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        print(json.dumps({
            "passed": False,
            "error": "bad-status-json",
            "reason": "bad-status-json: status.json missing or unparseable",
        }))
        return 2

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        print(json.dumps({
            "passed": False,
            "error": "bad-status-json",
            "reason": "bad-status-json: 'items' is not a list",
        }))
        return 2

    # Locate the item BEFORE mutating anything; missing/ambiguous -> no write.
    lookup, item = _find_item(items, opts["item"])
    if lookup != "ok":
        print(json.dumps({
            "passed": False,
            "error": "item-%s" % lookup,
            "reason": "item %s: %r" % (lookup, opts["item"]),
        }))
        return 2

    result = run_checks(opts["repo"], opts["commit"], expect_substring,
                        expect_file, test_cmd, test_timeout,
                        expect_definition=expect_definition, base_ref=base_ref)

    updated = now if now is not None else _now_iso()
    data["updated"] = updated

    if result["passed"]:
        item["verified"] = True
        item["status"] = "fixed"
        item["evidence"] = {
            "commit": result["commit"],
            "test": test_cmd,
            "log": log,
        }
        _dump_atomic(status_path, data)
        print(json.dumps(result))
        return 0

    # Fail path: downgrade toward less-verified (never fakes a pass).
    item["verified"] = False
    _dump_atomic(status_path, data)
    print(json.dumps(result))
    return 1


# ---------------------------------------------------------------------------
# Subcommand: init-status
# ---------------------------------------------------------------------------

def cmd_init_status(rest: List[str]) -> int:
    opts = _parse_flags(rest)
    if opts is None or any(k not in opts for k in ("path", "product", "done")):
        return _usage_error("init-status requires --path, --product, --done")

    path = opts["path"]
    if os.path.exists(path):
        print(json.dumps({
            "created": False,
            "error": "exists",
            "reason": "refusing to clobber existing file: %s" % path,
        }))
        return 2

    skeleton = {
        "product": opts["product"],
        "done_sentence": opts["done"],
        "updated": None,
        "items": [],
    }
    _dump_atomic(path, skeleton)
    print(json.dumps({"created": True, "path": path}))
    return 0


# ---------------------------------------------------------------------------
# Subcommand: classify
# ---------------------------------------------------------------------------

def cmd_classify(rest: List[str]) -> int:
    opts = _parse_flags(rest)
    if opts is None or any(k not in opts for k in ("repo", "commit", "base-ref")):
        return _usage_error("classify requires --repo, --commit, --base-ref")

    changed, reason = _changed_paths(opts["repo"], opts["base-ref"], opts["commit"])
    if changed is None:
        print(json.dumps({
            "passed": False,
            "mode": "unreadable",
            "reason": reason,
            "changed": [],
        }))
        return 1

    mode, workflows, gated, other = _classify_lane(changed)
    passed = mode in ("slice", "gate-maintenance")
    print(json.dumps({
        "passed": passed,
        "mode": mode,
        "changed": changed,
        "workflows": workflows,
        "gated": gated,
        "other": other,
    }))
    return 0 if passed else 1


# ---------------------------------------------------------------------------
# Subcommand: maintain
# ---------------------------------------------------------------------------

def cmd_maintain(rest: List[str]) -> int:
    opts = _parse_flags(rest)
    if opts is None or any(k not in opts for k in ("repo", "commit", "base-ref")):
        return _usage_error("maintain requires --repo, --commit, --base-ref")

    repo, sha, base = opts["repo"], opts["commit"], opts["base-ref"]

    # 1. Head gate must parse, and must carry a GATE_VERSION.
    head_blob = subprocess_git_bytes(repo, "%s:gates/reality_gate.py" % sha)
    if head_blob is None:
        print(json.dumps({
            "passed": False,
            "reason": "maintenance-unreadable",
            "checks": {"head-gate-parses": False, "version-monotonic": False,
                       "head-contract-binding": False},
        }))
        return 1
    head_version, head_parsed = _module_level_version(head_blob, "GATE_VERSION")
    if not head_parsed:
        print(json.dumps({
            "passed": False,
            "reason": "maintenance-unparseable",
            "checks": {"head-gate-parses": False, "version-monotonic": False,
                       "head-contract-binding": False},
        }))
        return 1
    if head_version is None:
        print(json.dumps({
            "passed": False,
            "reason": "maintenance-no-version",
            "checks": {"head-gate-parses": True, "version-monotonic": False,
                       "head-contract-binding": False},
        }))
        return 1

    # 2. Version monotonicity: the head gate must not be older than the base
    #    gate it is asking to authorize. (Minimum form: N authorizes only >= N.)
    base_blob = subprocess_git_bytes(repo, "%s:gates/reality_gate.py" % base)
    if base_blob is None:
        print(json.dumps({
            "passed": False,
            "reason": "maintenance-base-unreadable",
            "checks": {"head-gate-parses": True, "version-monotonic": False,
                       "head-contract-binding": False},
        }))
        return 1
    base_version, base_parsed = _module_level_version(base_blob, "GATE_VERSION")
    if not base_parsed or base_version is None:
        base_version = 0

    monotonic = head_version >= base_version

    # 3. The HEAD contract must still pin a binding check (the base contract's
    #    bindings are what the base gate enforces; a maintenance PR may not
    #    empty the head contract the next run would inherit).
    head_contract = subprocess_git_bytes(repo, "%s:gates/contract.yml" % sha)
    bound = head_contract is not None and _contract_is_bound(head_contract)

    checks = {
        "head-gate-parses": head_parsed,
        "version-monotonic": monotonic,
        "head-contract-binding": bound,
    }
    passed = head_parsed and monotonic and bound
    result = {
        "passed": passed,
        "checks": checks,
        "versions": {"base": base_version, "head": head_version},
    }
    if not passed:
        result["reason"] = "maintenance-blocked:bump" \
            if not monotonic else "maintenance-blocked:contract"
    print(json.dumps(result))
    return 0 if passed else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    args = argv[1:]
    if not args:
        print(json.dumps({
            "error": "usage: reality_gate.py <check|verify|init-status|classify|maintain> ...",
        }))
        return 2

    subcommand = args[0]
    rest = args[1:]

    if subcommand == "check":
        return cmd_check(rest)
    if subcommand == "verify":
        return cmd_verify(rest)
    if subcommand == "init-status":
        return cmd_init_status(rest)
    if subcommand == "classify":
        return cmd_classify(rest)
    if subcommand == "maintain":
        return cmd_maintain(rest)

    print(json.dumps({"error": "unknown subcommand: %s" % subcommand}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
