# Beta testing and first use

The first public beta is not approved or published yet. This guide describes the intended
supported tester path and the implemented onboarding behavior; it does not approve a candidate.
Use the [release evidence ledger](release-evidence.md) for publication and acceptance status.

## Installation and support

The initial target is Apple Silicon with macOS 14 or newer. Intel is not supported by this beta.
The physical 8 GB Mac acceptance run remains open; no memory/performance certification is claimed.

When publication is approved, the permanent
[Download Lyra Beta](https://oliverdougherc.github.io/Lyra/beta/) page will point to the approved
GitHub release. Until then it may be unavailable. An Actions artifact, locally signed app, or draft
release is for review and must not be confused with that download.

Open the approved DMG, drag Lyra to Applications, and launch from Applications. It contains the
frontend, Python backend, and helper runtime. No developer tools are needed. Do not disable
Gatekeeper to turn an unnotarized candidate into an approved release.

Approved beta downloads use hardened ad-hoc signatures; they are not Developer ID signed or
notarized by Apple. If macOS says the developer cannot be verified or Apple cannot check the app,
first confirm you downloaded the approved release from the page above. After attempting to open
Lyra, go to **System Settings → Privacy & Security**, find Lyra's **Open Anyway** button, and
confirm **Open**. This grants an exception for that app while leaving global security protections
enabled. Follow [Apple's first-launch instructions](https://support.apple.com/en-us/102445).
If macOS reports damage or malware, do not bypass that warning; report it with the release version.

An update can cause macOS to ask again for access to stored Keychain credentials because the
ad-hoc code identity changes between builds. Prior **Always Allow** approvals may not carry over.
The final candidate's first-launch and credential behavior must still pass the release acceptance
checks; these instructions are not evidence of that pass.

## Set up your first class

1. Create a class and add a small reading or assignment. PDF, plain text, Markdown, PNG, and JPEG
   uploads are accepted; an image or scanned PDF needs text recognition to become searchable.
2. Open Settings and configure an OpenAI-compatible tutor endpoint, model, and any required key.
   Use the connection/capability test. Lyra does not install or host a general tutor for you.
3. Review the remote-processing acknowledgement if the endpoint is not loopback-local. It applies
   to document text and to page images when you ask for recognition.
4. Return to the class to ask a question, prepare a solution, create practice material, or write.
   Start with synthetic or non-sensitive material while evaluating a beta.

A local tutor needs its own running server. A remote tutor needs network access and may bill your
provider account. Local-network hosts are treated as remote too. Image recognition requires a
vision-capable tutor; normal text chat capability alone does not establish image support.

## Draft a follow-up while Lyra responds

In class chat and the writing assistant, you can type the next message while a response is
running or finishing its reveal. **Send** becomes available once that turn has ended; typing
never queues or sends a request. **Stop** remains available during generation, and sending
waits for cancellation to be confirmed. A failed response does not replace your newer typing.

Unsent text belongs to the conversation currently on screen. Switching conversations, classes,
or writing documents clears it; copy anything you want to keep before navigating away.

During a response, brief activity labels report observed processing stages or completed tool
actions. When no usable activity signal is available, Lyra shows **Thinking**. These labels are
activity summaries, and the expandable details remain separate from the answer.

## Downloads, optional services, and offline use

Document search uses local `nomic-embed-text-v1.5` embeddings. First processing downloads about
146 MB of pinned weights from Hugging Face. Interrupted downloads can be retried. Verified cached
weights can be used offline; downloading weights does not upload course files.

Optional OCR/reranking model files are not downloaded automatically. The current document
recognition workflow sends requested page images to the configured vision tutor. A local OCR helper
implementation exists in source, but is not the selected ingestion path. See the
[RAG pipeline](rag-pipeline.md) for implementation boundaries.

Exa web research is optional, uses a separate user-owned key, and is tested only on request.
Without it, local documents and tutor workflows remain usable. Offline access to saved material
continues, but generation requires the configured tutor to be reachable and web research requires Exa.

## Protect your work and report problems

Settings includes **Save backup** and **Restore backup**. Finish saving edits before starting.
Restore verifies the archive before switching profiles and retains the previous profile as a recovery
copy. Archives can contain private fallback credentials; store them privately. Keychain credentials
are separate and may need configuring on another Mac. See [privacy](privacy-and-data-location.md).

App replacement preserves the separate data directory. Updates are checked only when requested in
Settings; install/restart and recovery behavior are documented in [releasing](releasing.md).
Native backup, export/print, update, and clean-machine behavior still require the candidate-specific
evidence recorded in the release ledger.

Report the version/build shown in Settings, macOS version, Mac model/memory, reproduction steps,
and whether retry or relaunch helped. Use the [GitHub issue template](https://github.com/oliverdougherC/Lyra/issues/new/choose)
and synthetic examples. Never attach private documents, databases, API keys, or raw logs.
For failures you can resolve locally, start with [troubleshooting](troubleshooting.md).
