"""Versioned synthetic writer evaluations through HTTP-started durable desktop runs.

No normal student profile/config is read. Explicit --config-db may name an isolated,
nonsecret handoff DB containing endpoint_url/model/context_window only. Output excludes
endpoint addresses and credentials. Fault-provider evidence is transport evidence only.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import sqlite_vec

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}
SCENARIOS = (
    "uninterrupted",
    "restart_inference",
    "restart_between_sections",
    "restart_review",
    "restart_persistence",
    "edit_restart",
    "edit_cancel_retry",
    "cancel_retry",
    "rate_limit",
    "transient",
    "partial_stream",
    "malformed_stream",
    "empty",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted((root / "backend").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".sql") and "tests" not in path.parts:
            hasher.update(str(path.relative_to(root)).encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def isolated_environment(profile: Path) -> dict[str, str]:
    # Explicit overrides prevent inherited student-profile settings from escaping isolation.
    env = {k: v for k, v in os.environ.items() if not k.startswith("LYRA_")}
    env.update(
        {
            "LYRA_DATA_DIR": str(profile / "data"),
            "LYRA_DB_PATH": str(profile / "data/lyra.db"),
            "LYRA_CACHE_DIR": str(profile / "cache"),
            "LYRA_LOGS_DIR": str(profile / "logs"),
            "LYRA_MODELS_DIR": str(profile / "models"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
    )
    return env


class DesktopBackend:
    """Real authenticated inherited socket; restart preserves only this synthetic profile."""

    def __init__(
        self,
        profile: Path,
        source_root: Path,
        executable: Path | None = None,
        embedding_port: int | None = None,
    ):
        self.profile, self.source_root, self.executable = profile, source_root, executable
        self.process: subprocess.Popen | None = None
        self.listener: socket.socket | None = None
        self.secret = ""
        if embedding_port is None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                embedding_port = reservation.getsockname()[1]
        self.embedding_port = embedding_port

    def start(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.secret = secrets.token_hex(32)
        command = (
            [str(self.executable)]
            if self.executable
            else [sys.executable, "-m", "backend.desktop_entry"]
        )
        environment = isolated_environment(self.profile)
        environment["LYRA_LLAMA_PORT"] = str(self.embedding_port)
        self.process = subprocess.Popen(  # noqa: S603 - explicit artifact/source root
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=self.source_root,
            pass_fds=(self.listener.fileno(),),
            env=environment,
        )
        bootstrap = {
            "protocol_version": 1,
            "socket_fd": self.listener.fileno(),
            "parent_pid": os.getpid(),
            "listener_addr": f"127.0.0.1:{self.port}",
            "session_header_name": "X-Lyra-Session",
            "session_secret": self.secret,
        }
        self.process.stdin.write(json.dumps(bootstrap) + "\n")
        self.process.stdin.close()
        readable, _, _ = select.select([self.process.stdout], [], [], 40)
        if not readable:
            raise TimeoutError("isolated backend readiness timeout")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("isolated backend exited before readiness; no private logs retained")
        ready = json.loads(line)
        if (
            any(
                ready.get(k) != bootstrap[k]
                for k in (
                    "protocol_version",
                    "listener_addr",
                    "session_header_name",
                    "session_secret",
                )
            )
            or ready.get("status") != "ready"
            or not ready.get("inherited_socket")
        ):
            raise RuntimeError("invalid desktop readiness handshake")
        if self.request("GET", "/api/health/live") != {"status": "ok"}:
            raise RuntimeError("authenticated desktop health failed")

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=45)
        try:
            conn.request(
                method,
                path,
                json.dumps(payload) if payload is not None else None,
                {
                    "X-Lyra-Session": self.secret,
                    "X-Lyra-Client": "writer-eval",
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            body = response.read()
            if response.status >= 400:
                # Do not copy server/provider errors, which can contain endpoint URLs.
                raise RuntimeError(f"HTTP {response.status} on {method} {path}")
            return json.loads(body) if body else None
        finally:
            conn.close()

    def stop(self, *, crash: bool = False) -> None:
        if self.process is not None:
            if crash:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None

    def restart(self) -> None:
        self.stop(crash=True)
        self.start()


def expand_body(case: dict[str, Any]) -> str:
    body = case["body"]
    repeat = case.get("body_repeat")
    if repeat:
        body += "\n\n" + "## " + repeat.get("section_title", "Appendix") + "\n\n"
        body += "\n\n".join(
            repeat["paragraph"].format(index=i)
            for i in range(
                repeat.get("index_start", 1), repeat.get("index_start", 1) + repeat["repeat_count"]
            )
        )
    return body


SEED_SCRIPT = """
import json, sqlite3, sys
from pathlib import Path
from backend.core import source_ledger, comments
payload = json.load(sys.stdin)
conn = sqlite3.connect(payload["database"])
conn.row_factory = sqlite3.Row
conn.execute("pragma foreign_keys=on")
ids = {}
document_ids = {}
for source in payload["sources"]:
    source_type = source.get("source_type", "web")
    document_id = None
    if source_type == "course":
        path = Path(payload["database"]).parent / "uploads" / (source["key"] + ".txt")
        path.parent.mkdir(exist_ok=True)
        path.write_text(source["content"])
        document_id = conn.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, ?, ?, 'text/plain', ?, 'ready')",
            (payload["class_id"], path.name, str(path), len(source["content"].encode()))).lastrowid
        conn.execute(
            "insert into chunks (document_id, class_id, content, token_count, doc_type, "
            "embedding_model, embedding_dim) values (?, ?, ?, ?, 'notes', 'fixture', 768)",
            (document_id, payload["class_id"], source["content"], len(source["content"]) // 4))
        document_ids[source["key"]] = document_id
        conn.commit()
    row = source_ledger.upsert_source(conn, payload["class_id"], source_type=source_type,
        document_id=document_id, title=source["title"],
        url="https://synthetic.invalid/" + source["key"] if source_type == "web" else None,
        snapshot=source["content"], accessed_at="2026-09-06T00:00:00Z",
        excerpts=[{"excerpt": e["text"]} for e in source.get("excerpts", [])])
    ids[source["key"]] = row["id"]
for mutation in payload.get("source_mutations", []):
    key = mutation.get("source_key", mutation.get("key"))
    source = next(s for s in payload["sources"] if s["key"] == key)
    operation = mutation.get("action", mutation.get("operation"))
    if operation == "delete_document":
        continue
    if operation == "delete":
        raise ValueError("Use actual HTTP delete_document for course source deletion")
    else:
        source_ledger.upsert_source(conn, payload["class_id"], source_type="web",
            title=source["title"], url="https://synthetic.invalid/" + key,
            snapshot=mutation.get("content", mutation.get("replacement_content", "")),
            accessed_at="2026-09-06T01:00:00Z")
part = conn.execute(
    "select id, content from artifact_parts where artifact_id=? and kind='draft_body'",
    (payload["artifact_id"],)).fetchone()
for comment in payload.get("initial_comments", []):
    quote = comment.get("quote")
    hint = part["content"].find(quote) if quote else None
    comments.add_comment(conn, part["id"], comments.REVIEWER, comment["body"],
        severity=comment.get("severity"), quote=quote,
        hint=hint if hint is not None and hint >= 0 else None,
        section_ref=comment.get("section_ref"))
print(json.dumps({"source_ids": ids, "document_ids": document_ids}))
conn.close()
"""


def seed(backend: DesktopBackend, case: dict[str, Any]) -> tuple[int, int, str]:
    cid = backend.request("POST", "/api/classes", {"name": "Synthetic writer evaluation"})["id"]
    draft = backend.request("POST", f"/api/classes/{cid}/drafts", {"title": case["id"]})
    aid = draft["id"]
    body = expand_body(case)
    backend.request(
        "PATCH",
        f"/api/drafts/{aid}/body",
        {"content": body, "expected_version": 0, "snapshot": True},
    )
    backend.request("PUT", f"/api/drafts/{aid}/brief", case["brief"])
    data = {
        "database": str(backend.profile / "data/lyra.db"),
        "class_id": cid,
        "artifact_id": aid,
        "initial_comments": case.get("initial_comments", []),
        "sources": case.get("sources", []),
        "source_mutations": case.get("source_mutations", []),
    }
    result = subprocess.run(  # noqa: S603 - fixed seed program, synthetic input
        [sys.executable, "-c", SEED_SCRIPT],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        cwd=backend.source_root,
        env=isolated_environment(backend.profile),
    )
    if result.returncode:
        raise RuntimeError("synthetic source seeding failed: " + result.stderr[-1500:])
    seeded = json.loads(result.stdout)
    ids = seeded["source_ids"]
    for mutation in case.get("source_mutations", []):
        if mutation["operation"] == "delete_document":
            document_id = seeded["document_ids"][mutation["source_key"]]
            backend.request("DELETE", f"/api/documents/{document_id}")
    if case.get("plan"):
        plan = json.loads(json.dumps(case["plan"]))
        for section in plan["sections"]:
            section["sources"] = [ids[key] for key in section.pop("source_keys", [])]
        backend.request("PUT", f"/api/drafts/{aid}/plan", plan)
    return aid, cid, body


def capture(backend: DesktopBackend, aid: int, cid: int) -> dict[str, Any]:
    result = {
        name: backend.request("GET", f"/api/drafts/{aid}{suffix}")
        for name, suffix in (
            ("draft", ""),
            ("status", "/status"),
            ("comments", "/comments"),
            ("pending", "/pending"),
            ("live_suggestion", "/live-suggestion"),
            ("plan", "/plan"),
        )
    }
    result["sources"] = backend.request("GET", f"/api/classes/{cid}/sources")
    conn = sqlite3.connect(backend.profile / "data/lyra.db")
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
    result["durable_rows"] = {
        table: [dict(row) for row in conn.execute(f'select * from "{table}"')]  # noqa: S608 - sqlite-owned names
        for table in tables
        if table.startswith(("writer_run", "writer_source", "draft_plan", "live_draft"))
    }
    conn.close()
    return result


def deterministic_metrics(
    case: dict[str, Any], before: str, after: dict[str, Any]
) -> dict[str, Any]:
    body = after["draft"]["body"]
    comments = after.get("comments", [])
    if isinstance(comments, dict):
        comments = comments.get("comments", [])
    signatures = [(c.get("quote"), c.get("severity"), c.get("body")) for c in comments]
    protected = case.get("expected", {}).get("protected_passages", [])
    candidate = (after.get("pending") or {}).get("proposed_content")
    rows = after.get("durable_rows", {})
    revisions = {r["id"]: r for r in rows.get("writer_source_revisions", [])}
    excerpts = rows.get("writer_source_excerpts", [])
    unsupported_excerpts = [
        e["id"]
        for e in excerpts
        if e.get("source_revision_id") is not None
        and (
            e["source_revision_id"] not in revisions
            or e["excerpt"] not in str(revisions[e["source_revision_id"]].get("snapshot", ""))
        )
    ]
    return {
        "candidate_present": candidate is not None,
        "candidate_protected_passages_preserved": sum(p in candidate for p in protected)
        if candidate is not None
        else None,
        "candidate_forbidden_literal_hits": [
            p for p in case.get("expected", {}).get("forbidden_output", []) if p in candidate
        ]
        if candidate is not None
        else None,
        "excerpt_revision_support_violations": unsupported_excerpts,
        "body_unchanged": before == body,
        "protected_passages_total": len(protected),
        "protected_passages_preserved": sum(p in body for p in protected),
        "duplicate_comment_signatures": sum(n - 1 for n in Counter(signatures).values()),
        "real_run_id_present": isinstance(after["status"].get("run_id"), int),
        "missed_seeded_issues": None,
        "unsupported_rewrites": None,
        "unrelated_candidate_edits": None,
        "citation_support": None,
        "seed_metrics_note": "Semantic measures await independent/human review.",
    }


def section_boundary(
    status: dict[str, Any],
    live: dict[str, Any] | None,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Require a finished SECTION, not merely a finished paragraph or reviewer lens."""
    if operation != "pass":
        return None
    selected = payload.get("sections", [])
    if selected:
        done = status.get("problems_done") or 0
        if len(selected) > 1 and 0 < done < len(selected):
            return {
                "kind": "selected_section_counter",
                "selected_sections": selected,
                "completed_section_count": done,
                "next_selected_section": selected[done],
            }
        return None
    if not live or live.get("run_id") != status.get("run_id"):
        return None
    blocks = live.get("blocks", [])
    unfinished = next((i for i, b in enumerate(blocks) if b.get("status") != "complete"), None)
    if unfinished is None or unfinished == 0:
        return None
    previous = blocks[unfinished - 1].get("section_ref")
    following = blocks[unfinished].get("section_ref")
    if not previous or not following or previous == following:
        return None
    preceding = [b for b in blocks if b.get("section_ref") == previous]
    if not preceding or any(b.get("status") != "complete" for b in preceding):
        return None
    return {
        "kind": "complete_section_before_next_unfinished_section",
        "completed_section_ref": previous,
        "next_section_ref": following,
        "completed_block_ids": [b["id"] for b in preceding],
        "first_unfinished_block_id": blocks[unfinished]["id"],
        "live_snapshot": live,
    }


def retained_user_edit(edit: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    live_blocks = (after.get("live_suggestion") or {}).get("blocks", [])
    historical = after.get("durable_rows", {}).get("live_draft_blocks", [])

    def retained(block: dict[str, Any]) -> bool:
        return (
            block.get("content") == edit["edited_content"]
            and (block.get("user_revision") or 0) >= edit["user_revision"]
        )

    return {
        "current_live_user_edit_preserved": any(retained(b) for b in live_blocks),
        "historical_user_edit_preserved": any(
            b.get("id") == edit["block_id"] and retained(b) for b in historical
        ),
        "historical_matching_block_ids": [b["id"] for b in historical if retained(b)],
    }


def run_case(
    args: argparse.Namespace,
    case: dict[str, Any],
    scenario: str,
    config: dict[str, Any],
    provider: Any = None,
) -> dict[str, Any]:
    profile = Path(tempfile.mkdtemp(prefix="lyra-writer-eval-"))
    backend = DesktopBackend(
        profile,
        args.source_root,
        args.backend_executable,
        embedding_port=provider.server.server_port if provider else None,
    )
    started = time.monotonic()
    if provider:
        provider.arm(None)
    evidence: dict[str, Any] = {
        "case_id": case["id"],
        "corpus_recovery_requirements": case.get("recovery_scenarios", []),
        "scenario": scenario,
        "evidence_kind": "deterministic_fault_provider" if provider else "real_provider",
        "subjective_scores": {d: None for d in args.dimensions},
        "independent_agent_review": None,
        "human_review": None,
        "timeline": [],
        "intervention_observed": False,
    }
    try:
        evidence["backend_source_sha256_at_start"] = source_digest(args.source_root)
        backend.start()
        settings = {
            "endpoint_url": config["endpoint_url"],
            "model": config["model"],
            "context_window": args.context_window
            or case.get("context_window_tokens")
            or config.get("context_window")
            or 32768,
            "remote_ack": args.allow_remote,
            "allow_web_research": False,
            "parallel_requests": False,
            "api_key": os.environ.get(args.api_key_env, ""),
        }
        backend.request("PUT", "/api/settings", settings)
        evidence["effective_context_window"] = settings["context_window"]
        evidence["tool_probe"] = backend.request("POST", "/api/settings/test-tools")
        seeded_case = dict(case)
        if getattr(args, "generate_plan", False):
            seeded_case.pop("plan", None)
        evidence["plan_mode"] = "generated" if not seeded_case.get("plan") else "corpus_seeded"
        aid, cid, body = seed(backend, seeded_case)
        evidence["before"] = capture(backend, aid, cid)
        operation = case["operation"]
        payload = case.get(f"{operation}_payload", {"depth": "quick"})
        if provider and scenario in (
            "rate_limit",
            "transient",
            "partial_stream",
            "malformed_stream",
            "empty",
        ):
            provider.arm(scenario)
        if provider and scenario in ("restart_inference", "cancel_retry"):
            provider.arm("slow")
        request_count = len(provider.requests) if provider else 0
        evidence["start_response"] = backend.request(
            "POST", f"/api/drafts/{aid}/{operation}", payload
        )
        previous = None
        retry_started = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            status = backend.request("GET", f"/api/drafts/{aid}/status")
            if provider and getattr(args, "retry_failures", False):
                evidence["intervention_observed"] = evidence["intervention_observed"] or any(
                    event.get("fault") == scenario for event in provider.requests[request_count:]
                )
            if status != previous:
                evidence["timeline"].append(
                    {"elapsed_seconds": round(time.monotonic() - started, 3), **status}
                )
                previous = status
            active = status.get("run_status") in ("queued", "running", "cancel_requested")
            observed_inference = (
                len(provider.requests) > request_count
                if provider
                else active and time.monotonic() - started > args.interrupt_after
            )
            boundary = None
            live = None
            if active and not evidence["intervention_observed"]:
                if scenario in ("restart_between_sections", "edit_restart", "edit_cancel_retry"):
                    live = backend.request("GET", f"/api/drafts/{aid}/live-suggestion")
                if scenario == "restart_between_sections":
                    boundary = section_boundary(status, live, operation, payload)
                elif (
                    scenario in ("edit_restart", "edit_cancel_retry")
                    and operation == "pass"
                    and live
                ):
                    complete = next(
                        (
                            b
                            for b in live.get("blocks", [])
                            if b.get("status") == "complete"
                            and b.get("kind") == "paragraph"
                            and b.get("content")
                        ),
                        None,
                    )
                    if live.get("run_id") == status.get("run_id") and complete:
                        boundary = {
                            "kind": "completed_live_block_before_user_edit",
                            "block": complete,
                            "live_snapshot": live,
                        }
                elif scenario == "restart_persistence" and operation == "review":
                    persisted = backend.request("GET", f"/api/drafts/{aid}/comments")
                    initial_ids = {c["id"] for c in evidence["before"]["comments"]}
                    added = [c for c in persisted if c["id"] not in initial_ids]
                    if added:
                        boundary = {
                            "kind": "new_comment_observed_via_http",
                            "new_comment_ids": [c["id"] for c in added],
                            "comments_snapshot": persisted,
                        }
            trigger = (
                (scenario in ("restart_inference", "cancel_retry") and observed_inference)
                or boundary is not None
                or (
                    scenario == "restart_review"
                    and "review" in str(status.get("stage_detail", "")).lower()
                )
            )
            if trigger and active and not evidence["intervention_observed"]:
                if boundary and scenario in ("edit_restart", "edit_cancel_retry"):
                    block = boundary["block"]
                    edited = block["content"].rstrip() + (
                        " I want this paragraph to keep my uncertainty visible rather than "
                        "sound more certain than the evidence allows."
                    )
                    updated = backend.request(
                        "PATCH",
                        f"/api/drafts/{aid}/live-suggestion/blocks/{block['id']}",
                        {"expected_revision": block["revision"], "content": edited},
                    )
                    evidence["user_edit"] = {
                        "block_id": block["id"],
                        "stable_key": block.get("stable_key"),
                        "original_content": block["content"],
                        "edited_content": updated["content"],
                        "user_revision": updated["user_revision"],
                        "original_revision": block["revision"],
                        "edited_revision": updated["revision"],
                    }
                evidence["intervention_observed"] = True
                evidence["intervention_boundary"] = status
                if boundary:
                    evidence["observed_boundary"] = boundary
                evidence["boundary_evidence"] = (
                    "provider_request_observed" if provider else "active_status_elapsed_proxy"
                )
                evidence["at_interruption"] = capture(backend, aid, cid)
                captured_status = evidence["at_interruption"]["status"].get("run_status")
                if captured_status not in ("queued", "running", "cancel_requested"):
                    evidence["intervention_observed"] = False
                    evidence["not_run_reason"] = (
                        "Run settled before the observed boundary could be interrupted."
                    )
                    continue
                if boundary:
                    evidence["boundary_evidence"] = boundary["kind"]
                if scenario in ("cancel_retry", "edit_cancel_retry"):
                    backend.request("POST", f"/api/drafts/{aid}/cancel")
                else:
                    backend.restart()
                    backend.request("PUT", "/api/settings", settings)
                continue
            if status.get("run_status") in TERMINAL:
                if (
                    evidence["intervention_observed"]
                    and (
                        scenario.startswith(("restart_", "cancel_", "edit_"))
                        or getattr(args, "retry_failures", False)
                    )
                    and status.get("run_status") != "completed"
                    and not retry_started
                ):
                    evidence["after_interruption"] = capture(backend, aid, cid)
                    evidence["retry_response"] = backend.request(
                        "POST", f"/api/drafts/{aid}/{operation}", payload
                    )
                    retry_started = True
                    continue
                break
            time.sleep(0.08)
        else:
            evidence["harness_timeout"] = True
            backend.stop(crash=True)
            backend.start()
        evidence["after"] = capture(backend, aid, cid)
        evidence["deterministic"] = deterministic_metrics(case, body, evidence["after"])
        if evidence.get("user_edit"):
            evidence["deterministic"].update(
                retained_user_edit(evidence["user_edit"], evidence["after"])
            )
        if provider and scenario in (
            "rate_limit",
            "transient",
            "partial_stream",
            "malformed_stream",
            "empty",
        ):
            evidence["intervention_observed"] = any(
                r.get("fault") == scenario for r in provider.requests[request_count:]
            )
            if not evidence["intervention_observed"]:
                evidence["not_run_reason"] = (
                    "Requested fault boundary was not reached by this transport."
                )
        evidence["scenario_status"] = (
            "control"
            if scenario == "uninterrupted"
            else "observed"
            if evidence["intervention_observed"]
            else "not_run"
        )
        if evidence["scenario_status"] == "not_run":
            evidence.setdefault(
                "not_run_reason",
                "The requested durable boundary was not observed while the run was active.",
            )
        evidence["status"] = "recorded"
    except Exception as exc:
        evidence["status"] = "blocked"
        # Exceptions from HTTP are generated locally; other details may contain provider addresses.
        evidence["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        log = profile / "logs/backend.log"
        if log.is_file():
            evidence["isolated_backend_failure_detail"] = log.read_text(errors="replace")[-8000:]
        try:
            db = sqlite3.connect(profile / "data/lyra.db")
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            evidence["database_quick_check"] = [
                list(r) for r in db.execute("pragma integrity_check")
            ]
            db.close()
        except sqlite3.DatabaseError as database_error:
            evidence["database_diagnostic"] = str(database_error)
    finally:
        backend.stop()
        if (
            provider
            and getattr(args, "retain_profile_on_error", False)
            and evidence.get("status") == "blocked"
        ):
            print(f"Synthetic diagnostic profile retained: {profile}", file=sys.stderr, flush=True)
        else:
            shutil.rmtree(profile, ignore_errors=True)
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    evidence["backend_source_sha256_at_end"] = source_digest(args.source_root)
    evidence["code_changed_during_case"] = (
        evidence.get("backend_source_sha256_at_start") != evidence["backend_source_sha256_at_end"]
    )
    redactions = [
        config["endpoint_url"],
        urlsplit(config["endpoint_url"]).hostname or "",
        os.environ.get(args.api_key_env, ""),
        str(profile),
    ]
    return redact(evidence, redactions)


def redact(value: Any, sensitive: list[str]) -> Any:
    """Scrub configuration echoes from provider errors and model-generated content."""
    if isinstance(value, dict):
        return {k: redact(v, sensitive) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, sensitive) for v in value]
    if isinstance(value, str):
        for item in sorted((s for s in sensitive if s), key=len, reverse=True):
            value = value.replace(item, "<private>")
    return value


def configuration(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if args.config_db:
        conn = sqlite3.connect(args.config_db.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result = dict(
            conn.execute(
                "select endpoint_url, model, context_window from settings limit 1"
            ).fetchone()
        )
        conn.close()
    if args.endpoint:
        result["endpoint_url"] = args.endpoint
    if args.model:
        result["model"] = args.model
    if not result.get("endpoint_url") or not result.get("model"):
        raise ValueError("Provide --endpoint and --model or an explicit isolated --config-db")
    host = urlsplit(result["endpoint_url"]).hostname
    local = host in ("localhost", "127.0.0.1", "::1")
    result["locality"] = "loopback" if local else "non_loopback"
    if not local and not args.allow_remote:
        raise ValueError("Non-loopback provider requires explicit --allow-remote consent")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "scripts/eval_corpora/writer_quality.v1.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--backend-executable", type=Path)
    parser.add_argument("--config-db", type=Path)
    parser.add_argument("--endpoint", default=os.environ.get("LYRA_EVAL_ENDPOINT"))
    parser.add_argument("--model", default=os.environ.get("LYRA_EVAL_MODEL"))
    parser.add_argument("--api-key-env", default="LYRA_EVAL_API_KEY")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--fault-provider", action="store_true")
    parser.add_argument(
        "--retain-profile-on-error",
        action="store_true",
        help="Fault provider only; preserve synthetic error profile for storage diagnostics",
    )
    parser.add_argument("--case", action="append")
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Retry an observed injected failure once in the same profile",
    )
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--interrupt-after", type=float, default=1.0)
    parser.add_argument("--context-window", type=int)
    parser.add_argument(
        "--generate-plan",
        action="store_true",
        help="Omit corpus plan; exercise actual model planning",
    )
    args = parser.parse_args(argv)
    if args.retain_profile_on_error and not args.fault_provider:
        parser.error(
            "profile retention requires --fault-provider; real credentials are never retained"
        )
    corpus_bytes = args.corpus.read_bytes()
    corpus = json.loads(corpus_bytes)
    if corpus.get("version") != "writer-quality.v1" or not corpus.get("synthetic_only"):
        parser.error("requires the versioned, explicitly synthetic writer-quality.v1 corpus")
    args.dimensions = corpus["dimensions"]
    provider = None
    if args.fault_provider:
        from writer_eval_provider import FaultProvider

        provider = FaultProvider()
        args.endpoint, args.model = provider.endpoint, "synthetic-writer-v1"
    config = configuration(args)
    args.output.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(  # noqa: S603 - read-only git metadata
        [shutil.which("git") or "/usr/bin/git", "rev-parse", "HEAD"],
        cwd=args.source_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()  # noqa: S603,S607
    (args.output / "corpus.json").write_bytes(corpus_bytes)
    report: dict[str, Any] = {
        "version": "writer-evaluation.v1",
        "tested_sha": sha,
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "prompt_sha256": digest(args.source_root / "backend/llm/prompts.py"),
        "runtime_sha256": digest(args.backend_executable) if args.backend_executable else None,
        "harness_sha256": digest(Path(__file__)),
        "backend_source_sha256": source_digest(args.source_root),
        "generate_plan": args.generate_plan,
        "retry_failures": args.retry_failures,
        "model": config["model"],
        "locality": config["locality"],
        "context_window": args.context_window or config.get("context_window") or 32768,
        "runtime": "frozen_sidecar" if args.backend_executable else "source_desktop_bootstrap",
        "comparison_kind": "same_model_interruption",
        "model_comparison": None,
        "subjective_acceptance": "not_reviewed",
        "cases": [],
    }
    try:
        cases = [c for c in corpus["cases"] if not args.case or c["id"] in args.case]
        if not cases:
            parser.error("no matching corpus cases")
        for case in cases:
            for scenario in args.scenario or ["uninterrupted"]:
                evidence = run_case(args, case, scenario, config, provider)
                filename = f"{case['id']}--{scenario}.json"
                (args.output / filename).write_text(json.dumps(evidence, indent=2) + "\n")
                report["cases"].append(
                    {
                        "case_id": case["id"],
                        "scenario": scenario,
                        "status": evidence["status"],
                        "file": filename,
                        "run_id": evidence.get("after", {}).get("status", {}).get("run_id"),
                        "intervention_observed": evidence["intervention_observed"],
                        "scenario_status": evidence.get("scenario_status", "blocked"),
                    }
                )
                (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report["cases"][-1]), flush=True)
        if provider:
            (args.output / "provider-events.json").write_text(
                json.dumps(provider.requests, indent=2) + "\n"
            )
    finally:
        if provider:
            provider.close()
    return int(any(c["status"] == "blocked" for c in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
