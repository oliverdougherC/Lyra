# Synthetic visual evidence

These before/after captures support the September 6 usability PR. They are development/browser
captures, not physical-input, real-provider, final installed-candidate or first-time-human passes.
Only synthetic text, filenames, commands and endpoints appear.

| Surface | Before | After | Boundary |
| --- | --- | --- | --- |
| Solver | [before](solver-before-light.png) | [after](solver-after-light.png), [dark](solver-after-dark.png) | Full app, intercepted API; correction/save/reload in `solver-readability.spec.ts` |
| Settings | [before](settings-before-light-remote-ack-top.png) | [after](settings-after-light-remote-ack-top.png), [narrow dark](settings-after-dark-narrow-remote-ack-top.png) | Full app, synthetic Settings and native stubs |
| Activity/brief | [before](activity-before-dark-narrow.png) | [after](activity-after-dark-narrow.png) | Actual components in a narrow synthetic harness |
| Files | [before](files-before-375.png) | [after](files-after-375.png) | Full app, 100 long names, 375px; `list-navigation.spec.ts` |
| Work | [before](work-before-light-wide.png) | [after](work-after-light-wide.png) | Actual components, 105 synthetic artifacts |
| Practice | [before](practice-before-dark-narrow.png) | [after](practice-after-dark-narrow.png) | Actual components, narrow dark fixture |

Full local screenshot sets and runnable capture scripts are retained in `output/playwright` and
`frontend/output/playwright/practice` in the implementation worktree. Reproducible full-app
regressions are checked in under `frontend/e2e`. See [acceptance register](../beta-usability-acceptance.md)
for physical and unfamiliar-user protocols, limitations, and ownership.
