# Chat draft persistence

The desktop composer store is synchronous because typing must update the visible text
without waiting for a database or network request. `frontend/src/lib/chat-draft-store.ts`
uses `localStorage` for bounded offline recovery and `sessionStorage` for a distinct
writer ID in each window.

## Revision invariant

Each new draft revision has a unique `lyra:unsent-chat:v2:` key containing its
scope, window writer ID, and revision ID. No writer replaces that key. Cleanup
removes only named revision keys, so a competing write gets a different key and
cannot be removed by an earlier cleanup. A saved revision also records its
still-present predecessor keys. If the app exits or storage refuses cleanup
after the new revision is saved, a later accepted revision retires those exact
predecessors before retiring itself. A competing branch with a different
revision is not part of that ancestry.

Acknowledgements use one `lyra:chat-draft-retired:v2:` key per revision. They
reach storage before the acknowledged record is removed. Separate windows can
acknowledge concurrently without overwriting one shared marker array. Once no
record has the acknowledged revision, the marker is compacted. A denied marker
write is reported as failed settlement; the current window keeps an in-memory
suppression and warning instead of claiming durable retirement.

The previous `v1` per-writer slot and marker array remain readable. New code
never rewrites or deletes a legacy mutable slot: an older open context could
otherwise put new unsent text into it just before deletion. Accepted legacy
revisions are suppressed by revision markers, and legacy source choices are
copied to the replaceable preference key without removing the old slot.

## Capacity and cost

At most 128 genuine unsent records are admitted. A competing last-slot write
checks capacity after persistence and rolls back its own revision if it loses
the slot. A replacement briefly uses one extra key before its predecessor is
removed. If storage refuses deletion, a failed rollback can leave an extra
physical copy; later writes refuse the occupied cap until cleanup succeeds.
Pre-existing legacy slots are a fixed migration population and do not grow
under the new writer. Retired legacy copies and source choices do not consume
the unsent admission limit, though browser quota can still refuse a write and
is reported as non-durable.

Ordinary replacement typing reads only its known predecessor keys and removes
them after the new key is saved. A full retired-record sweep runs at capacity,
not for every keystroke. Read and settlement paths still inspect the records
for their scope. The store never scans settled chat transcripts during typing.
