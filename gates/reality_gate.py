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

Subcommands:
  check --repo <path> --commit <hash|HEAD>
        [--expect-substring <str>] [--expect-file <relpath>]
        [--test-cmd <cmd>] [--test-timeout N]
      Read-only. Runs ground-truth checks and prints a JSON result. Writes nothing.
      Imposes no binding-check requirement. Exit 0 iff passed, else 1.

  verify --status-json <path> --item <id-or-exact-title>
         --repo <path> --commit <hash|HEAD>
         [--expect-substring <str>] [--expect-file <relpath>]
         [--test-cmd <cmd>] [--test-timeout N] [--log <path>] [--now <iso8601>]
      Runs the SAME checks as `check`, then -- iff they pass -- atomically marks the
      located item verified. REQUIRES at least one binding check (--expect-substring
      and/or --test-cmd). Exit 0 on pass-and-write, 1 on check-fail-and-downgrade,
      2 on usage/lookup/bad-status errors.

  init-status --path <path> --product <name> --done <sentence>
      Writes a status.json skeleton (empty items) iff --path does not exist. Never
      clobbers an existing file (exit 2). Exit 0 on create.

Exit codes:
  0  passed (check) / verified written (verify) / created (init-status)
  1  check failed (check/verify): a requested check did not pass
  2  usage error, item not found/ambiguous, bad status.json, or existing init path.
     Named reason tokens appear in the stdout JSON: "no-binding-check",
     "test-timeout", "bad-status-json".
"""
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
               test_timeout: int) -> Dict:
    """Run the ground-truth checks and return the result dict:
      {"passed": bool, "commit": <sha-or-None>,
       "checks": {"commit-is-real": bool, "substring-present": bool/None,
                  "test-passes": bool/None},
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

    test_check: Optional[bool] = None
    if test_cmd is not None:
        test_check, test_reason = _test_passes(repo, test_cmd, test_timeout)
        if test_reason is not None:
            reasons.append(test_reason)
        elif test_check is False:
            reasons.append("test-failed")

    ran = [v for v in (commit_real, substring_check, test_check) if v is not None]
    passed = all(ran)

    return {
        "passed": passed,
        "commit": sha,
        "checks": {
            "commit-is-real": commit_real,
            "substring-present": substring_check,
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
    expect_file = opts.get("expect-file")
    test_cmd = opts.get("test-cmd")

    if expect_file is not None and expect_substring is None:
        return _usage_error("--expect-file requires --expect-substring")

    try:
        test_timeout = int(opts.get("test-timeout", DEFAULT_TEST_TIMEOUT))
    except ValueError:
        return _usage_error("--test-timeout must be an integer")

    result = run_checks(opts["repo"], opts["commit"], expect_substring,
                        expect_file, test_cmd, test_timeout)
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
    expect_file = opts.get("expect-file")
    test_cmd = opts.get("test-cmd")
    log = opts.get("log")
    now = opts.get("now")

    if expect_file is not None and expect_substring is None:
        return _usage_error("--expect-file requires --expect-substring")

    # Binding requirement (SECURITY): commit-is-real alone can never verify.
    if expect_substring is None and test_cmd is None:
        print(json.dumps({
            "passed": False,
            "error": "no-binding-check",
            "reason": "no-binding-check: verify requires --expect-substring "
                      "and/or --test-cmd",
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
                        expect_file, test_cmd, test_timeout)

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
