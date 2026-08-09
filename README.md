# slice-closure-pilot

Throwaway repo for the Slice Closure Gate pilot.

## What the gate asserts

`.github/workflows/slice-closure-gate.yml` runs on every PR to `main`. It loads
`gates/contract.yml` and `gates/reality_gate.py` **from the base branch**, refuses any
PR that touches `gates/**` or `.github/workflows/**`, and runs the base-pinned
assertions against the PR head.

A verdict of `passed: true` means every check that ran passed:

| check | what it actually proves |
|---|---|
| `commit-is-real` | the commit has a non-empty diffstat (not `--allow-empty`) |
| `definition-present` | **this change introduced** the pinned name as a **module-level** `def`/`async def`/`class`, parsed with `ast` |
| `substring-present` | the pinned string occurs somewhere — **including in a comment or docstring** |
| `test-passes` | `test_cmd` exited 0 |

### What `definition-present` requires, exactly

The symbol must be a **module-level** `def`/`async def`/`class` in **production
code** at the head commit, and **absent** from production code at the **merge base**.

It compares whole-tree presence at two commits. It does *not* diff paths. Per-path
range diffing was the wrong primitive and leaked twice: an unrebased branch whose
base had renamed the defining file scored that file's stale copy as newly
introduced, and a pure rename (0 insertions, 0 deletions) counted as landing the
symbol. Tree presence has no such edge — if it existed anywhere in production code
at the merge base, this change did not land it.

The **merge base**, not the base tip. `base..head` is a tree-to-tree diff; what a PR
proposes is `base...head`. With the tip, a base commit that *deletes* the definer
makes a stale branch's leftover copy look new, and it verifies green.

Not satisfied by: a method on a class, a def nested in another function, a def under
`if False:` or `if TYPE_CHECKING:`, a module-level `def` later `del`-eted, or a
definition living only in `tests/`, `__tests__/`, `docs/`, `examples/`, `conftest.py`,
`test_*.py`, or `*_test.py`.

Consequences worth knowing before you hit them:

- **CI must pass `--base-ref`.** The workflow does. Without it the gate falls back to
  the tip commit's parent, so a multi-commit PR is judged on its last commit alone.
  Both behaviours are pinned by tests.
- **A method is not the symbol.** `class Validator: def foo` does not satisfy
  `expect_definition: foo`. Pin the class name if the class is the deliverable. A
  deliberate spec decision, not an oversight.
- **Shipped packages named `testing/` are production code.** Only `tests/`,
  `__tests__/`, `docs/`, `examples/` directories are excluded, plus pytest's own
  collection patterns. An earlier revision excluded any segment named `test` or
  `testing` and wrongly blocked `src/testing/factories.py` — a real layout
  (`django.test`, pytest plugins).
- **Once a slice has landed, re-running its contract blocks** with
  `definition-preexisting`. Concretely: the merged builder commit `de3ddcb` blocks
  against base `2bb1558`, because `2bb1558` had already landed the symbol. For a
  slice-*closure* gate that is the intended reading — the slice was already closed.
- **Everything unreadable fails closed.** An unparseable base blob yields
  `definition-unparseable`, not "absent, therefore new". A malformed `--base-ref`
  (empty, or starting with `-`) is rejected outright: a leading-dash value was
  otherwise parsed by git as an option, and `--output=` really did create a file.
- **Known limit:** a decorator that rebinds the name (`@_kill def foo` leaving
  `foo is None`) still passes. The check is "a module-level `def`/`class` statement
  binds this name", which is decidable; "importing the module yields a callable" is
  not, and no sound static rule separates a nulling decorator from `@functools.cache`
  — which this suite requires to pass. Note the gate *does* already execute head-tree
  code via `test_cmd`, so this is a choice about what the check means, not a
  limitation of what the gate is able to run.

## Why the contract pins `expect_definition`, not `expect_substring`

`expect_substring` is a raw text match. It cannot tell "the symbol was defined" from
"the symbol was mentioned", so a commit that defines nothing still passes as long as
some added line contains the string.

This is not hypothetical — it happened here. Branch `pilot/t3-hollow` was written to be
**blocked** and the gate **passed** it. Its only line containing the pinned string was
its own docstring, saying the file *"deliberately does NOT contain the contract-pinned
symbol `def validate_pilot_input`"*. The disclaimer satisfied the check it was
disclaiming, and the run went green.

**`expect_file` is not a fix for this.** It narrows *which* text is searched and then
still does a raw `in` against the blob, so a comment in the pinned file satisfies it
exactly as easily. That is pinned as a test
(`test_expect_file_does_not_rescue_the_substring_check`) so the belief can't quietly
return.

`expect_definition` parses the committed blob and requires a real binding that this
change introduced. Verified against both live proof branches:

```
pilot/t3-hollow      definition-present: false  test-passes: true   -> BLOCK
pilot/positive-slice definition-present: true   test-passes: true   -> PASS
```

Note the hollow branch has a **green test suite** and is still blocked. That is the
point: when one agent writes both the slice and its tests, hollow code gets hollow
tests, so `test_cmd` alone carries less weight than it appears to.

`expect_substring` is still supported, for assertions that genuinely are about text —
a version string, a config value. Never for "this symbol exists".

### Scope and limits

- **Python only.** A non-`.py` file simply yields no candidates and the check reports
  `definition-absent` — it never falls back to a fuzzy text match, because fuzziness is
  the defect being closed. Adding a language means adding a parser, not a regex.
- Unparseable Python fails closed (`definition-unparseable`), on either side.
- `expect_file` scopes the **head** search to one path. It deliberately does *not*
  scope the base lookup: a symbol that merely moved into the pinned path was not landed
  by this change.
- Candidate files are enumerated **from the tree** (`ls-tree` + one batched `cat-file`),
  never with `git grep`. `git grep <tree-ish>` reads `.gitattributes` from the *working
  tree* — which in CI is the PR head — so a PR could ship two lines marking the base's
  files `binary` and make an already-landed symbol look new. The byte pre-filter is the
  weakest one that cannot produce a false negative (`def` or `class` appearing at all);
  anything narrower re-creates a pre-filter/AST divergence, and at the base that is a
  fail-open.
- All merge bases are consulted (`merge-base -a`). A criss-cross history has more than
  one, and picking the arbitrary one lets an already-landed symbol look new.
- A shallow clone reports `definition-shallow-repo` rather than a generic unreadable —
  the symptom is "the gate says every slice is hollow" and the cause is checkout depth.
  The workflow's base fetch deliberately does **not** use `--depth=1`, which would
  shallow-ify a `fetch-depth: 0` checkout and block every PR.
- The guarantee is still bounded: the gate is invoked by the party it constrains, so it
  defends against hallucinated completion, not a determined forger.

## Tests

```bash
python3 -m pytest tests/ -q
```

`tests/test_gate_hollow_slice.py` holds the negative cases — the verdicts the gate must
**refuse** to give. Before it existed, every assertion in the rig checked a verdict the
gate *does* produce, so a gate that could never fail would have passed the whole suite.

It also keeps deliberate characterization tests asserting behaviour that is *not* ideal
but is real: that `expect_substring` does green-light a hollow slice, and that decorator
rebinding passes the definition check. Those document limits rather than hiding them; if
anyone changes either, the test fails loudly and forces this README to be updated with it.

Two independent verifier passes shaped this file. The first found **eight false-pass
classes** against the original check — `if False:`, nested defs, `TYPE_CHECKING`,
methods, test-only definitions, touching a file that already defined the symbol,
`del` after def, and decorator rebinding — plus a false negative on non-ASCII
filenames. The second defeated the *fix*, finding five more: a stale branch plus a
rename on the base, a pure rename, an unparseable base blob read as "absent", an
empty `--base-ref`, and git option injection through that flag. **All are now blocked
except decorator rebinding**, which is documented above as a deliberate limit. Each
has a regression test.

Every one of those tests was mutation-checked — and mutation testing repeatedly earned
its place. It caught that the stale-branch test passed whether or not the merge base
was used, so it never actually pinned that fix; `test_merge_base_not_base_tip` exists
because of that, and constructs the one shape that discriminates.

A third pass then defeated *that* revision: a PR-supplied `.gitattributes` hid the base
from the scan, multiple merge bases let an arbitrary one be chosen, a backslash-joined
definition slipped the regex pre-filter, and the workflow's own `--depth=1` fetch could
block every PR. The scan is now tree-enumerated, which removes that class rather than
patching it.

`test_a_plain_new_definition_is_found` is the most important test here despite being
the most boring. The candidate pre-filter is a `git grep -E` regex, and **POSIX ERE
does not support `\b`** — a revision using it matched nothing, exited 1
indistinguishably from "no candidates", and silently blocked *everything*. Every
adversarial test still passed, because they all expect a block. Only a positive
control separates "correctly refusing" from "broken and refusing everything".

**Fixture rule**, learned from `t3-hollow` defeating itself: a hollow fixture's own text
— comments, docstrings, commit message — must not accidentally satisfy the assertion
under test. The suite assembles the pinned symbol at runtime so its own source never
contains the literal string.

## Phase 0 probe marker (name-collision probe, read-only)

This line is the README-only change shipped with the Phase 0 name-collision probe.
It defines nothing; the honest gate under the base contract reports
`definition-absent` and goes red as designed.
