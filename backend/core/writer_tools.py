"""The writer's tool registry: how the assistant reads, searches, and proposes.

One factory, three grants. The chat profile talks to the student and proposes; the
drafter fills sections inside a pipeline; the reviewer files findings (its comment tool
arrives with the comments substrate). All three run the same loop in `backend/llm/tools`
with the registry built here, so every call - whichever profile made it - lands in the
loop's transcript and, through `activity_label`, in front of the student.

Two constraints are enforced here rather than trusted to the model:

- **One pending edit per draft.** A proposal over a draft that already carries one is
  spliced against the already-proposed content and coalesced into the same edit, so the
  student always reviews one diff.
- **Direct writes only into empty sections, checked at write time.** The student may
  have typed into a section while the model was thinking, and a check made at loop
  start would write over what they typed.

Handlers close over a connection owned by the caller (the streaming generator or the
worker), never a request-scoped one: the loop outlives the request that started it.
"""

import sqlite3
from dataclasses import dataclass, fields

from backend.core import (
    artifacts,
    briefs,
    comments,
    mathnorm,
    sections,
    source_ledger,
    suggestions,
    web_research,
    writer_attempts,
    writer_plans,
    writer_runs,
)
from backend.core.errors import LyraError
from backend.core.query_guard import PrivateContextLedger
from backend.core.writer_budgets import (
    DEPTHS,
    WriterCapabilities,
    get_writer_capabilities,
    validate_depth,
)
from backend.llm.tools import RecordedCall, ToolDefinition
from backend.rag.retrieve import retrieve
from backend.tools.result import ToolResult, failure, success

CHAT = "chat"
DRAFTER = "drafter"
REVIEWER = "reviewer"

PROFILES: tuple[str, ...] = (CHAT, DRAFTER, REVIEWER)

# Estimated tokens a search may bring back. Kept below the chat retrieval share on
# purpose: search results land in the tool transcript and are replayed on every later
# round of the same turn, so an over-generous fetch costs the whole conversation.
SEARCH_BUDGET_TOKENS = 1_500

# How much of one retrieved chunk the model is shown. Provenance plus an excerpt is
# enough to decide whether to lean on it; the full chunk arrives when writing does.
_EXCERPT_CHARS = 700
_FETCH_PREVIEW_CHARS = source_ledger.MAX_RELIED_EXCERPT_CHARS
_SOURCE_PAGE_CHARS = 4_000

_NO_SECTION = (
    "No section matches {ref!r}. Address sections by their outline number or title; "
    "read_outline lists them."
)
_OCCUPIED = (
    "Section {ref!r} already carries the student's prose, so it cannot be written "
    "directly. Propose the change instead, so the student can review it."
)
_NOT_EMPTY_DOCUMENT = "Nothing is indexed for this class yet."
_NO_BRIEF_NOTE = (
    "This draft has no brief yet. Discern one from the title, the document, and the "
    "class documents if you can - save_brief records your proposal for the student to "
    "confirm - or ask the student what the assignment is."
)
_ALREADY_FILED = (
    "A comment at this severity is already open on that exact passage. Do not file "
    "the same finding twice; move on to the next one."
)


@dataclass
class RunEffects:
    """What one loop changed, for the route to report as frames after the fact.

    The tools append; the route reads. This exists because a proposal made three rounds
    deep inside the loop still has to reach the interface as its own event, and the
    loop's transcript records calls, not consequences.

    ``attempt_id`` is set by the route after planning but before the loop starts, so
    every effectful handler can bind its target to the producing attempt via
    ``link_target``. It is ``None`` during the probe registry (planning only) and for
    non-chat callers (pipeline workers). The handlers check it on each call rather than
    at build time, so the same registry instance works across the plan-then-run boundary.
    """

    attempt_id: int | None = None
    proposed_edit_id: int | None = None
    brief_saved: bool = False
    pass_started: bool = False
    review_started: bool = False
    replied_to_comments: bool = False
    wrote_sections: list[str] | None = None
    filed_comment_ids: list[int] | None = None
    confirmed_comment_ids: list[int] | None = None

    def note_write(self, ref: str) -> None:
        self.wrote_sections = [*(self.wrote_sections or []), ref]

    def note_comment(self, comment_id: int) -> None:
        self.filed_comment_ids = [*(self.filed_comment_ids or []), comment_id]

    def note_confirmed(self, comment_id: int) -> None:
        self.confirmed_comment_ids = [*(self.confirmed_comment_ids or []), comment_id]

    def link(
        self,
        conn: sqlite3.Connection,
        target_kind: str,
        target_id: int,
        *,
        commit: bool = True,
    ) -> None:
        """Bind a durable target to this attempt, if one is active.

        When ``commit=False`` the caller owns the transaction boundary.
        """
        if self.attempt_id is not None:
            writer_attempts.link_target(
                conn,
                self.attempt_id,
                target_kind=target_kind,
                target_id=target_id,
                commit=commit,
            )


def _compatible_job(job_type: type, artifact_id: int, **options: object) -> object:
    """Construct a writer job while mixed-version migrations are still possible.

    The route/tool contract lands before every pipeline phase consumes every option.
    Filtering against dataclass fields keeps a W1 server able to queue the older runner;
    as soon as the pipeline grows the fields, the same values flow through unchanged.
    """
    accepted = {field.name for field in fields(job_type)}
    return job_type(
        artifact_id, **{key: value for key, value in options.items() if key in accepted}
    )


def _body_part(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The draft's one body part, fresh from the database each time it is asked for."""
    for part in artifacts.list_parts(conn, artifact_id):
        if part["kind"] == artifacts.DRAFT_BODY:
            return part
    raise LookupError(f"Draft {artifact_id} has no body part.")


# The shared run-local ledger lives in `query_guard` so the class agent's tools add to the
# same class the writer's tools use.
_PrivateContextLedger = PrivateContextLedger


def _seed_private_context(
    conn: sqlite3.Connection, artifact_id: int, artifact: dict[str, object]
) -> tuple[str, ...]:
    """Private draft state available before the loop starts."""
    seeded = _PrivateContextLedger(str(artifact["title"]))
    with_body = None
    try:
        with_body = _body_part(conn, artifact_id)
    except LookupError:
        with_body = None
    if with_body is not None:
        seeded.add(with_body.get("content"))
    brief = briefs.get_brief(conn, artifact_id)
    if brief is not None:
        seeded.add(
            brief.get("summary"),
            brief.get("assignment_type"),
            brief.get("audience"),
            brief.get("length_target"),
        )
    plan = writer_plans.get_active_plan(conn, artifact_id)
    if plan is not None:
        seeded.add(plan)
    return seeded.snapshot()


def build_registry(
    conn: sqlite3.Connection,
    artifact_id: int,
    profile: str,
    *,
    private_context: tuple[str, ...] = (),
    capabilities: WriterCapabilities | None = None,
    run_id: int | None = None,
) -> tuple[dict[str, ToolDefinition], RunEffects]:
    """The tools one profile may use on one draft, plus the effects record they share.

    When ``capabilities`` is provided the registry is built from that frozen snapshot
    instead of re-reading class/global settings. This is the mechanism that lets the
    chat route freeze the policy at preflight and guarantee the execution registry
    matches the budgeted one (PLA-309).

    Raises:
        ValueError: on a profile outside the set. A caller bug, not model input.
    """
    if profile not in PROFILES:
        raise ValueError(f"Unknown writer profile: {profile}")

    artifact = artifacts.get_artifact(conn, artifact_id)
    class_id = int(artifact["class_id"])
    if capabilities is None:
        capabilities = get_writer_capabilities(conn, class_id)
    effects = RunEffects()
    exposed_private = _PrivateContextLedger(
        *_seed_private_context(conn, artifact_id, artifact), *private_context
    )

    def read_brief() -> ToolResult:
        brief = briefs.get_brief(conn, artifact_id)
        if brief is None:
            return success(brief=None, note=_NO_BRIEF_NOTE)
        exposed_private.add(
            brief["summary"],
            brief["assignment_type"],
            brief["audience"],
            brief["length_target"],
        )
        return success(
            brief={
                "assignment_type": brief["assignment_type"],
                "summary": brief["summary"],
                "audience": brief["audience"],
                "length_target": brief["length_target"],
                "status": brief["status"],
            }
        )

    def read_outline() -> ToolResult:
        body = str(_body_part(conn, artifact_id)["content"])
        outline = sections.outline(body)
        exposed_private.add(outline)
        return success(outline=outline)

    def read_plan() -> ToolResult:
        plan = writer_plans.get_active_plan(conn, artifact_id)
        if plan is None:
            return success(plan=None, note="This draft does not have a saved writing plan yet.")
        exposed_private.add(plan)
        return success(plan=plan)

    def read_section(ref: str) -> ToolResult:
        body = str(_body_part(conn, artifact_id)["content"])
        section = sections.extract(body, ref)
        if section is None:
            return failure(_NO_SECTION.format(ref=ref))
        exposed_private.add(section.title, section.text)
        return success(
            number=section.number,
            title=section.title,
            word_count=section.word_count,
            text=section.text,
        )

    def search_course_material(query: str) -> ToolResult:
        result = retrieve(conn, class_id, query, SEARCH_BUDGET_TOKENS)
        if not result.chunks:
            return success(results=[], note=_NOT_EMPTY_DOCUMENT)
        ledger_ids: dict[int, int] = {}
        for chunk in result.chunks:
            if chunk.document_id in ledger_ids:
                continue
            source = source_ledger.upsert_source(
                conn,
                class_id,
                source_type=source_ledger.COURSE,
                document_id=chunk.document_id,
                title=chunk.filename,
            )
            ledger_ids[chunk.document_id] = int(source["id"])
        rows = [
            {
                "source_id": ledger_ids[chunk.document_id],
                "source": chunk.filename,
                "section": chunk.section_title or chunk.section_path or "",
                "page": chunk.page_number,
                "excerpt": chunk.content[:_EXCERPT_CHARS],
            }
            for chunk in result.chunks
        ]
        exposed_private.add(*(row["excerpt"] for row in rows))
        return success(results=rows)

    def _web_still_allowed() -> bool:
        """Re-read the live web-research permission at dispatch time.

        The frozen ``capabilities`` snapshot decides which tools appear in the
        registry (grant membership) and how many schema tokens are charged.
        This helper decides whether the outbound network request may proceed:
        a student who revokes web research mid-turn gets an immediate local
        refusal even though the tool is still in the transcript (PLA-309).
        """
        return get_writer_capabilities(conn, class_id).allow_web_research

    def search_web(query: str) -> ToolResult:
        if not _web_still_allowed():
            return failure("Web research has been disabled for this class.")
        try:
            return success(
                results=web_research.search_web(
                    query,
                    allowed=True,
                    private_context=exposed_private.snapshot(),
                )
            )
        except (ValueError, web_research.WebResearchError) as exc:
            return failure(str(exc))

    def fetch_source(url: str) -> ToolResult:
        if not _web_still_allowed():
            return failure("Web research has been disabled for this class.")
        try:
            fetched = web_research.fetch_source(
                url,
                allowed=True,
                source_content_enabled=capabilities.source_content_enabled,
            )
            source = source_ledger.upsert_source(
                conn,
                class_id,
                source_type=source_ledger.WEB,
                url=str(fetched["url"]),
                title=str(fetched["title"]),
                accessed_at=str(fetched["accessed_at"]),
                snapshot=str(fetched["snapshot"]),
                final_url=str(fetched["final_url"]),
                content_type=(str(fetched["content_type"]) if fetched["content_type"] else None),
                truncated=bool(fetched["truncated"]),
                commit=False,
            )
            effects.link(conn, "source", int(source["id"]), commit=False)
            conn.commit()
            return success(
                source_id=source["id"],
                title=source["title"],
                url=source["url"],
                content_preview=str(fetched["snapshot"])[:_FETCH_PREVIEW_CHARS],
                note=(
                    "If you rely on a passage, call record_source_excerpt with this "
                    "source id and the exact excerpt. Cite it as [@lyra:<source_id>]."
                ),
            )
        except (ValueError, web_research.WebResearchError) as exc:
            return failure(str(exc))
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    def read_source(
        source_id: int, source_revision_id: int | None = None, offset: int = 0
    ) -> ToolResult:
        """Read saved evidence without fetching, repairing, or extending the ledger."""
        if type(source_id) is not int or source_id < 1:
            return failure("source_id must be a positive integer.")
        if source_revision_id is not None and (
            type(source_revision_id) is not int or source_revision_id < 1
        ):
            return failure("source_revision_id must be a positive integer or omitted.")
        if type(offset) is not int or offset < 0:
            return failure("offset must be a non-negative character offset.")
        try:
            source = source_ledger.get_source(conn, source_id, class_id=class_id)
        except LyraError as exc:
            return failure(exc.message)
        revision_id = (
            source_revision_id
            if source_revision_id is not None
            else source.get("current_revision_id")
        )
        revision = None
        if revision_id is not None:
            row = conn.execute(
                "select id, revision, snapshot, accessed_at, truncated "
                "from writer_source_revisions where id = ? and source_id = ?",
                (revision_id, source_id),
            ).fetchone()
            if row is None:
                return failure(
                    "That saved source revision is unavailable; no other revision was used."
                )
            revision = dict(row)
            content = str(revision["snapshot"] or "")
        elif source["source_type"] == source_ledger.WEB:
            return failure("This source has no saved immutable revision available to read.")
        else:
            content = str(source.get("snapshot") or "")
        if not content.strip():
            return failure(
                "The requested saved source content is unavailable; "
                "excerpts alone are partial evidence."
            )
        if offset > len(content):
            return failure("offset is beyond the end of this saved source.")
        end = min(offset + _SOURCE_PAGE_CHARS, len(content))
        page = content[offset:end]
        exposed_private.add(page)
        return success(
            source_id=source_id,
            title=source["title"],
            source_revision_id=revision_id,
            revision=revision["revision"] if revision else None,
            accessed_at=revision["accessed_at"] if revision else source["accessed_at"],
            provenance="immutable_revision" if revision else "unversioned_saved_course_snapshot",
            content=page,
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
            omitted=offset > 0 or end < len(content),
            snapshot_truncated=bool(revision["truncated"]) if revision else None,
            note=(
                "Read next_offset with this source_revision_id to continue this revision. "
                "Omitted or unavailable content is not evidence that a claim is absent."
            ),
        )

    def record_source_excerpt(source_id: int, excerpt: str, section_ref: str = "") -> ToolResult:
        try:
            source_ledger.get_source(conn, source_id, class_id=class_id)
            recorded = source_ledger.add_relied_on_excerpt(
                conn,
                source_id,
                excerpt,
                section_ref=section_ref.strip() or None,
                commit=False,
            )
            if conn.in_transaction:
                effects.link(conn, "source_excerpt", int(recorded["id"]), commit=False)
                conn.commit()
            return success(
                recorded=True,
                excerpt_id=recorded["id"],
                citation=f"[@lyra:{source_id}]",
            )
        except (ValueError, LyraError) as exc:
            return failure(exc.message if isinstance(exc, LyraError) else str(exc))
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    def list_class_documents() -> ToolResult:
        # A document's kind lives on its chunks (assigned at ingestion, one kind per
        # document), so it is read off any one of them. Unindexed documents have no
        # chunks yet and honestly report no kind.
        rows = conn.execute(
            "select d.filename, d.state, "
            "(select c.doc_type from chunks c where c.document_id = d.id limit 1) as doc_type "
            "from documents d where d.class_id = ? order by d.filename",
            (class_id,),
        ).fetchall()
        return success(
            documents=[
                {
                    "filename": row["filename"],
                    "kind": row["doc_type"] or "",
                    "state": row["state"],
                }
                for row in rows
            ]
        )

    def read_comments() -> ToolResult:
        part = _body_part(conn, artifact_id)
        threads = comments.unresolved_threads(conn, int(part["id"]), str(part["content"]))
        if not threads:
            return success(comments=[], note="No open comments on this draft.")
        exposed_private.add(
            *(
                piece
                for thread in threads
                for piece in (
                    thread["quote"] or "",
                    thread["body"],
                    *[
                        reply["body"]  # type: ignore[index]
                        for reply in thread["replies"]  # type: ignore[union-attr]
                    ],
                )
            )
        )
        return success(
            comments=[
                {
                    "id": thread["id"],
                    "author": thread["author"],
                    "severity": thread["severity"] or "",
                    "quote": thread["quote"] or "",
                    "body": thread["body"],
                    "replies": [
                        {"author": reply["author"], "body": reply["body"]}
                        for reply in thread["replies"]  # type: ignore[union-attr]
                    ],
                }
                for thread in threads
            ]
        )

    def reply_to_comment(comment_id: int, body: str) -> ToolResult:
        part = _body_part(conn, artifact_id)
        row = conn.execute(
            "select id, parent_id from draft_comments where id = ? and part_id = ?",
            (comment_id, int(part["id"])),
        ).fetchone()
        if row is None:
            return failure(
                f"No comment {comment_id} on this draft. read_comments lists the open "
                "threads with their ids."
            )
        if row["parent_id"] is not None:
            return failure("Replies attach to the thread root, not to another reply.")
        try:
            conn.execute("begin immediate")
            reply = comments.add_reply(conn, comment_id, comments.WRITER, body, commit=False)
            effects.link(conn, "reply", int(reply["id"]), commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        effects.replied_to_comments = True
        return success(replied=True, comment_id=comment_id)

    def add_comment(body: str, severity: str, quote: str = "", section_ref: str = "") -> ToolResult:
        if severity not in comments.SEVERITIES:
            options = ", ".join(comments.SEVERITIES)
            return failure(f"Severity must be one of: {options}.")
        try:
            # Lock before checking ownership and duplicates so cancellation cannot
            # race between validation and a comment being committed.
            conn.execute("begin immediate")
            if run_id is not None:
                current = writer_runs.latest_run(conn, artifact_id)
                if (
                    current is None
                    or int(current["id"]) != run_id
                    or current["status"] not in (writer_runs.QUEUED, writer_runs.RUNNING)
                ):
                    conn.rollback()
                    return failure("This review is no longer active. Its finding was not filed.")
            part = _body_part(conn, artifact_id)
            content = str(part["content"])
            cleaned = quote.strip()
            ref = section_ref.strip()
            hint: int | None = None
            canonical_quote: str | None = cleaned or None
            if cleaned:
                target = sections.extract(content, ref) if ref else None
                if ref and target is None:
                    conn.rollback()
                    return failure(_NO_SECTION.format(ref=ref))
                anchor = comments.resolve_quote(
                    content,
                    cleaned,
                    scope_start=target.start if target is not None else 0,
                    scope_end=target.end if target is not None else None,
                )
                if anchor is not None:
                    hint = anchor.start
                    # Persist what is actually in the document. Keeping the model's
                    # approximate spelling would make the comment orphan itself on the
                    # first list/read even though the server had just found its passage.
                    canonical_quote = content[anchor.start : anchor.end]
                # A model that loses its place restates its findings; the live model did
                # exactly that on its first real review. Same passage at the same severity,
                # still open, is the same finding, and refusing it here also keeps a
                # re-review from re-filing everything the student has not yet resolved.
                duplicate = conn.execute(
                    "select id from draft_comments where part_id = ? and parent_id is null "
                    "and author = ? and quote = ? and severity = ? and resolved = 0 limit 1",
                    (int(part["id"]), comments.REVIEWER, canonical_quote, severity),
                ).fetchone()
            else:
                # Exact unanchored findings must also survive retry without duplicates.
                duplicate = conn.execute(
                    "select id from draft_comments where part_id = ? and parent_id is null "
                    "and author = ? and quote is null and severity = ? and body = ? "
                    "and coalesce(section_ref, '') = ? and resolved = 0 limit 1",
                    (int(part["id"]), comments.REVIEWER, severity, body, ref),
                ).fetchone()
            if duplicate is not None:
                conn.rollback()
                effects.note_confirmed(int(duplicate["id"]))
                return failure(_ALREADY_FILED)
            filed = comments.add_comment(
                conn,
                int(part["id"]),
                comments.REVIEWER,
                body,
                severity=severity,
                quote=canonical_quote,
                hint=hint,
                section_ref=ref or None,
                orphaned=bool(cleaned and hint is None),
                commit=False,
            )
            effects.link(conn, "comment", int(filed["id"]), commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        effects.note_comment(int(filed["id"]))
        return success(filed=True, anchored=hint is not None, comment_id=filed["id"])

    def save_brief(
        summary: str,
        assignment_type: str = "",
        audience: str = "",
        length_target: str = "",
    ) -> ToolResult:
        try:
            conn.execute("begin immediate")
            brief = briefs.save_brief(
                conn,
                artifact_id,
                assignment_type=assignment_type,
                summary=summary,
                audience=audience,
                length_target=length_target,
                commit=False,
            )
            effects.link(conn, "brief", int(brief["artifact_id"]), commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        effects.brief_saved = True
        return success(
            saved=True,
            status=briefs.PROPOSED,
            note=(
                "Saved as your proposal. The student will confirm or correct it - say "
                "what you recorded and that it is a guess until they do."
            ),
        )

    def propose_revision(section: str, replacement: str) -> ToolResult:
        part_id = int(_body_part(conn, artifact_id)["id"])
        replacement = mathnorm.normalize(replacement)
        try:
            conn.execute("begin immediate")
            current = str(artifacts.get_part(conn, part_id)["content"])
            pending = suggestions.pending_for_part(conn, part_id)
            base_text = str(pending["proposed_content"]) if pending else current
            target = sections.extract(base_text, section)
            if target is None:
                conn.rollback()
                return failure(_NO_SECTION.format(ref=section))
            proposed = sections.splice(base_text, target, replacement)
            edit = suggestions.propose(conn, part_id, proposed, f"revise {section}", commit=False)
            if edit is None:
                conn.commit()
                return success(
                    proposed=False,
                    note="That replacement reads exactly as the document already does.",
                )
            effects.link(conn, "proposal", edit.id, commit=False)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        effects.proposed_edit_id = edit.id
        return success(
            proposed=True,
            note=(
                "Recorded as a suggestion the student will review hunk by hunk. The "
                "document itself is unchanged until they accept."
            ),
        )

    def start_draft_pass(
        instruction: str = "",
        section_refs: list[str] | None = None,
        depth: str = "quick",
        pause_at_plan: bool = False,
    ) -> ToolResult:
        from backend.api import routes_drafts
        from backend.core import writer_pipeline

        refs = tuple(ref.strip() for ref in (section_refs or []) if ref.strip())
        try:
            chosen_depth = validate_depth(depth)
            routes_drafts.begin_writer_run(
                conn,
                artifact_id,
                routes_drafts.PASS_JOB_KIND,
                chosen_depth,
                request_payload={
                    "instruction": instruction.strip() or None,
                    "section_refs": [*refs],
                    "pause_at_plan": pause_at_plan,
                },
                commit=False,
            )
            run = routes_drafts.writer_runs.active_run(conn, artifact_id)
            if run is not None:
                effects.link(conn, "pass", int(run["id"]), commit=False)
            conn.commit()
        except (LyraError, ValueError) as exc:
            return failure(exc.message if isinstance(exc, LyraError) else str(exc))
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        writer_pipeline.enqueue(
            _compatible_job(
                writer_pipeline.PassJob,
                artifact_id,
                instruction=instruction.strip() or None,
                section_refs=refs,
                depth=chosen_depth,
                pause_at_plan=pause_at_plan,
                run_id=int(run["id"]) if run is not None else None,
            )
        )
        effects.pass_started = True
        return success(
            queued=True,
            note=(
                "The pass is queued and runs in the background; the workspace shows "
                "its progress section by section. Tell the student it has started - "
                "do not wait for it here."
            ),
        )

    def start_review(depth: str = "quick") -> ToolResult:
        from backend.api import routes_drafts
        from backend.core import review_pipeline

        try:
            chosen_depth = validate_depth(depth)
            routes_drafts.begin_writer_run(
                conn,
                artifact_id,
                routes_drafts.REVIEW_JOB_KIND,
                chosen_depth,
                request_payload={},
                commit=False,
            )
            run = routes_drafts.writer_runs.active_run(conn, artifact_id)
            if run is not None:
                effects.link(conn, "review", int(run["id"]), commit=False)
            conn.commit()
        except (LyraError, ValueError) as exc:
            return failure(exc.message if isinstance(exc, LyraError) else str(exc))
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        review_pipeline.enqueue(
            _compatible_job(
                review_pipeline.ReviewJob,
                artifact_id,
                depth=chosen_depth,
                run_id=int(run["id"]) if run is not None else None,
            )
        )
        effects.review_started = True
        return success(
            queued=True,
            note=(
                "The review is queued and runs in the background, filing findings as "
                "margin comments in the Comments tab as it works. Tell the student it "
                "has started - do not wait for it here."
            ),
        )

    def write_section(section: str, content: str) -> ToolResult:
        part_id = int(_body_part(conn, artifact_id)["id"])
        try:
            conn.execute("begin immediate")
            body = str(artifacts.get_part(conn, part_id)["content"])
            target = sections.extract(body, section)
            if target is None:
                conn.rollback()
                return failure(_NO_SECTION.format(ref=section))
            if not target.is_empty:
                conn.rollback()
                return failure(_OCCUPIED.format(ref=section))
            artifacts.apply_part_content(
                conn,
                part_id,
                sections.splice(body, target, mathnorm.normalize(content)),
                origin=artifacts.GENERATED,
                note=f"drafted {target.number} {target.title}".strip(),
                record_revision=True,
            )
            effects.link(conn, "section_write", part_id, commit=False)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        effects.note_write(target.number)
        return success(written=True, section=target.number)

    catalogue = {
        "read_brief": _tool(
            "read_brief",
            "What this document is: the assignment, audience, and length. Read it "
            "before advising on structure or content.",
            read_brief,
        ),
        "read_outline": _tool(
            "read_outline",
            "The document's numbered outline with per-section word counts. Cheap; "
            "prefer it to reading sections you do not need.",
            read_outline,
        ),
        "read_plan": _tool(
            "read_plan",
            "The saved thesis, argument map, section jobs, research notes, word budgets, "
            "and source ids. Read it before steering or judging a planned draft.",
            read_plan,
        ),
        "read_section": _tool(
            "read_section",
            "One section's full text, children included, by outline number or title.",
            read_section,
            ref={"type": "string", "description": "Outline number ('2.1') or a title."},
        ),
        "search_course_material": _tool(
            "search_course_material",
            "Search the class's indexed documents. Returns excerpts with their source "
            "file and section, which is what to cite when you lean on one.",
            search_course_material,
            query={"type": "string", "description": "What to look for, as a question."},
        ),
        "search_web": _tool(
            "search_web",
            "Search the public web when this class explicitly permits web research. "
            "A result is not a source until fetch_source snapshots it.",
            search_web,
            query={"type": "string", "description": "A narrow research question."},
        ),
        "fetch_source": _tool(
            "fetch_source",
            "Fetch, sanitize, and snapshot one public textual source into the class ledger.",
            fetch_source,
            url={"type": "string", "description": "An http or https result URL."},
        ),
        "read_source": _tool(
            "read_source",
            "Read up to 4000 characters of a saved class source, without network access. "
            "Use the cited supporting source_revision_id for historical evidence. "
            "Selected ledger excerpts do not show the full source; check saved content "
            "before claiming evidence is absent. Follow next_offset for additional pages.",
            read_source,
            source_id={"type": "integer", "description": "The ledger source id."},
            source_revision_id={
                "type": "integer",
                "optional": True,
                "description": "Supporting revision ID; omitted uses current saved revision.",
            },
            offset={
                "type": "integer",
                "optional": True,
                "description": "Character offset: initially 0, then returned next_offset.",
            },
        ),
        "record_source_excerpt": _tool(
            "record_source_excerpt",
            "Record the exact passage actually relied on, bound to a ledger source and "
            "optional plan section. Returns the citation marker for prose.",
            record_source_excerpt,
            source_id={"type": "integer", "description": "The ledger source id."},
            excerpt={"type": "string", "description": "The exact relied-on passage."},
            section_ref={
                "type": "string",
                "description": "The plan/document section this evidence supports.",
                "optional": True,
            },
        ),
        "list_class_documents": _tool(
            "list_class_documents",
            "Every document uploaded to this class, with its kind. Use it to find an "
            "assignment handout or rubric worth reading.",
            list_class_documents,
        ),
        "read_comments": _tool(
            "read_comments",
            "Unresolved margin comments on this draft, anchored to the passages they are about.",
            read_comments,
        ),
        "reply_to_comment": _tool(
            "reply_to_comment",
            "Reply under one comment thread, as the writer. Use it to say what you did "
            "about a finding - name the proposal or the change - or to disagree and say "
            "why. Resolving the thread is the student's call, never yours.",
            reply_to_comment,
            comment_id={
                "type": "integer",
                "description": "The thread root's id, from read_comments.",
            },
            body={"type": "string", "description": "The reply, short and specific."},
        ),
        "add_comment": _tool(
            "add_comment",
            "File one margin comment: a specific finding on a specific passage. Quote "
            "the passage verbatim from the current text; omit the quote only for a "
            "finding about the whole document. One comment per finding. Identify the "
            "problem and what needs to change - do not write the fix.",
            add_comment,
            body={
                "type": "string",
                "description": "The finding: what is wrong, why, and what needs to change.",
            },
            severity={
                "type": "string",
                "enum": list(comments.SEVERITIES),
                "description": "critical invalidates, major weakens, minor is surface, "
                "note is a suggestion.",
            },
            quote={
                "type": "string",
                "description": "The passage, copied verbatim from the document as it "
                "stands. Keep it short and exact.",
                "optional": True,
            },
            section_ref={
                "type": "string",
                "description": "Outline number or title containing the quoted passage. "
                "Use this whenever the finding is section-specific.",
                "optional": True,
            },
        ),
        "start_review": _tool(
            "start_review",
            "Queue a background review of the whole document: structure, argument, "
            "prose, and claims against the course material, filed as margin comments. "
            "Use it when the student asks for a review or feedback on the piece, not "
            "for a question about one passage.",
            start_review,
            depth={
                "type": "string",
                "enum": list(DEPTHS),
                "description": "How much review time to spend.",
                "optional": True,
            },
        ),
        "save_brief": _tool(
            "save_brief",
            "Record your proposal for what this document is. The student confirms or "
            "corrects it; never present it back to them as settled.",
            save_brief,
            summary={
                "type": "string",
                "description": "One or two sentences: topic, purpose, and any stated constraints.",
            },
            assignment_type={
                "type": "string",
                "description": "'essay', 'lab report', and the like.",
                "optional": True,
            },
            audience={"type": "string", "description": "Who it is for.", "optional": True},
            length_target={
                "type": "string",
                "description": "As stated: '5 pages', '2000 words'.",
                "optional": True,
            },
        ),
        "start_draft_pass": _tool(
            "start_draft_pass",
            "Queue a background draft pass over the document: structure first if it "
            "has none, then each section in order - written directly when empty, "
            "proposed for review when not. Use it when the student wants the document "
            "drafted or a whole-document polish, not for one section's edit.",
            start_draft_pass,
            instruction={
                "type": "string",
                "description": "The lens for the pass, such as 'tighten the argument'. "
                "Empty means draft the document from the brief.",
                "optional": True,
            },
            section_refs={
                "type": "array",
                "items": {"type": "string"},
                "description": "Outline numbers or titles to limit the pass to.",
                "optional": True,
            },
            depth={
                "type": "string",
                "enum": list(DEPTHS),
                "description": "How much drafting and revision time to spend.",
                "optional": True,
            },
            pause_at_plan={
                "type": "boolean",
                "description": "Stop after saving the plan so the student can review it.",
                "optional": True,
            },
        ),
        "propose_revision": _tool(
            "propose_revision",
            "Propose replacing one section. The student reviews the change hunk by "
            "hunk; the document is untouched until they accept. The replacement must "
            "be the whole section including its heading line.",
            propose_revision,
            section={"type": "string", "description": "Outline number or title."},
            replacement={
                "type": "string",
                "description": "The complete new section markdown, heading included.",
            },
        ),
        "write_section": _tool(
            "write_section",
            "Write one EMPTY section directly. Refused on any section carrying prose; "
            "propose_revision is the path for those.",
            write_section,
            section={"type": "string", "description": "Outline number or title."},
            content={
                "type": "string",
                "description": "The complete section markdown, heading included.",
            },
        ),
    }

    grants = {
        CHAT: (
            "read_brief",
            "read_plan",
            "read_outline",
            "read_section",
            "read_source",
            "search_course_material",
            "list_class_documents",
            "read_comments",
            "save_brief",
            "propose_revision",
            "start_draft_pass",
            "start_review",
            "reply_to_comment",
            "record_source_excerpt",
            *(("search_web", "fetch_source") if capabilities.allow_web_research else ()),
        ),
        DRAFTER: (
            "read_brief",
            "read_plan",
            "read_outline",
            "read_section",
            "read_source",
            "search_course_material",
            "list_class_documents",
            "read_comments",
            "write_section",
            "propose_revision",
            "record_source_excerpt",
            *(("search_web", "fetch_source") if capabilities.allow_web_research else ()),
        ),
        REVIEWER: (
            "read_brief",
            "read_plan",
            "read_outline",
            "read_section",
            "read_source",
            "search_course_material",
            "list_class_documents",
            "read_comments",
            "add_comment",
            "record_source_excerpt",
            *(("search_web", "fetch_source") if capabilities.allow_web_research else ()),
        ),
    }
    return {name: catalogue[name] for name in grants[profile]}, effects


def _tool(name: str, description: str, handler: object, **properties: object) -> ToolDefinition:
    """Build a definition the way the loop's own `_tool` does, without importing a private.

    Required arguments are those without the `optional` marker, and the marker is
    stripped before the schema is handed to the model, exactly as in `backend/llm/tools`.
    """
    required = [
        key
        for key, schema in properties.items()
        if not (isinstance(schema, dict) and schema.get("optional"))
    ]
    cleaned = {
        key: (
            {inner: value for inner, value in schema.items() if inner != "optional"}
            if isinstance(schema, dict)
            else schema
        )
        for key, schema in properties.items()
    }
    return ToolDefinition(
        name=name,
        description=description,
        parameters={"type": "object", "properties": cleaned, "required": required},
        handler=handler,  # type: ignore[arg-type]
    )


def activity_label(call: RecordedCall) -> str:
    """One human sentence per tool call, for the activity frames and the stored trail."""
    ref = str(call.arguments.get("ref") or call.arguments.get("section") or "").strip()
    quoted = f'"{ref}"' if ref else ""
    if call.name == "read_brief":
        return "Reading the brief"
    if call.name == "read_outline":
        return "Reading the outline"
    if call.name == "read_plan":
        return "Reading the writing plan"
    if call.name == "read_section":
        return f"Reading section {quoted}" if quoted else "Reading a section"
    if call.name == "search_course_material":
        return "Searching the course material"
    if call.name == "search_web":
        return "Searching the web"
    if call.name == "fetch_source":
        return "Fetching and saving a source"
    if call.name == "record_source_excerpt":
        return "Recording cited evidence"
    if call.name == "list_class_documents":
        return "Looking over the class documents"
    if call.name == "read_comments":
        return "Reading the comments"
    if call.name == "reply_to_comment":
        return "Replying to a comment"
    if call.name == "add_comment":
        severity = str(call.arguments.get("severity") or "").strip()
        return f"Filing a {severity} comment" if severity else "Filing a comment"
    if call.name == "save_brief":
        return "Proposing a brief"
    if call.name == "start_draft_pass":
        return "Starting a draft pass"
    if call.name == "start_review":
        return "Starting a review"
    if call.name == "propose_revision":
        return f"Proposing a revision to {quoted}" if quoted else "Proposing a revision"
    if call.name == "write_section":
        return f"Writing section {quoted}" if quoted else "Writing a section"
    return call.name


def activity_entry(call: RecordedCall) -> dict[str, object]:
    """The stored and streamed shape of one call: what it did, in words, and whether it ran."""
    return {"tool": call.name, "label": activity_label(call), "ok": call.ok}
