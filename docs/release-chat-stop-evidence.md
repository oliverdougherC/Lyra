# Stop recovery race — CI follow-up

The full-stack acceptance failure on `b173199` (Actions run `34008924629`, job `101421153926`) was a frontend lost-send race. The retained Actions trace contains one initial agent POST and one Stop POST, both returning HTTP 200 with stopped outcomes. It contains **no second agent POST**. The test filled “Can you continue now?” immediately after cancellation settled; the final screenshot shows an empty composer and only the stopped first turn.

`ChatPane` exposed Send as soon as `turnOutcome` became stopped, while `pendingTurn` remained populated until its awaited transcript refresh finished. `send()` cleared the draft and updated the submitted-text bookkeeping before `runTurn()` rejected that still-pending turn. Thus the next message disappeared without reaching the backend. The failing revision changed prompts/native confirmation, not this longstanding timing-sensitive path.

The repair uses the existing `showingTurn` predicate consistently: the composer remains blocked while its old optimistic turn settles, and `send()` rejects that state **before** touching the draft or operation ID. There are no backend cancellation changes, timeout increases, retries, request filters or weakened assertions.

A deterministic component regression holds the stopped transcript refresh at a promise barrier. It failed before the repair because the composer was already enabled. After releasing the barrier, it verifies that the immediate next Send calls the agent endpoint exactly once with the follow-up text.

Verification:

- `frontend/tests/chat-pane.test.tsx`: **42 passed**.
- Frontend typecheck, targeted ESLint and Prettier passed.
- The unchanged real-stack `ambiguity-recovery.spec.ts`: **8 passed**, including Stop and immediate reuse, operation idempotency, lost-response recovery and busy-409 handling. Teardown reported no owned process-group members or unconsumed backend failures.
- The real-stack check used a temporary archive of `b173199` with only this repair, its own Python 3.12 environment, independent production frontend output, ports 18144–18146 and disposable application data. `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` was verified and inherited by the backend child before imports; OS Keychain was not available to the run.

PR 73 run `34006192578` failed a different test, the unavailable-original download response wait. Its trace was handed to the native acceptance owner; it is not claimed fixed here. The release owner owns committing this shared frontend repair into both PR stacks, rerunning required CI and rebuilding the desktop candidate.
