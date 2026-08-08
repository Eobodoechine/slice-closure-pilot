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

Exit codes:
  0  passed (check) / verified written (verify) / created (init-status)
  1  check failed (check/verify): a requested check did not pass
  2  usage error, item not found/ambiguous, bad status.json, or existing init path.
     Named reason tokens appear in the stdout JSON: "no-binding-check",
     "test-timeout", "bad-status-json", "substring-absent", "definition-absent",
     "definition-preexisting" (already defined at the merge base -- this change
     did not land it), "definition-bad-base-ref", "definition-unparseable",
     "definition-unreadable".
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
        return subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


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
    if any(p in ("tests", "__tests__", "docs", "examples") for p in parts[:-1]):
        return True
    base = parts[-1]
    if base == "conftest.py":
        return True
    return base.startswith("test_") or base.endswith("_test.py")


def _module_level_defs(repo: str, ref: str, path: str) -> Optional[set]:
    """The names bound at MODULE level by a def/async def/class in `path` at `ref`,
    minus any that a module-level `del` unbinds. None if the blob is unreadable or
    does not parse as Python.

    Deliberately `tree.body`, not `ast.walk`: walking counts a definition anywhere
    in the file, including positions that never bind an importable module
    attribute -- inside `if False:`, under `if TYPE_CHECKING:`, nested in another
    function, or as a method on a class.
    """
    blob = subprocess_git_bytes(repo, "%s:%s" % (ref, path))
    if blob is None:
        return None
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


def _candidate_paths(repo: str, ref: str, name: str) -> Optional[List[str]]:
    """.py paths at `ref` that could plausibly define `name`. A cheap regex
    pre-filter so the whole tree need not be parsed -- the AST still decides.
    Over-inclusive by design (it matches nested and dead-code defs too).
    None on git failure; [] legitimately means no candidates."""
    # POSIX ERE: `\b` is NOT supported by git grep -E -- it matches nothing and
    # exits 1, which is indistinguishable from "no candidates" and would make
    # every definition invisible. Spell the word boundary out.
    pattern = (r"^[[:space:]]*(async[[:space:]]+)?(def|class)[[:space:]]+"
               r"%s([^A-Za-z0-9_]|$)" % name)
    r = _git(repo, "-c", "core.quotePath=false", "grep", "-l", "-I", "-E",
             pattern, ref, "--", "*.py")
    if r is None or r.returncode not in (0, 1):
        return None
    prefix = "%s:" % ref
    return [ln[len(prefix):] for ln in r.stdout.splitlines()
            if ln.startswith(prefix)]


def _defines_at(repo: str, ref: str, name: str,
                expect_file: Optional[str]) -> Tuple[Optional[bool], Optional[str]]:
    """Whether `name` is a module-level definition in production code at `ref`.
    (None, reason) when it cannot be determined -- callers must fail closed on it,
    since "I could not read it" is not "it was not there"."""
    paths = _candidate_paths(repo, ref, name)
    if paths is None:
        return None, "definition-unreadable"
    if expect_file is not None:
        paths = [p for p in paths if p == expect_file]
    else:
        paths = [p for p in paths if not _is_non_production_path(p)]
    for path in paths:
        defs = _module_level_defs(repo, ref, path)
        if defs is None:
            # Present but unparseable. Cannot prove presence OR absence here.
            return None, "definition-unparseable"
        if name in defs:
            return True, None
    return False, None


def _merge_base(repo: str, base_ref: str, sha: str) -> Optional[str]:
    r = _git(repo, "merge-base", base_ref, sha)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() or None


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
        base = _merge_base(repo, base_ref, sha)
        if base is None:
            return False, "definition-unreadable"
    else:
        # No parent (root commit) => nothing existed before it.
        base = _resolve_commit(repo, "%s^" % sha)

    head_has, reason = _defines_at(repo, sha, name, expect_file)
    if head_has is None:
        return False, reason
    if not head_has:
        return False, "definition-absent"
    if base is None:
        return True, None

    # Base side is deliberately NOT scoped by expect_file: a symbol that merely
    # moved into the pinned path was not landed by this change.
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
# main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    args = argv[1:]
    if not args:
        print(json.dumps({
            "error": "usage: reality_gate.py <check|verify|init-status> ...",
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

    print(json.dumps({"error": "unknown subcommand: %s" % subcommand}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
