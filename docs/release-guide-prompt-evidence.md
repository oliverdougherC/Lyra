# PLA-461 targeted Guide prompt repair

The live Linear issue and its full current description were read on 2026-09-05; it had no comments. The baseline production `class_chat` run is retained under `docs/release-provider-evidence/guide-run-1`; its same-model Qwen3.8-27B grade was 11/13. The five-case repeat under `guide-run-2` graded 4/5. Both baseline first-step answers gave the full solved mixing ODE and boxed result after the student asked only how to start. The simplification answer repeated the formal integral and symbol definitions; it failed the first grading and passed the repeat despite identical answer bytes, demonstrating judge variance.

The targeted Guide wording in `backend/llm/prompts.py` clarifies two existing requirements:

- Getting-started help supplies one concrete first move and useful setup, stopping before the remaining solution unless the student requests that scope.
- Simpler explanations reduce abstraction and steps, using a familiar picture or small example instead of repeating the formal definition that caused difficulty.

This is a general prompt instruction. There is no request keyword filter, fixture-specific ODE example, answer post-processing, grade threshold change or corpus change. Direct conceptual explanations, explicit answer requests and Show retain their existing instructions. Contract version remains 2 because this strengthens the proportionality behavior already required by the unchanged version-2 corpus.

Baseline prompt SHA-256: `d8566f770af83455d416e9d60c00e486bd6ce74417f9b7bcc0a39aa014bbcefb`.

Intermediate expanded prompt SHA-256: `8474bbc233c104fd74b1e5dc0c841a5c37cc830030f0e4b8dac45d8ad9312412`. That version passed the first-step model case but exceeded existing small-context budget fixtures. Its retained critical run graded 4/5: the simplification answer was now a plain echo analogy without the original formal integral, but failed the unchanged overlap/sliding-window criterion.

Intermediate compact prompt SHA-256: `92c8e5f20f153f9bdfdfe4cdb5a6d16cb853af9654e1f507c1ecefbcce313a80`. The Guide text is 968 characters, below the baseline 1,091, preserving the existing request capacity.

Two prompt/production-assembly wiring regressions failed before the edit. After the intermediate compact wording, **284 tests passed** across `test_prompts.py`, `test_eval_tutor.py`, `test_tutor_chat_safety.py`, `test_api_agent_chat.py`, `test_api_chat.py`, `test_api_agent.py` and `test_tools_loop.py`, including the existing small-context boundary checks. Ruff and diff checks passed. These tests prove wiring and guard existing behavior; they do not establish real-model teaching quality.

The acceptance lane is rerunning the critical five and full thirteen unchanged cases through fresh production planner/tool-loop processes with the configured Qwen3.8-27B endpoint. It owns the exact runtime metadata, transcripts and judgments. A same-model grade is supporting evidence, not independent human acceptance or packaged-candidate verification.

The intermediate compact critical run is retained under `guide-compact-critical`: **4/5** under the unchanged same-model grading. First-step help, the partial attempt, answer checking and Show passed. Simplification received 2/2 in all seven dimensions and omitted the previous integral/definitions, but still failed the fixture's specific overlap/sliding-window criterion. This failure is retained, not reclassified as a pass. Full-corpus and held-out runs on this exact compact prompt are owned by the acceptance lane; independent human review and rubric reconciliation remain outstanding.


## No-question instruction repair

The compact candidate's full thirteen-case run graded **11/13**, exposing a real additional failure: after “Don't ask me questions; teach it,” the answer ended with an unsolicited “Want me to…” question. Its two held-out cases passed, but that did not waive the explicit-request failure. All intermediate artifacts remain retained.

The next wording explicitly honors requests for no questions, including closing questions and follow-up offers. Simplification now explains the same concrete mechanism in plain words instead of substituting a new analogy. Neither change names a corpus topic or changes the rubric. The Guide block is **1,090 characters versus 1,091 at baseline**, so it retains the existing small-context capacity.

No-question candidate SHA-256: `a997cf7dfe16aeb7827cef08b91ad0345598b17c33c89f27226412f1506657d3`.

Three added or extended prompt/production-assembly wiring checks failed before these final changes. **285 tests passed** afterward across the same seven prompt/evaluation/tutor/API suites, including the existing budget boundaries. Ruff and diff checks passed. Acceptance has the frozen hash for fresh critical-six, full-thirteen and held-out-two runs; no human or final-provider pass is claimed here.


## Final bounded scope and verification repair

The `a997` critical-six run graded **4/6**. It obeyed the no-question request and simplified the convolution mechanism, but regressed to a complete ODE solution for initial guidance and introduced an incorrect eigenvector equality after checking only eigenvalues. Those are actual failures; the run and all earlier evidence are retained. Its automatic full run was stopped before evaluation rather than represented as a full pass.

The final scoped change makes the **latest/current request** authoritative over earlier requests and over extra information returned by tools. The class-agent capability prompt now requires relevant tool results, limits verification claims to what was actually checked, and requires checking additional worked-example arithmetic. It does not add a topic-specific example, filter output, alter tool permissions, or weaken grading.

Frozen source hashes:

- `backend/llm/prompts.py`: `99383ff71f3d187ff79a3870522fcc52de7cd5db43df4d103c71cf9c1965bdd3`
- `backend/api/routes_agent_chat.py`: `32d2036ed8179695b00b743e890f813dd31cd470bc902bb6c12da841dd619325` (class-agent prompt constants only)

Guide length is **1,089 characters**, below baseline. Two extended wiring checks failed before the final patch; afterward **285 tests passed**, including small-context boundary tests. Ruff and diff checks passed. Acceptance is running critical-six first against both frozen source hashes. If the configured provider still fails critical teaching quality, that remains a genuine release-quality blocker; further benchmark-specific prompt tuning is not authorized.

Final scoped critical-six result (`guide-scope-critical`): **4/6**, with the unchanged rubric and same-model judge. The first-step response stopped at the ODE setup; explicit no-question teaching was obeyed; the replacement eigenvector arithmetic was correct. Two failures remain: simplification still misses the fixture's specific overlap/sliding-window requirement, and feedback on a partial attempt does not acknowledge the interval where the student's proposed bounds are valid. The Show case passed the judge but received correctness 1/2 for an inconsistent displayed general integration bound despite the correct final piecewise answer. These limitations are retained as configured-provider quality evidence, not declared resolved. No additional full-corpus run or further prompt tuning was undertaken after this final bounded attempt. Independent human acceptance remains outstanding.
