"""Contextual, audited tool registries for the Phase 4 class agent.

The registry assembled here is the server-side authority.  Prompts may describe a
capability, but a model can call it only when the current class/session grants cause its
definition to be present.  Every exposed handler repeats that authorization at dispatch
time and writes a durable start/terminal audit pair.

Network reads and database proposals deliberately remain separate tools.  A fetched
page is held in one bounded, run-local collector until the model explicitly proposes it
for the shared source ledger.  Workspace changes and commands are likewise inert rows;
there is no model-callable file-apply or process-execute handler in this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from backend.core import (
    agent_store,
    profiles,
    sessions,
    source_ledger,
    tool_audit,
    web_research,
    workspace_changes,
    workspace_paths,
)
from backend.core.errors import LyraError
from backend.core.query_guard import QueryRefusal, guard_web_query
from backend.core.writer_budgets import WriterCapabilities, get_writer_capabilities
from backend.llm import tool_profiles
from backend.llm.tools import REGISTRY as COMPUTE_REGISTRY
from backend.llm.tools import ToolDefinition
from backend.tools.result import ToolResult, failure, success

type AgentProfile = Literal["research", "code", "command"]

PROFILES: tuple[AgentProfile, ...] = ("research", "code", "command")
FETCH_PREVIEW_CHARS = source_ledger.MAX_RELIED_EXCERPT_CHARS


@dataclass(frozen=True, slots=True)
class AgentCapabilitySnapshot:
    """The capability state that decides which tool schemas one turn exposes.

    Read once, at the start of the pre-mutation plan, and reused for both the token
    budgeting and the executable registry, so a settings or grant change that lands between
    the two cannot make the registry the loop runs larger or different from the one the
    preflight charged for. It carries only the booleans that gate *schema inclusion* - not
    the workspace row or the firecrawl endpoint, which each retained handler re-reads live
    at dispatch time. That separation is deliberate: freezing the snapshot fixes what the
    model is *offered*, while dispatch-time reauthorization still decides what it may
    *do*. A grant revoked after the snapshot is taken therefore still fails closed at the
    handler; a grant newly enabled after it waits until the next turn's snapshot.
    """

    allow_web_research: bool
    firecrawl_scrape_enabled: bool
    workspace_present: bool
    workspace_read_enabled: bool
    workspace_change_proposals_enabled: bool
    workspace_commands_enabled: bool


def snapshot_agent_capabilities(conn: sqlite3.Connection, class_id: int) -> AgentCapabilitySnapshot:
    """Read the schema-gating capability state for one class exactly once.

    Both the web-research grant and the workspace grants are read here so a single frozen
    value drives every profile's schema selection. The handlers built from this snapshot
    still re-read the live workspace row and web-research grant when they run, so this read
    settles only what is exposed, never what is authorized.
    """
    capabilities = get_writer_capabilities(conn, class_id)
    workspace = agent_store.get_workspace_for_class(conn, class_id)
    return AgentCapabilitySnapshot(
        allow_web_research=bool(capabilities.allow_web_research),
        firecrawl_scrape_enabled=bool(capabilities.firecrawl_scrape_enabled),
        workspace_present=workspace is not None,
        workspace_read_enabled=bool(workspace["read_enabled"]) if workspace else False,
        workspace_change_proposals_enabled=(
            bool(workspace["change_proposals_enabled"]) if workspace else False
        ),
        workspace_commands_enabled=bool(workspace["commands_enabled"]) if workspace else False,
    )


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """One bounded page held only for the lifetime of an agent turn."""

    fetch_id: str
    url: str
    final_url: str
    title: str
    accessed_at: str
    content_type: str | None
    snapshot: str
    truncated: bool
    warning: str | None


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """A compact activity item suitable for SSE/UI projection."""

    audit_id: str
    tool: str
    capability: str
    effect: str
    state: str
    target_kind: str | None = None
    target_id: str | None = None


@dataclass(slots=True)
class AgentRunActivity:
    """Run-local consequences and durable audit identifiers produced by a registry."""

    events: list[AgentActivity] = field(default_factory=list)
    fetched_sources: dict[str, FetchedSource] = field(default_factory=dict, repr=False)
    source_ids: list[int] = field(default_factory=list)
    workspace_change_ids: list[int] = field(default_factory=list)
    command_request_ids: list[int] = field(default_factory=list)
    profile_fact_ids: list[int] = field(default_factory=list)

    def note(
        self,
        *,
        audit_id: str,
        tool: str,
        capability: str,
        effect: str,
        state: str,
        target_kind: str | None = None,
        target_id: str | None = None,
    ) -> None:
        self.events.append(
            AgentActivity(
                audit_id=audit_id,
                tool=tool,
                capability=capability,
                effect=effect,
                state=state,
                target_kind=target_kind,
                target_id=target_id,
            )
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    result: ToolResult
    summary: Mapping[str, object]
    target_kind: str | None = None
    target_id: str | None = None


class _RefusalError(Exception):
    """A safe policy/input refusal that should be visible to the model."""


def _definition(
    name: str,
    description: str,
    handler: Callable[..., ToolResult],
    *,
    properties: Mapping[str, object] | None = None,
    required: Sequence[str] = (),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": dict(properties or {}),
            "required": list(required),
            "additionalProperties": False,
        },
        handler=handler,
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _audit_arguments(tool: str, arguments: Mapping[str, object]) -> dict[str, object]:
    """Project arguments into metadata without persisting private/bulky values."""
    projected: dict[str, object] = {}
    for name, value in arguments.items():
        if name in {"query", "proposed_content", "excerpt"}:
            text = str(value)
            projected[f"{name}_chars"] = len(text)
            projected[f"{name}_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        elif name == "argv":
            projected["argv_count"] = len(value) if isinstance(value, list) else None
            projected["argv_sha256"] = _digest(value)
        elif name == "url":
            text = str(value)
            parsed = urlsplit(text)
            projected["url_origin"] = (
                f"{parsed.scheme.lower()}://{parsed.hostname.lower()}"
                if parsed.scheme and parsed.hostname
                else "invalid"
            )
            projected["url_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        else:
            projected[name] = value
    projected["tool"] = tool
    return projected


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, _RefusalError | ValueError | LyraError | web_research.WebResearchError):
        return str(exc)
    return "That tool could not complete safely."


def _text(value: object, field_name: str, *, maximum: int, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise _RefusalError(f"{field_name} must be text.")
    if len(value) > maximum:
        raise _RefusalError(f"{field_name} is too long.")
    if not allow_blank and not value.strip():
        raise _RefusalError(f"{field_name} cannot be blank.")
    return value


def _integer(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _RefusalError(f"{field_name} must be an integer.")
    if value < minimum or value > maximum:
        raise _RefusalError(f"{field_name} is outside the allowed range.")
    return value


def _session_scope(conn: sqlite3.Connection, class_id: int, session_id: int) -> None:
    session = sessions.get_session(conn, session_id)
    if int(session["class_id"]) != class_id:
        raise _RefusalError("That conversation does not exist in this class.")


def _research_authorization(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    *,
    require_scrape: bool = False,
) -> object:
    _session_scope(conn, class_id, session_id)
    capabilities = get_writer_capabilities(conn, class_id)
    if not capabilities.allow_web_research:
        raise _RefusalError("Web research is disabled for this class.")
    if require_scrape and not capabilities.firecrawl_scrape_enabled:
        raise _RefusalError(
            "Web source fetching is unavailable until the scrape safety gate passes."
        )
    return capabilities


def _workspace_authorization(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    grant: str,
) -> dict[str, object]:
    _session_scope(conn, class_id, session_id)
    workspace = agent_store.get_workspace_for_class(conn, class_id)
    if workspace is None:
        raise _RefusalError("No workspace is attached to this class.")
    if not bool(workspace[grant]):
        labels = {
            "read_enabled": "Workspace read is disabled for this class.",
            "change_proposals_enabled": "Workspace change proposals are disabled for this class.",
            "commands_enabled": "Commands are disabled for this class.",
        }
        raise _RefusalError(labels[grant])
    root = workspace_paths.canonical_workspace_root(str(workspace["root_path"]))
    details = os.lstat(root)
    if int(workspace["root_device"]) != int(details.st_dev) or int(workspace["root_inode"]) != int(
        details.st_ino
    ):
        raise _RefusalError("The attached workspace changed and must be attached again.")
    return workspace


def _audited_handler(
    conn: sqlite3.Connection,
    *,
    class_id: int,
    session_id: int,
    name: str,
    capability: tool_profiles.Capability,
    effect: tool_profiles.Effect,
    activity: AgentRunActivity,
    authorize: Callable[[], object],
    action: Callable[..., _Outcome],
) -> Callable[..., ToolResult]:
    """Wrap authorization and work in one durable audit lifecycle."""

    def call(**arguments: object) -> ToolResult:
        safe_arguments = _audit_arguments(name, arguments)
        try:
            authorization = authorize()
            policy_decision = "allowed"
        except Exception as exc:
            message = _safe_error(exc)
            try:
                started = tool_audit.start_event(
                    conn,
                    caller_kind="class_agent",
                    caller_id=str(session_id),
                    class_id=class_id,
                    session_id=session_id,
                    tool=name,
                    capability=capability,
                    effect=effect,
                    arguments=safe_arguments,
                    policy_decision="refused",
                )
                tool_audit.finish_event(conn, started.id, state="refused", error_message=message)
                activity.note(
                    audit_id=started.id,
                    tool=name,
                    capability=capability,
                    effect=effect,
                    state="refused",
                )
            except Exception:
                return failure("Tool audit is unavailable; no action was taken.")
            return failure(message)

        try:
            started = tool_audit.start_event(
                conn,
                caller_kind="class_agent",
                caller_id=str(session_id),
                class_id=class_id,
                session_id=session_id,
                tool=name,
                capability=capability,
                effect=effect,
                arguments=safe_arguments,
                policy_decision=policy_decision,
            )
        except Exception:
            return failure("Tool audit is unavailable; no action was taken.")

        try:
            outcome = action(authorization, **arguments)
        except Exception as exc:
            refused = isinstance(exc, _RefusalError | ValueError | LyraError)
            state = "refused" if refused else "failed"
            message = _safe_error(exc)
            try:
                tool_audit.finish_event(conn, started.id, state=state, error_message=message)
            except Exception:
                state = tool_audit.STARTED
            activity.note(
                audit_id=started.id,
                tool=name,
                capability=capability,
                effect=effect,
                state=state,
            )
            return failure(message)

        terminal_state = "succeeded"
        try:
            tool_audit.finish_event(
                conn,
                started.id,
                state=terminal_state,
                result_summary=outcome.summary,
            )
        except Exception:
            terminal_state = tool_audit.STARTED
        activity.note(
            audit_id=started.id,
            tool=name,
            capability=capability,
            effect=effect,
            state=terminal_state,
            target_kind=outcome.target_kind,
            target_id=outcome.target_id,
        )
        return outcome.result

    return call


def _annotated(
    definition: ToolDefinition,
    *,
    capability: tool_profiles.Capability,
    effect: tool_profiles.Effect,
    trust: tool_profiles.Trust,
) -> tool_profiles.AnnotatedToolDefinition:
    return tool_profiles.annotate_tool_definition(
        definition, capability=capability, effect=effect, trust=trust
    )


def build_agent_registry(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    profile: AgentProfile,
    *,
    private_context: Sequence[str] = (),
    snapshot: AgentCapabilitySnapshot | None = None,
) -> tuple[dict[str, ToolDefinition], AgentRunActivity]:
    """Build the smallest raw registry allowed for one class-agent turn.

    Capability state gates which schemas are exposed, and each retained handler repeats that
    authorization at dispatch so a grant revoked during a running turn fails closed. When a
    caller has already frozen the schema-gating state for the turn it passes that
    `snapshot`, and this build offers exactly the tools the snapshot admits - the agent
    route does this so the registry the loop runs is provably the one its preflight
    budgeted. Omitting `snapshot` reads the state live here, which is the behaviour every
    caller outside the fit-checked agent route relies on.
    """
    if profile not in PROFILES:
        raise ValueError(f"Unknown agent profile: {profile}")
    _session_scope(conn, class_id, session_id)
    if snapshot is None:
        snapshot = snapshot_agent_capabilities(conn, class_id)
    activity = AgentRunActivity()
    definitions: list[tool_profiles.AnnotatedToolDefinition] = []

    def session_authorization() -> object:
        _session_scope(conn, class_id, session_id)
        return None

    for compute in COMPUTE_REGISTRY.values():

        def compute_action(
            _authorization: object,
            _handler: Callable[..., ToolResult] = compute.handler,
            **arguments: object,
        ) -> _Outcome:
            result = _handler(**arguments)
            if not result.ok:
                raise _RefusalError(result.error)
            return _Outcome(
                result=result,
                summary={"ok": True, "result_keys": sorted(result.value)},
                target_kind="computation",
            )

        wrapped = ToolDefinition(
            name=compute.name,
            description=compute.description,
            parameters=compute.parameters,
            handler=_audited_handler(
                conn,
                class_id=class_id,
                session_id=session_id,
                name=compute.name,
                capability="compute",
                effect="pure",
                activity=activity,
                authorize=session_authorization,
                action=compute_action,
            ),
        )
        definitions.append(
            _annotated(wrapped, capability="compute", effect="pure", trust="computed")
        )

    if profile == "research":
        _add_research_tools(
            conn,
            class_id,
            session_id,
            definitions,
            activity,
            tuple(private_context),
            snapshot,
        )
    elif profile == "code":
        _add_code_tools(conn, class_id, session_id, definitions, activity, snapshot)
    else:
        _add_command_tools(conn, class_id, session_id, definitions, activity, snapshot)

    selected = tool_profiles.build_tool_profile(profile, definitions)
    return {item.name: item.definition for item in selected.definitions}, activity


def _add_research_tools(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    definitions: list[tool_profiles.AnnotatedToolDefinition],
    activity: AgentRunActivity,
    private_context: tuple[str, ...],
    snapshot: AgentCapabilitySnapshot,
) -> None:
    # Schema inclusion is decided by the frozen snapshot, not a fresh read, so the tools
    # offered match what the preflight budgeted. The handlers below still re-read the live
    # web-research grant (and firecrawl gate) at dispatch through `_research_authorization`.
    if not snapshot.allow_web_research:
        return

    def research_auth() -> object:
        return _research_authorization(conn, class_id, session_id)

    def scrape_auth() -> object:
        return _research_authorization(conn, class_id, session_id, require_scrape=True)

    def search_action(capabilities: WriterCapabilities, *, query: str) -> _Outcome:
        query = _text(query, "query", maximum=500)
        guarded = guard_web_query(query, private_context=private_context)
        if isinstance(guarded, QueryRefusal):
            raise _RefusalError(guarded.message)
        results = web_research.search_web(
            guarded.query,
            allowed=True,
            firecrawl_base_url=capabilities.firecrawl_base_url,
            private_context=private_context,
        )
        return _Outcome(
            success(results=results, count=len(results)),
            {"result_count": len(results)},
            target_kind="public_web_search",
        )

    search_definition = _definition(
        "search_web",
        "Search the public web with a short query. Never include private text, paths, or secrets.",
        _audited_handler(
            conn,
            class_id=class_id,
            session_id=session_id,
            name="search_web",
            capability="web_read",
            effect="network_read",
            activity=activity,
            authorize=research_auth,
            action=search_action,
        ),
        properties={"query": {"type": "string", "maxLength": 500}},
        required=("query",),
    )
    definitions.append(
        _annotated(
            search_definition,
            capability="web_read",
            effect="network_read",
            trust="public_web",
        )
    )

    if snapshot.firecrawl_scrape_enabled:

        def fetch_action(capabilities: WriterCapabilities, *, url: str) -> _Outcome:
            url = _text(url, "url", maximum=source_ledger.MAX_URL_CHARS)
            fetched = web_research.fetch_source(
                url,
                allowed=True,
                firecrawl_base_url=capabilities.firecrawl_base_url,
                scrape_enabled=True,
            )
            fetch_id = uuid.uuid4().hex
            source = FetchedSource(
                fetch_id=fetch_id,
                url=str(fetched["url"]),
                final_url=str(fetched["final_url"]),
                title=str(fetched["title"]),
                accessed_at=str(fetched["accessed_at"]),
                content_type=(
                    str(fetched["content_type"]) if fetched.get("content_type") else None
                ),
                snapshot=str(fetched["snapshot"]),
                truncated=bool(fetched.get("truncated")),
                warning=str(fetched["warning"]) if fetched.get("warning") else None,
            )
            activity.fetched_sources[fetch_id] = source
            preview = source.snapshot[:FETCH_PREVIEW_CHARS]
            return _Outcome(
                success(
                    fetch_id=fetch_id,
                    url=source.url,
                    final_url=source.final_url,
                    title=source.title,
                    accessed_at=source.accessed_at,
                    content_type=source.content_type,
                    preview=preview,
                    preview_truncated=len(source.snapshot) > len(preview),
                    source_truncated=source.truncated,
                    warning=source.warning,
                ),
                {
                    "fetch_id": fetch_id,
                    "snapshot_chars": len(source.snapshot),
                    "snapshot_sha256": hashlib.sha256(source.snapshot.encode()).hexdigest(),
                    "truncated": source.truncated,
                },
                target_kind="fetched_source",
                target_id=fetch_id,
            )

        fetch_definition = _definition(
            "fetch_source",
            "Fetch one public search result into a bounded temporary snapshot for review.",
            _audited_handler(
                conn,
                class_id=class_id,
                session_id=session_id,
                name="fetch_source",
                capability="web_read",
                effect="network_read",
                activity=activity,
                authorize=scrape_auth,
                action=fetch_action,
            ),
            properties={"url": {"type": "string", "maxLength": 4096}},
            required=("url",),
        )
        definitions.append(
            _annotated(
                fetch_definition,
                capability="web_read",
                effect="network_read",
                trust="public_web",
            )
        )

    def propose_snapshot_action(_authorization: object, *, fetch_id: str) -> _Outcome:
        fetch_id = _text(fetch_id, "fetch_id", maximum=64)
        fetched = activity.fetched_sources.get(fetch_id)
        if fetched is None:
            raise _RefusalError("That fetched source is not available in this agent turn.")
        stored = source_ledger.upsert_source(
            conn,
            class_id,
            source_type=source_ledger.WEB,
            title=fetched.title,
            url=fetched.url,
            accessed_at=fetched.accessed_at,
            snapshot=fetched.snapshot,
            final_url=fetched.final_url,
            content_type=fetched.content_type,
            snapshot_hash=hashlib.sha256(fetched.snapshot.encode()).hexdigest(),
            truncated=fetched.truncated,
        )
        source_id = int(stored["id"])
        if source_id not in activity.source_ids:
            activity.source_ids.append(source_id)
        return _Outcome(
            success(source=source_ledger.prompt_source(stored)),
            {"source_id": source_id, "fetch_id": fetch_id},
            target_kind="source",
            target_id=str(source_id),
        )

    snapshot_definition = _definition(
        "propose_source_snapshot",
        "Propose a fetched snapshot for the class source ledger; this does not change a profile.",
        _audited_handler(
            conn,
            class_id=class_id,
            session_id=session_id,
            name="propose_source_snapshot",
            capability="source_proposal",
            effect="database_proposal",
            activity=activity,
            authorize=research_auth,
            action=propose_snapshot_action,
        ),
        properties={"fetch_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        required=("fetch_id",),
    )
    definitions.append(
        _annotated(
            snapshot_definition,
            capability="source_proposal",
            effect="database_proposal",
            trust="database",
        )
    )

    def propose_excerpt_action(
        _authorization: object,
        *,
        source_id: int,
        excerpt: str,
        section_ref: str = "",
    ) -> _Outcome:
        source_id = _integer(source_id, "source_id", minimum=1, maximum=2**63 - 1)
        excerpt = _text(excerpt, "excerpt", maximum=source_ledger.MAX_RELIED_EXCERPT_CHARS)
        section_ref = _text(
            section_ref,
            "section_ref",
            maximum=source_ledger.MAX_SECTION_REF_CHARS,
            allow_blank=True,
        )
        source_ledger.get_source(conn, source_id, class_id=class_id)
        stored = source_ledger.add_excerpt(
            conn, source_id, excerpt, section_ref=section_ref or None
        )
        return _Outcome(
            success(excerpt=stored),
            {"source_id": source_id, "excerpt_id": int(stored["id"])},
            target_kind="source_excerpt",
            target_id=str(stored["id"]),
        )

    excerpt_definition = _definition(
        "propose_source_excerpt",
        "Record one exact relied-on passage from a class source. This does not change a profile.",
        _audited_handler(
            conn,
            class_id=class_id,
            session_id=session_id,
            name="propose_source_excerpt",
            capability="source_proposal",
            effect="database_proposal",
            activity=activity,
            authorize=research_auth,
            action=propose_excerpt_action,
        ),
        properties={
            "source_id": {"type": "integer", "minimum": 1},
            "excerpt": {"type": "string", "maxLength": source_ledger.MAX_RELIED_EXCERPT_CHARS},
            "section_ref": {
                "type": "string",
                "maxLength": source_ledger.MAX_SECTION_REF_CHARS,
                "default": "",
            },
        },
        required=("source_id", "excerpt"),
    )
    definitions.append(
        _annotated(
            excerpt_definition,
            capability="source_proposal",
            effect="database_proposal",
            trust="database",
        )
    )

    def propose_profile_fact_action(
        _authorization: object,
        *,
        kind: str,
        label: str,
        value: str,
        source_id: int,
        excerpt_id: int,
    ) -> _Outcome:
        kind = _text(kind, "kind", maximum=32)
        label = _text(label, "label", maximum=500)
        value = _text(value, "value", maximum=4_000)
        source_id = _integer(source_id, "source_id", minimum=1, maximum=2**63 - 1)
        excerpt_id = _integer(excerpt_id, "excerpt_id", minimum=1, maximum=2**63 - 1)
        fact = profiles.propose_ledger_fact(
            conn,
            class_id,
            kind=kind,
            label=label,
            value=value,
            source_id=source_id,
            excerpt_id=excerpt_id,
        )
        fact_id = int(fact["id"])
        if fact_id not in activity.profile_fact_ids:
            activity.profile_fact_ids.append(fact_id)
        return _Outcome(
            success(
                fact_id=fact_id,
                confirmed=bool(fact["confirmed"]),
                active=bool(fact["active"]),
                note=(
                    "This fact already exists in active class context."
                    if fact["active"]
                    else "This fact is not used until the student confirms it."
                ),
            ),
            {"fact_id": fact_id, "source_id": source_id, "excerpt_id": excerpt_id},
            target_kind="profile_fact",
            target_id=str(fact_id),
        )

    profile_definition = _definition(
        "propose_profile_fact",
        "Propose an unconfirmed class-profile fact backed by one exact web-source excerpt.",
        _audited_handler(
            conn,
            class_id=class_id,
            session_id=session_id,
            name="propose_profile_fact",
            capability="profile_proposal",
            effect="database_proposal",
            activity=activity,
            authorize=research_auth,
            action=propose_profile_fact_action,
        ),
        properties={
            "kind": {
                "type": "string",
                "enum": ["deadline", "topic", "grading", "professor", "prerequisite", "note"],
            },
            "label": {"type": "string", "minLength": 1, "maxLength": 500},
            "value": {"type": "string", "minLength": 1, "maxLength": 4_000},
            "source_id": {"type": "integer", "minimum": 1},
            "excerpt_id": {"type": "integer", "minimum": 1},
        },
        required=("kind", "label", "value", "source_id", "excerpt_id"),
    )
    definitions.append(
        _annotated(
            profile_definition,
            capability="profile_proposal",
            effect="database_proposal",
            trust="database",
        )
    )


def _add_code_tools(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    definitions: list[tool_profiles.AnnotatedToolDefinition],
    activity: AgentRunActivity,
    snapshot: AgentCapabilitySnapshot,
) -> None:
    # The frozen snapshot decides which workspace schemas are exposed; each handler still
    # re-reads the live workspace row and its grants at dispatch via `_workspace_authorization`.
    if not snapshot.workspace_present or not snapshot.workspace_read_enabled:
        return

    def read_auth() -> object:
        return _workspace_authorization(conn, class_id, session_id, "read_enabled")

    read_tools: tuple[
        tuple[str, str, Mapping[str, object], Sequence[str], Callable[..., dict[str, object]]], ...
    ] = (
        (
            "list_workspace",
            "List bounded, non-secret entries beneath a relative workspace directory.",
            {
                "relative_path": {"type": "string", "default": ".", "maxLength": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 200},
            },
            (),
            lambda root, relative_path=".", limit=200: workspace_paths.list_workspace(
                root, relative_path, limit=limit
            ),
        ),
        (
            "search_workspace",
            "Search bounded text matches beneath the workspace without invoking a shell.",
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "relative_path": {"type": "string", "default": ".", "maxLength": 1000},
                "glob": {"type": "string", "maxLength": 200, "default": ""},
            },
            ("query",),
            lambda root, query, relative_path=".", glob="": workspace_paths.search_workspace(
                root, query, glob or None, relative_path=relative_path
            ),
        ),
        (
            "read_workspace_file",
            "Read a bounded line range from one non-secret workspace text file.",
            {
                "relative_path": {"type": "string", "minLength": 1, "maxLength": 1000},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ("relative_path",),
            lambda root, relative_path, start_line=1, end_line=None: (
                workspace_paths.read_workspace_file(
                    root, relative_path, start_line=start_line, end_line=end_line
                )
            ),
        ),
    )
    for name, description, properties, required, primitive in read_tools:

        def read_action(
            authorization: object,
            _primitive: Callable[..., dict[str, object]] = primitive,
            _name: str = name,
            **arguments: object,
        ) -> _Outcome:
            workspace_row = dict(authorization)  # type: ignore[arg-type]
            if _name == "list_workspace":
                arguments["relative_path"] = _text(
                    arguments.get("relative_path", "."),
                    "relative_path",
                    maximum=1_000,
                )
                arguments["limit"] = _integer(
                    arguments.get("limit", 200), "limit", minimum=1, maximum=200
                )
            elif _name == "search_workspace":
                arguments["query"] = _text(arguments.get("query"), "query", maximum=500)
                arguments["relative_path"] = _text(
                    arguments.get("relative_path", "."),
                    "relative_path",
                    maximum=1_000,
                )
                arguments["glob"] = _text(
                    arguments.get("glob", ""), "glob", maximum=200, allow_blank=True
                )
            else:
                arguments["relative_path"] = _text(
                    arguments.get("relative_path"), "relative_path", maximum=1_000
                )
                arguments["start_line"] = _integer(
                    arguments.get("start_line", 1),
                    "start_line",
                    minimum=1,
                    maximum=2**31 - 1,
                )
                if arguments.get("end_line") is not None:
                    arguments["end_line"] = _integer(
                        arguments["end_line"],
                        "end_line",
                        minimum=1,
                        maximum=2**31 - 1,
                    )
            result = _primitive(Path(str(workspace_row["root_path"])), **arguments)
            return _Outcome(
                success(**result),
                {
                    "path": result.get("path"),
                    "truncated": bool(result.get("truncated")),
                    "entry_count": len(result.get("entries", [])),
                    "match_count": len(result.get("matches", [])),
                    "content_chars": len(str(result.get("content", ""))),
                },
                target_kind="workspace_path",
                target_id=str(result.get("path", ".")),
            )

        definition = _definition(
            name,
            description,
            _audited_handler(
                conn,
                class_id=class_id,
                session_id=session_id,
                name=name,
                capability="workspace_read",
                effect="filesystem_read",
                activity=activity,
                authorize=read_auth,
                action=read_action,
            ),
            properties=properties,
            required=required,
        )
        definitions.append(
            _annotated(
                definition,
                capability="workspace_read",
                effect="filesystem_read",
                trust="workspace",
            )
        )

    if not snapshot.workspace_change_proposals_enabled:
        return

    def change_auth() -> object:
        return _workspace_authorization(conn, class_id, session_id, "change_proposals_enabled")

    def change_action(
        authorization: object,
        *,
        relative_path: str,
        observed_base_hash: str,
        proposed_content: str,
        rationale: str = "",
    ) -> _Outcome:
        workspace_row = dict(authorization)  # type: ignore[arg-type]
        relative_path = _text(relative_path, "relative_path", maximum=1_000)
        observed_base_hash = _text(observed_base_hash, "observed_base_hash", maximum=64)
        if len(observed_base_hash) != 64:
            raise _RefusalError("observed_base_hash must be a SHA-256 digest.")
        proposed_content = _text(
            proposed_content,
            "proposed_content",
            maximum=workspace_paths.MAX_TEXT_FILE_BYTES,
            allow_blank=True,
        )
        rationale = _text(
            rationale,
            "rationale",
            maximum=agent_store.MAX_REASON_CHARS,
            allow_blank=True,
        )
        proposal = workspace_changes.build_workspace_proposal(
            Path(str(workspace_row["root_path"])),
            relative_path,
            observed_base_hash,
            proposed_content,
        )
        stored = agent_store.create_workspace_change(
            conn,
            class_id,
            workspace_id=int(workspace_row["id"]),
            session_id=session_id,
            relative_path=proposal.relative_path,
            base_hash=proposal.base_hash,
            base_content=proposal.base_content,
            proposed_content=proposal.proposed_content,
            file_device=proposal.identity.device,
            file_inode=proposal.identity.inode,
            file_mode=proposal.file_mode,
            newline=proposal.newline,
            rationale=rationale or None,
        )
        change_id = int(stored["id"])
        activity.workspace_change_ids.append(change_id)
        return _Outcome(
            success(
                change_id=change_id,
                relative_path=proposal.relative_path,
                base_hash=proposal.base_hash,
                state=stored["state"],
                hunk_count=len(proposal.hunks),
            ),
            {
                "change_id": change_id,
                "relative_path": proposal.relative_path,
                "hunk_count": len(proposal.hunks),
            },
            target_kind="workspace_change",
            target_id=str(change_id),
        )

    change_definition = _definition(
        "create_workspace_change",
        "Create an inert full-text workspace change proposal for later student review.",
        _audited_handler(
            conn,
            class_id=class_id,
            session_id=session_id,
            name="create_workspace_change",
            capability="change_proposal",
            effect="database_proposal",
            activity=activity,
            authorize=change_auth,
            action=change_action,
        ),
        properties={
            "relative_path": {"type": "string", "minLength": 1, "maxLength": 1000},
            "observed_base_hash": {"type": "string", "minLength": 64, "maxLength": 64},
            "proposed_content": {
                "type": "string",
                "maxLength": workspace_paths.MAX_TEXT_FILE_BYTES,
            },
            "rationale": {"type": "string", "maxLength": 2000, "default": ""},
        },
        required=("relative_path", "observed_base_hash", "proposed_content"),
    )
    definitions.append(
        _annotated(
            change_definition,
            capability="change_proposal",
            effect="database_proposal",
            trust="database",
        )
    )


def _add_command_tools(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    definitions: list[tool_profiles.AnnotatedToolDefinition],
    activity: AgentRunActivity,
    snapshot: AgentCapabilitySnapshot,
) -> None:
    # Exposure is decided by the frozen snapshot; the handler re-reads the live command
    # grant at dispatch via `_workspace_authorization`.
    if not snapshot.workspace_present or not snapshot.workspace_commands_enabled:
        return

    def command_auth() -> object:
        return _workspace_authorization(conn, class_id, session_id, "commands_enabled")

    def command_action(
        authorization: object,
        *,
        argv: list[str],
        relative_cwd: str,
        reason: str,
        expected_signal: str = "",
        timeout_seconds: int = agent_store.DEFAULT_TIMEOUT_SECONDS,
    ) -> _Outcome:
        workspace_row = dict(authorization)  # type: ignore[arg-type]
        relative_cwd = _text(relative_cwd, "relative_cwd", maximum=1_000)
        reason = _text(reason, "reason", maximum=agent_store.MAX_REASON_CHARS)
        expected_signal = _text(
            expected_signal,
            "expected_signal",
            maximum=agent_store.MAX_EXPECTED_SIGNAL_CHARS,
            allow_blank=True,
        )
        timeout_seconds = _integer(
            timeout_seconds,
            "timeout_seconds",
            minimum=1,
            maximum=agent_store.MAX_TIMEOUT_SECONDS,
        )
        stored = agent_store.create_command_request(
            conn,
            class_id,
            workspace_id=int(workspace_row["id"]),
            session_id=session_id,
            argv=argv,
            relative_cwd=relative_cwd,
            reason=reason,
            expected_signal=expected_signal or None,
            timeout_seconds=timeout_seconds,
        )
        request_id = int(stored["id"])
        activity.command_request_ids.append(request_id)
        return _Outcome(
            success(
                request_id=request_id,
                argv=stored["argv"],
                relative_cwd=stored["relative_cwd"],
                reason=stored["reason"],
                expected_signal=stored["expected_signal"],
                timeout_seconds=stored["timeout_seconds"],
                state=stored["state"],
            ),
            {
                "request_id": request_id,
                "argv_count": len(stored["argv"]),
                "relative_cwd": stored["relative_cwd"],
                "state": stored["state"],
            },
            target_kind="command_request",
            target_id=str(request_id),
        )

    command_definition = _definition(
        "create_command_request",
        "Create an inert exact-argv command request. It cannot run without separate user "
        "confirmation.",
        _audited_handler(
            conn,
            class_id=class_id,
            session_id=session_id,
            name="create_command_request",
            capability="command_proposal",
            effect="database_proposal",
            activity=activity,
            authorize=command_auth,
            action=command_action,
        ),
        properties={
            "argv": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "minItems": 1,
                "maxItems": 128,
            },
            "relative_cwd": {"type": "string", "minLength": 1, "maxLength": 1000},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "expected_signal": {"type": "string", "maxLength": 1000, "default": ""},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": agent_store.MAX_TIMEOUT_SECONDS,
                "default": agent_store.DEFAULT_TIMEOUT_SECONDS,
            },
        },
        required=("argv", "relative_cwd", "reason"),
    )
    definitions.append(
        _annotated(
            command_definition,
            capability="command_proposal",
            effect="database_proposal",
            trust="database",
        )
    )


def registry_names(registry: Mapping[str, ToolDefinition]) -> frozenset[str]:
    """Small public helper for static profile-enumeration checks."""
    return frozenset(registry)
