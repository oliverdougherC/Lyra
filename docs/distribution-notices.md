# Distribution notices and license review

The build generates `Contents/Resources/resources/notices/THIRD-PARTY-NOTICES.txt` and
`dependency-licenses.json` from installed Python distribution metadata, production frontend
license inventory, and locked Cargo metadata. It conservatively includes build dependencies and
preserves available license/NOTICE text. Local source locations are not included in the inventory.
Pinned llama.cpp source license files are retained under `packaging/notices`, with source commit
and archive checksum. The b10287 server does not support `--license`.

This collection is attribution evidence, not a new license for Lyra or certification of compliance.
The repository currently has no top-level project license. PyMuPDF 1.28.0 metadata explicitly
states **Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License**; its upstream
[licensing documentation](https://pymupdf.readthedocs.io/en/latest/about.html) confirms these
alternatives. Before public distribution, the owner must resolve and document the applicable
licensing basis. This task does not choose a project license or assume commercial rights.

Missing license text in an installed wheel is not permission to omit upstream distribution terms.
The owner must review the final inventory, native transitive libraries, downloaded model licenses,
and source-availability obligations against the selected licensing basis. Keep that decision with
the immutable release evidence. No public beta is approved by generating this file.

Regenerate with the locked build environment:

```sh
pnpm --dir frontend licenses list --prod --json > frontend-licenses.json
uv run python scripts/collect_distribution_notices.py --frontend-inventory frontend-licenses.json
```

The release workflow generates this inventory before bundling. Optional downloaded models are
not included in the app archive and retain their own upstream licenses; the pinned required
embedding model's source/revision and digest live in `backend/llm/model_provisioning.py`.
