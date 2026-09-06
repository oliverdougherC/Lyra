# Required model cache and download evidence

PLA-458/460 release repair, September 6, 2026. Base main:
`3e109a7ef1cdce7362d9f0e8a286881ebf6fa5a5`. The integration ledger must record the
reviewed implementation commit and final packaged candidate; results here exercise
source provisioning in disposable storage, not clean-user packaged acceptance.

## Identity and recovery

The required Nomic embedding file now pins its size and SHA-256 as well as its
immutable Hugging Face revision. The upstream immutable tree API was read during
this task and returned the Git LFS identity below:

- Repository: `nomic-ai/nomic-embed-text-v1.5-GGUF`
- Revision: `0188c9bf409793f810680a5a431e7b899c46104c`
- File: `nomic-embed-text-v1.5.Q8_0.gguf`
- Bytes: `146146432` (about 146 MB / 139 MiB)
- SHA-256: `3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7`
- [Immutable manifest source](https://huggingface.co/api/models/nomic-ai/nomic-embed-text-v1.5-GGUF/tree/0188c9bf409793f810680a5a431e7b899c46104c)

Previously, both provisioning and embedding readiness accepted any existing path.
Four focused regressions failed before repair: empty, truncated and wrong cached
bytes returned as installed without attempting recovery; a symlink target was
also accepted. No student files were opened during these tests.

The embedding readiness path now validates the actual required bytes. Verification
uses a no-follow descriptor and regular-file check; it rejects directories,
symlinks and unreadable entries. Legacy caches and each fresh process rehash the
file. A bounded in-memory fingerprint cache uses device/inode/size/mtime/ctime to
avoid hashing unchanged weights on each request and invalidates replacement or
same-size changes even when mtime is restored.

Downloads use the existing HTTPX dependency with explicit network timeout, a
600-second body deadline checked on every received chunk, and manifest-sized
transfer limits. The response is streamed directly into a private staging
folder, verified, flushed and then atomically published. A failed download,
verification or pre-publication filesystem operation preserves the old cache.
Unsafe entries are rejected instead of following or deleting their targets.
Successful cached setup works offline. A later task can retry a failed attempt;
concurrent waiters receive the same success/failure rather than serially retrying
an outage. Waiters have a 615-second bound. Cancelling one caller does not cancel
a shared setup needed by other callers; the transport retains its own deadline.

Only required embedding setup is automatic. Optional rerank/OCR absence does not
cause unrelated first-use chat to download those assets. This change does not
claim an identity manifest for the separately opted-in historical OCR downloader.

## Disclosure

`model_provisioning.setup_disclosure()` derives the approximate required size
from the same manifest. Settings exposes it as readonly `local_model_setup`;
integration renders that text alongside Setup. Download activity also uses the
manifest-derived description and service name.

The help text distinguishes one-time acquisition from remote inference: weights
come from Hugging Face, do not upload coursework, remain in Lyra's local model
storage, and permit offline local processing after verification. A failed or
interrupted task can be retried in the app. Optional OCR/rerank assets are not
installed automatically. Packaged default storage is
`~/Library/Application Support/Lyra/models`; explicitly configured test/recovery
model roots remain supported. Remote tutor and Exa data-sharing rules are separate.

## Executed verification

```sh
python -m pytest backend/tests/test_model_provisioning.py backend/tests/test_embed.py backend/tests/test_api_chat.py backend/tests/test_fetch_models.py backend/tests/test_llama_server_lifecycle.py backend/tests/test_api_settings.py -q
```

**257 passed**; existing SWIG and Starlette/httpx deprecation warnings remain.
Ruff lint/format and diff whitespace checks passed for this workstream's files.
Coverage includes atomic corrupt-cache repair, wrong same-size identity,
invalid downloaded bytes, partial transfer/retry, cache invalidation/restart,
unsafe entries, denied replacement, actual embedding readiness recovery,
optional-asset absence, transport size/deadline and bounded waiter behavior.
Tests use a small versioned fixture manifest and real verification logic instead
of bypassing SHA/size checks.

A real network check then ran `ensure_weight(EMBEDDING_WEIGHTS)` in a fresh
`TemporaryDirectory`, using `settings.models_dir_override`. It downloaded the
actual required file from the pinned Hugging Face URL. After clearing the
verification cache and replacing the fetch boundary with a function that raises
if called, the second invocation revalidated and reused it without network.
The temporary root was removed after verification. Retained result:

```json
{
  "scenario": "real-huggingface-required-weight-then-offline-revalidation",
  "bytes": 146146432,
  "sha256": "3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7",
  "revision": "0188c9bf409793f810680a5a431e7b899c46104c",
  "download_seconds": 3.18,
  "offline_revalidation_passed": true,
  "optional_rerank_absent": true,
  "optional_ocr_absent": true,
  "remaining_staging_directories": 0
}
```

Real clean-account packaged first-use progress/retry, process interruption during
an actual transfer, helper loading of the fetched file and target 8 GB resource
acceptance remain part of PLA-160/329. Synthetic transport interruption, a
successful real download, and source offline reuse do not close those gates.
