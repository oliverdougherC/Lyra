# Learning quality follow-up — September 7, 2026

> Dated workstream evidence. The implementation is integrated; these source hashes, results and
> earlier handoff statements remain historical. See the [current integration ledger](../release-evidence.md).

Follow-up to reviewed PR #80 at `2ee865c0da52c94c1f5aa056ea64a2e2de513e4e` and
[review 5128345583](https://github.com/oliverdougherC/Lyra/pull/80#pullrequestreview-5128345583).
The reviewed head is unchanged. This branch is stacked on it for a focused code diff.
No merge, release, or support-policy change is authorized by these results.

## Causal findings

1. **Execution success was not equality.** The retained direct-answer call used lowercase
   `e`, which is a legitimate symbol in the calculator. The CAS returned `equal:false`;
   that full result reached the model. The model's claim of exact verification was false.
   The tool description omitted exponential notation and emphasized the generic execution
   flag. The follow-up documents `exp`/`E` and attaches computed matched/mismatched/unresolved
   meaning without changing any raw outcome or reinterpreting a symbol.
2. **The solver could accept a contradiction.** A successful calculator execution plus
   model `agrees` could produce `verified` even with `equal:false`. The application now
   returns `uncheckable` for such a transcript. This is not an automatic claim that the
   student's solution is wrong. Ordinary tool errors followed by successful retries still
   work. A legitimate correction after a contradictory comparison needs a fresh coherent
   verification; the application does not guess which earlier check was superseded.
3. **The card schema required an unused field.** `FLASHCARDS_SCHEMA` required `topic`,
   while its prompt requested fronts/backs and persistence used the already-selected topic.
   The failed output attempted to close an object without the unseen required field and
   continued as whitespace until the cap. In a fresh controlled replay, changing only that
   schema produced valid JSON with identical messages/configuration. The redundant field
   is removed and the prompt now names the actual payload. The full deck still requires
   independent content review; a successful diagnostic call is not deck acceptance.
4. **Attempt scope needed clearer instructions.** Guide now explicitly names the conditions
   under which partial work is valid and stops after the requested diagnosis. This is a
   narrow instruction refinement, not evidence that history was dropped: the production
   planner delivers the original problem, attempt and latest request.
5. **Conceptual reliability is separate.** The osmosis request/history arrives intact with
   no retrieved sources in this corpus case. The model adds an incorrect equilibrium claim.
   No application evidence-loss cause was found. Changed answers after unrelated tool-schema
   changes do not establish a scientific-quality repair.

## Configuration and review policy

Only the already-authorized endpoint was used, with isolated nonsecret settings, null Keychain,
synthetic data and serialized provider windows coordinated with writing. Its model-list operation
advertises only Qwen3.8-27B. No alternative authorized configuration is known; no paid service
was added and the normal endpoint was never changed. See [configuration inventory](configurations.json).

A protocol-compatible configuration is not automatically a supported beta-quality configuration.
The matrix distinguishes bounded observed passes, failures and untested configurations;
Oliver owns support policy. Independent-agent judgment and actual human acceptance remain
separate. Human review is not run. Existing corpora/rubrics and historical evidence are unchanged;
two held-out variations test conditional division and an actual false equality.

## Reproduction

Use the existing `eval_tutor.py` and `eval_study.py`, a separate authorized configuration database,
and a new output/profile directory for every repeat. Set `PYTHON_KEYRING_BACKEND` to
`keyring.backends.null.Keyring` and all `LYRA_DATA_DIR`, `LYRA_CACHE_DIR`, `LYRA_LOGS_DIR`,
`LYRA_MODELS_DIR` paths to isolated evaluation directories. Provider calls are serial.

```bash
uv run python scripts/eval_tutor.py run --surface class_chat \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/guide-1" \
  --case just-tell-me-the-answer --case attempt-where-did-i-go-wrong \
  --case dont-ask-me-questions-teach-it --case show-convolution-worked
uv run python scripts/eval_tutor.py run --surface class_chat \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/osmosis-1" \
  --corpus scripts/eval_corpora/tutor_beta_heldout.json --case simpler-osmosis
uv run python scripts/eval_tutor.py run --surface class_chat \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/transfer-1" \
  --corpus scripts/eval_corpora/tutor_learning_followup.json
```

Repeat into `*-2` directories, preserving all failures. The previous reviewed worktree supplies
an unchanged production baseline. [Study diagnostics](study-quality-followup.md) retain the
single-variable schema comparison and full-deck rerun recipe. Per-case results are retained in the [Guide independent review](guide-independent-review.md),
[study independent review](study-independent-review.md), and [solver verdict replay](solver-verdict-replay.md).
Exact final production source is `19b94fbfeac16fda05ba8fa5459a6ef205b4ea75`.


## Final results and remaining outcomes

| Scope | Before → after | Status |
| --- | --- | --- |
| Direct ODE verification | False symbolic check with exact-verification claim2/2 → actual matched CAS and correctly scoped claim2/2 | Observed pass in final configuration |
| Eigenvalue/Show derivations | Incorrect eigen polynomial2/2; Show already passed → correct final eigen/Show2/2 | Observed pass, not universal proof |
| Partial-attempt feedback | Prior error diagnosis/credit issues → final text still falsely says taking a constant factor outside requires the entire integrand to be constant2/2 | **Failing**; correct supplied integrals do not repair the prose |
| Osmosis | Fresh baseline avoids the old guarantee; intermediate output falsely guarantees equal concentration; final has no explicit critical error2/2 | Variable model behavior; no causal scientific-repair claim |
| Held-out transfer | Division loses zero; false polynomial identity | Both pass2/2, including actual `equal:false` respected in the answer |
| Ecology deck | Prior483.488s failure and164s cancellation; fresh oldschema replay fails43.868s → schema-only same request succeeds26.257s; full final decks Ready14cards in139.185/138.228s | Independent critical content pass, qualification notes remain; each deck recovers one token-limit failure |
| Solver confidence | Replay18verified/10uncheckable →14verified/14uncheckable | Four correct floating-to-rational retries lose verified status; deliberate conservative policy cost, not bad answers |

No difficult case or historical failure was removed, and the rubric was not weakened. The
partial-attempt instruction refinement has **not** fixed that model's incorrect rule. No
alternative configuration was available for an authorized comparison. Oliver should decide
support scope from this evidence; a failing configuration is not a universal verdict on Lyra.

## Small human-review set

Actual human acceptance remains **not run**. Review these limited sets before considering
any broader acceptance; passing them alone does not certify the entire product:

1. Four selected Guide items in the [Guide review worksheet](guide-independent-review.md):
   actual verification scope, failed partial feedback, osmosis qualification and worked derivation.
2. Six exact cards with sources in [study human review](study-human-review.md), including
   migration/assumption wording. Full decks are linked so unselected cards remain inspectable.
3. One representative floating-to-rational retry in [solver replay](solver-verdict-replay.md)
   to assess the conservative confidence tradeoff. All four affected histories remain available.

## Software and native evidence

- Final source: `uv run pytest backend/tests -q` — **3194 passed,1 skipped**.
- Frontend unchanged: typecheck, lint and build passed. No new dependency or evaluation framework.
- A2048-token-window regression exposed extra schema overhead; compact equivalent wording
  restored the original1055-token schema cost without changing any budget or test boundary.
- PyInstaller/Tauri rebuilt, stable development signing and native architecture/deployment
  checks passed; signed frozen authenticated smoke passed. [Native receipt](native.json)
  records the bundle digest and externally observed app/backend selectors/startup/termination.
- **Native isolation limit:** runtime subsequently verified that WebKit's default data store
  and app-ID IPC are shared. Prior direct-Popen receipts prove backend selectors and process
  behavior only, not whole-native isolation or no-impact. No CUA/LaunchServices was used here;
  rendered UI, human acceptance and whole-native isolation are not established. Further
  production-identity native tests require an isolated account/device and no incumbent IPC.

## Integrated candidate handoff

Keep reviewed PR80 unchanged. Integrate this delta only after owner review and fresh-base
checks, reconciling writer prompt sections. Repeat the same Guide cases/held-out cases and
full ecology corpus at the combined immutable source and the same separately authorized
configuration. Retain internal retries as well as terminal artifacts, and obtain actual
human review. The current source-process results are not combined-tree or packaged semantic
acceptance. No merge, publication, or beta support-policy change was performed.
