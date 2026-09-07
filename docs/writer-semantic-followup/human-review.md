# Human review — semantic follow-up

Source: `e2857f61cbe04b9bde533419b0f393bcaa1dfa11`. **All human decisions remain unchecked.** The independent agent reviewed 13 observations: 11 source runs and two frozen reruns of the same two unique heldouts. This is not four heldout cases.

Read the linked raw receipt and [independent findings](independent-review.json) against the existing [corpus/rubric](../writer-quality-rubric.md). Scores are separate dimensions; no average is an acceptance gate.

| Case / scenario | State | P / E / R / review / I / voice | Human accept / revise / reject |
|---|---|---|---|
| [targeted: pass_targeted_causal_precision (uninterrupted)](targeted/pass_targeted_causal_precision--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [targeted: pass_reject_empty_and_unrelated_rewrite (uninterrupted)](targeted/pass_reject_empty_and_unrelated_rewrite--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [targeted: source_history_replace (uninterrupted)](targeted/source_history_replace--uninterrupted.json) | completed | — / 3 / 4 / — / 3 / 4 | [ ] / [ ] / [ ] |
| [targeted: source_history_delete (uninterrupted)](targeted/source_history_delete--uninterrupted.json) | completed | — / 4 / 3 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [targeted: pass_multisection_same_model_recovery (uninterrupted)](targeted/pass_multisection_same_model_recovery--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [controls: full_live_draft_from_student_notes (uninterrupted)](controls/full_live_draft_from_student_notes--uninterrupted.json) | failed | — / 4 / — / — / 1 / 3 | [ ] / [ ] / [ ] |
| [controls: full_live_draft_from_student_notes (restart_review)](controls/full_live_draft_from_student_notes--restart_review.json) | completed | — / 3 / — / — / 3 / 3 | [ ] / [ ] / [ ] |
| [controls: full_live_draft_from_student_notes (edit_cancel_retry)](controls/full_live_draft_from_student_notes--edit_cancel_retry.json) | failed | — / 3 / — / — / 2 / 3 | [ ] / [ ] / [ ] |
| [planning: full_live_draft_from_student_notes (uninterrupted)](planning/full_live_draft_from_student_notes--uninterrupted.json) | completed | 3 / 2 / — / — / 3 / 3 | [ ] / [ ] / [ ] |
| [heldout: holdout_museum_label_revision (uninterrupted)](heldout/holdout_museum_label_revision--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [heldout: holdout_seed_notebook_historical_method (uninterrupted)](heldout/holdout_seed_notebook_historical_method--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [frozen-heldout: holdout_museum_label_revision (uninterrupted)](frozen-heldout/holdout_museum_label_revision--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |
| [frozen-heldout: holdout_seed_notebook_historical_method (uninterrupted)](frozen-heldout/holdout_seed_notebook_historical_method--uninterrupted.json) | completed | — / 4 / 4 / — / 4 / 4 | [ ] / [ ] / [ ] |

Focus the review on: historical-revision attribution; supported 18/14 and exact methods; preservation of personal stake; unsupported population/method claims; a concrete next step; and whether incomplete output is useful. The two failed full-live runs are incomplete deliveries after inference, not successful quality results or preflight safety refusals.

Human reviewer: __________  Date: __________  Findings / owner decision: __________
