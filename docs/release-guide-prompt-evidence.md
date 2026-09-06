# PLA-461 targeted Guide prompt repair

The live Linear issue and its full current description were read on 2026-09-05; it had no comments. The baseline production `class_chat` run is retained under `docs/release-provider-evidence/guide-run-1`; its same-model Qwen3.8-27B grade was 11/13. The five-case repeat under `guide-run-2` graded 4/5. Both baseline first-step answers gave the full solved mixing ODE and boxed result after the student asked only how to start. The simplification answer repeated the formal integral and symbol definitions; it failed the first grading and passed the repeat despite identical answer bytes, demonstrating judge variance.

The targeted Guide wording in `backend/llm/prompts.py` clarifies two existing requirements:

- Getting-started help supplies one concrete first move and useful setup, stopping before the remaining solution unless the student requests that scope.
- Simpler explanations reduce abstraction and steps, using a familiar picture or small example instead of repeating the formal definition that caused difficulty.

This is a general prompt instruction. There is no request keyword filter, fixture-specific ODE example, answer post-processing, grade threshold change or corpus change. Direct conceptual explanations, explicit answer requests and Show retain their existing instructions. Contract version remains 2 because this strengthens the proportionality behavior already required by the unchanged version-2 corpus.

Baseline prompt SHA-256: `d8566f770af83455d416e9d60c00e486bd6ce74417f9b7bcc0a39aa014bbcefb`.

Intermediate expanded prompt SHA-256: `8474bbc233c104fd74b1e5dc0c841a5c37cc830030f0e4b8dac45d8ad9312412`. That version passed the first-step model case but exceeded existing small-context budget fixtures. Its retained critical run graded 4/5: the simplification answer was now a plain echo analogy without the original formal integral, but failed the unchanged overlap/sliding-window criterion.

Final compact prompt SHA-256: `92c8e5f20f153f9bdfdfe4cdb5a6d16cb853af9654e1f507c1ecefbcce313a80`. The Guide text is 968 characters, below the baseline 1,091, preserving the existing request capacity.

Two prompt/production-assembly wiring regressions failed before the edit. After the final compact wording, **284 tests passed** across `test_prompts.py`, `test_eval_tutor.py`, `test_tutor_chat_safety.py`, `test_api_agent_chat.py`, `test_api_chat.py`, `test_api_agent.py` and `test_tools_loop.py`, including the existing small-context boundary checks. Ruff and diff checks passed. These tests prove wiring and guard existing behavior; they do not establish real-model teaching quality.

The acceptance lane is rerunning the critical five and full thirteen unchanged cases through fresh production planner/tool-loop processes with the configured Qwen3.8-27B endpoint. It owns the exact runtime metadata, transcripts and judgments. A same-model grade is supporting evidence, not independent human acceptance or packaged-candidate verification.
