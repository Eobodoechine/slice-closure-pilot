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
| `definition-present` | the pinned name is a real `def`/`async def`/`class` binding in the committed tree, parsed with `ast` |
| `substring-present` | the pinned string occurs somewhere — **including in a comment or docstring** |
| `test-passes` | `test_cmd` exited 0 |

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

`expect_definition` parses the committed blob and requires a real binding. Verified
against both live proof branches:

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

- **Python only.** A non-`.py` file returns `definition-unsupported-language` and fails
  closed rather than falling back to a fuzzy text match — fuzziness is the defect being
  closed. Adding a language means adding a parser, not a regex.
- Unparseable Python fails closed (`definition-unparseable`).
- Without `expect_file`, every `.py` path the commit touched is parsed. With it, only
  that path — which is what `expect_file` is genuinely good for once the check is
  AST-based.
- The guarantee is still bounded: the gate is invoked by the party it constrains, so it
  defends against hallucinated completion, not a determined forger.

## Tests

```bash
python3 -m pytest tests/ -q
```

`tests/test_gate_hollow_slice.py` holds the negative cases — the verdicts the gate must
**refuse** to give. Before it existed, every assertion in the rig checked a verdict the
gate *does* produce, so a gate that could never fail would have passed the whole suite.

It also keeps one deliberate characterization test asserting that `expect_substring`
*does* green-light a hollow slice. That test documents the hole rather than hiding it;
if anyone hardens the substring path, it fails loudly and forces this README to be
updated with it.

**Fixture rule**, learned from `t3-hollow` defeating itself: a hollow fixture's own text
— comments, docstrings, commit message — must not accidentally satisfy the assertion
under test. The suite assembles the pinned symbol at runtime so its own source never
contains the literal string.
