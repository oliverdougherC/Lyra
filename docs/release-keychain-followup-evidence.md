# PLA-462 delayed Keychain read follow-up

Reviewed baseline: PR #73 c0d991091d6bf9ea305e21ddff3ed67a8089690a,
inherited by PR #74 06f9a874b06e3ec9203a4644b9eca1530e89a2d9.

An unfinished healthy legacy tutor or Exa read was caught as unavailable storage,
permanently demoting the shared storage flag and returning an absent fallback as no key.
The worker's eventual result was lost. Cold availability probing had the same problem.

The repair distinguishes pending reads from missing credentials and unavailable storage.
One bounded worker retains a capped set of pending per-key results for retry, including
interleaved tutor/Exa requests. Mutations retire prior read deliveries; existing durable
revocation/file authority still wins. Reads use one actual lookup rather than a redundant
availability probe followed by another lookup. A synchronous read that exceeds its deadline
also remains pending. Explicit writes retain bounded durable fallback behavior.

The initial before-repair production-route set had **11 failures and 8 passes**;
the final expanded 24-case before-repair set had **16 failures and 8 passes**.
Warm/cold legacy and UUID route tests cover models, connection, tools and vision with
synthetic authentication assertions; Exa uses its actual settings route and an injected
adapter with a healthy delayed lookup. Additional tests cover interleaved delivery,
denied storage, late deletion, and short synchronous deadlines. These use isolated fake
stores, never the owner's Keychain. Final focused/full results and exact remote CI are
recorded in the PR handoff and review-candidate receipts.

A pending read reports an actionable retry response rather than claiming the key is absent;
retry consumes the completed result without requiring the user to re-save it. This does
not certify a real locked/denied macOS prompt or erase the earlier credential incident.

Final focused verification: **126 passed**, including 24 new pending/route/deadline
cases plus existing credential transitions, settings and isolation regressions. Denied
reads are actionable failures in both synchronous and asynchronous paths; an actual
unavailable Keyring backend still permits the documented private fallback. Ruff and
diff checks pass. Older regressions now explicitly reject both stale-key resurrection
and falsely reporting a denied store as empty.
