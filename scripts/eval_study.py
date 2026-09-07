"""Run production study generation on synthetic documents; never read a student profile.

Use --profile with a separately authorized isolated settings database. Output contains
synthetic prompts and terminal answers only, never endpoint URLs or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def structural_checks(content: list[dict], selected_ids: list[int], kind: str) -> dict:
    """Mechanical safeguards only; never treat these as factual acceptance."""
    from backend.core import study

    stems = [
        study._dedupe_key(item["content"].get("question" if kind == "quiz" else "front", ""))
        for item in content
    ]
    provenance_ok = all(
        item["provenance"]
        and all(source["document_id"] in selected_ids for source in item["provenance"])
        for item in content
    )
    return {
        "published_count": len(content),
        "duplicate_stems": len(stems) - len(set(stems)),
        "selected_provenance_only": bool(content) and provenance_ok,
        "semantic_correctness": "not established by structural checks",
    }


def exercise_review(
    conn: sqlite3.Connection, artifact_id: int, parts: list[dict], kind: str
) -> dict:
    """Exercise real route handlers with synthetic ratings; no HTTP/UX acceptance claim."""
    from backend.api import routes_study as api
    from backend.core.errors import ConflictError

    if kind == "quiz":
        scores = []
        resume_ok = True
        retry_ok = True
        for all_correct in (False, True):
            attempt = api.start_attempt(artifact_id, conn)
            attempt_id = int(attempt["attempt_id"])
            for index, part in enumerate(parts):
                question = json.loads(part["content"])
                correct = int(question["correct_index"])
                choice = correct if all_correct or index % 2 == 0 else (correct + 1) % 4
                api.answer_question(
                    attempt_id, api.AnswerCreate(part_id=part["id"], selected_index=choice), conn
                )
                resumed = api.current_attempt(artifact_id, conn)["attempt"]
                resume_ok = (
                    resume_ok
                    and resumed["attempt_id"] == attempt_id
                    and len(resumed["answers"]) == index + 1
                )
            score = api.finish_attempt(attempt_id, conn)
            retry_ok = retry_ok and score == api.finish_attempt(attempt_id, conn)
            scores.append(score)
        return {
            "surface": "production route handlers, no HTTP",
            "attempt_results": scores,
            "resume_preserved": resume_ok,
            "finish_retry_identical": retry_ok,
            "semantic_keys": "model-produced keys; factual review separate",
        }
    part_id = int(parts[0]["id"])
    first = None
    last = None
    retry_ok = True
    for index in range(100):
        payload = api.CardReview(rating="easy", operation_id=f"synthetic-{index}")
        last = api.review_card(part_id, payload, conn)
        if first is None:
            first = last
        retry_ok = retry_ok and last == api.review_card(part_id, payload, conn)
    conflict = False
    try:
        api.review_card(part_id, api.CardReview(rating="good", operation_id="synthetic-0"), conn)
    except ConflictError:
        conflict = True
    return {
        "surface": "production route handlers, no HTTP",
        "first": first,
        "after_100_same_day": last,
        "retry_identical": retry_ok,
        "changed_rating_rejected": conflict,
        "review_rows": conn.execute(
            "select count(*) from card_review_log where part_id = ?", (part_id,)
        ).fetchone()[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "scripts/eval_corpora/study-beta.json"
    )
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--replay-report", type=Path)
    parser.add_argument("--replay-case", default="ecology")
    parser.add_argument("--replay-index", type=int, default=-1)
    parser.add_argument("--kinds", nargs="+", choices=["quiz", "deck"], default=["quiz", "deck"])
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--llama-port", type=int, default=18441)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Provider exceptions may include private URLs.
    if args.workspace.exists():
        parser.error("workspace must be new and isolated")
    args.workspace.mkdir(parents=True, mode=0o700)
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    os.environ["LYRA_DATA_DIR"] = str(args.workspace.resolve())
    os.environ["LYRA_DB_PATH"] = str((args.workspace / "lyra.db").resolve())
    os.environ["LYRA_CACHE_DIR"] = str((args.workspace / "cache").resolve())
    os.environ["LYRA_LOGS_DIR"] = str((args.workspace / "logs").resolve())
    os.environ["LYRA_LLAMA_PORT"] = str(args.llama_port)
    if args.models_dir:
        os.environ["LYRA_MODELS_DIR"] = str(args.models_dir.resolve())
    from backend.core import artifacts, study
    from backend.core.app_settings import resolve_tutor_access
    from backend.llm import client, prompts
    from backend.llm.budget import generation_reserve
    from backend.llm.locality import is_local_endpoint
    from backend.storage.database import connect, migrate

    conn = connect()
    migrate(conn)
    with sqlite3.connect(f"file:{args.profile.resolve()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        row = source.execute("select * from settings where id = 1").fetchone()
    columns = ["endpoint_url", "model", "context_window", "remote_ack", "tools_supported"]
    conn.execute(
        "update settings set endpoint_url = ?, model = ?, context_window = ?, "
        "remote_ack = ?, tools_supported = ? where id = 1",
        [row[key] for key in columns],
    )
    conn.commit()
    access = resolve_tutor_access(conn)
    if access.document_block or access.config is None:
        raise SystemExit("Isolated profile has no authorized tutor for study generation")
    config = access.config
    corpus = json.loads(args.corpus.read_text())
    report = {
        "corpus_version": corpus["version"],
        "corpus_sha256": digest(args.corpus),
        "rubric_version": corpus["rubric_version"],
        "critical_acceptance": corpus["critical_acceptance"],
        "sha": subprocess.check_output(  # noqa: S603 - fixed read-only git arguments
            [shutil.which("git") or "/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "study_sha256": digest(ROOT / "backend/core/study.py"),
        "runner_sha256": digest(Path(__file__)),
        "prompts_sha256": digest(ROOT / "backend/llm/prompts.py"),
        "study_prompt_sha256": hashlib.sha256(
            "\n".join(
                [prompts._TOPICS_PROMPT, prompts._FLASHCARDS_PROMPT, prompts._QUIZ_PROMPT]
            ).encode()
        ).hexdigest(),
        "app_version": json.loads((ROOT / "frontend/package.json").read_text())["version"],
        "configuration": {
            "model": config.model,
            "locality": "loopback" if is_local_endpoint(config.endpoint_url) else "remote",
            "tools_supported_stored": config.tools_supported,
            "context_window": config.context_window,
            "max_tokens": min(
                study._STUDY_OUTPUT_TOKEN_CAP, generation_reserve(config.context_window)
            ),
            "temperature": client.DETERMINISTIC_TEMPERATURE,
            "concurrency": 1,
            "request_timeout_seconds": {
                "read": client.BACKGROUND_TIMEOUT.read,
                "connect": client.BACKGROUND_TIMEOUT.connect,
            },
            "embedding": "real nomic local helper" if "deck" in args.kinds else "not used",
            "schema_capability": "production fallback unchanged; exact accepted format unobserved",
        },
        "review": {
            "deterministic": "per-case structural results only",
            "model_self_judging": "not run",
            "independent_agent": "pending",
            "human": "not run",
        },
        "results": [],
    }
    calls: list[dict] = []
    original_post = client._post_constrained

    async def capture(*pos, **kwargs):
        start = time.monotonic()
        record = {
            "messages": pos[4],
            "temperature": pos[6],
            "max_tokens": pos[5],
            "schema": getattr(pos[7], "name", None),
        }
        calls.append(record)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        try:
            payload = await original_post(*pos, **kwargs)
            choices = payload.get("choices", [])
            record["stop_reason"] = choices[0].get("finish_reason") if choices else None
            record["terminal_answer"] = (
                (choices[0].get("message") or {}).get("content") if choices else None
            )
            record["usage"] = payload.get("usage")
            record["response_model"] = payload.get("model")
            return payload
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["latency_seconds"] = round(time.monotonic() - start, 3)
            args.output.write_text(json.dumps(report, indent=2) + "\n")

    client._post_constrained = capture
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.replay_report:
            prior = json.loads(args.replay_report.read_text())
            case = next(
                item
                for item in prior["results"]
                if item["case"] == args.replay_case and item.get("calls")
            )
            captured = case["calls"][args.replay_index]
            report["replay"] = {
                "source_report_sha256": digest(args.replay_report),
                "case": args.replay_case,
                "call_index": args.replay_index,
                "scope": "single captured flashcard call; not full deck acceptance",
            }
            report["in_progress"] = {"calls": calls, "state": "running"}
            start = time.monotonic()
            outcome = {"case": args.replay_case, "kind": "call_replay", "calls": calls}
            try:
                outcome["returned_json"] = study._call_json(
                    config, captured["messages"], prompts.FLASHCARDS_SCHEMA
                )
                outcome["state"] = "returned"
            except Exception as exc:
                outcome["state"] = "failed"
                outcome["error_type"] = type(exc).__name__
            outcome["latency_seconds"] = round(time.monotonic() - start, 3)
            report["results"].append(outcome)
            report.pop("in_progress", None)
            print(json.dumps({key: outcome[key] for key in ("state", "latency_seconds")}))
            return
        for case in corpus["cases"]:
            if args.cases and case["id"] not in args.cases:
                continue
            class_id = conn.execute(
                "insert into classes (name) values (?)", ("Synthetic " + case["id"],)
            ).lastrowid
            sources = []
            for index, doc in enumerate(
                [
                    *case["documents"],
                    {"filename": "Excluded key.txt", "page": 1, "text": case["excluded"]},
                ]
            ):
                doc_id = conn.execute(
                    "insert into documents (class_id, filename, stored_path, mime, "
                    "byte_size, state) "
                    "values (?, ?, '/synthetic', 'text/plain', ?, 'ready')",
                    (class_id, doc["filename"], len(doc["text"])),
                ).lastrowid
                chunk_id = conn.execute(
                    "insert into chunks (document_id, class_id, content, token_count, page_number, "
                    "doc_type, embedding_model, embedding_dim) "
                    "values (?, ?, ?, ?, ?, 'generic', 'nomic-embed-text-v1.5.Q8_0', 768)",
                    (doc_id, class_id, doc["text"], max(1, len(doc["text"]) // 4), doc["page"]),
                ).lastrowid
                if "deck" in args.kinds:
                    import sqlite_vec

                    from backend.rag.embed import embed_documents

                    vector = embed_documents([doc["text"]])[0]
                    conn.execute(
                        "insert into chunk_embeddings (chunk_id, class_id, embedding) "
                        "values (?, ?, ?)",
                        (chunk_id, class_id, sqlite_vec.serialize_float32(vector)),
                    )
                if index < len(case["documents"]):
                    sources.append(doc_id)
            conn.commit()
            for kind in args.kinds:
                for repeat in range(args.repeat):
                    calls.clear()
                    artifact = artifacts.create_artifact(
                        conn,
                        class_id,
                        f"{case['id']} {kind}",
                        [
                            artifacts.SourceSpec(document_id=value, role=artifacts.STUDY_SOURCE)
                            for value in sources
                        ],
                        kind=artifacts.KIND_QUIZ
                        if kind == "quiz"
                        else artifacts.KIND_FLASHCARD_DECK,
                    )
                    job = study._Job(
                        int(artifact["id"]),
                        source_ids=tuple(sources),
                        count=5,
                        cards_per_topic=2,
                        difficulty=case["difficulty"],
                        types=("mcq",),
                    )
                    study.persist_job(conn, job, str(artifact["kind"]))
                    start = time.monotonic()
                    report["in_progress"] = {
                        "case": case["id"],
                        "kind": kind,
                        "repeat": repeat + 1,
                        "artifact_id": job.artifact_id,
                        "calls": calls,
                        "state": "running",
                        "source_documents": case["documents"],
                    }
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
                    study.run_generation(job)
                    final = artifacts.get_artifact(conn, job.artifact_id)
                    parts = artifacts.list_parts(conn, job.artifact_id)
                    content = [
                        {
                            "content": json.loads(part["content"]),
                            "provenance": artifacts.list_provenance(conn, int(part["id"])),
                        }
                        for part in parts
                        if part["content"]
                    ]
                    result = {
                        "case": case["id"],
                        "split": case["split"],
                        "kind": kind,
                        "repeat": repeat + 1,
                        "state": final["state"],
                        "latency_seconds": round(time.monotonic() - start, 3),
                        "terminal_content": content,
                        "calls": list(calls),
                        "critical_semantic_review": "pending",
                        "deterministic": structural_checks(content, sources, kind),
                        "source_documents": case["documents"],
                        "expected_concepts": case["expected_concepts"],
                    }
                    if final["state"] == artifacts.READY:
                        result["review_contracts"] = exercise_review(
                            conn, job.artifact_id, parts, kind
                        )
                    report["results"].append(result)
                    report.pop("in_progress", None)
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                key: result[key]
                                for key in ("case", "kind", "repeat", "state", "latency_seconds")
                            }
                        ),
                        flush=True,
                    )
    finally:
        if "in_progress" in report:
            report["in_progress"]["state"] = "interrupted_before_terminal_artifact"
            report["in_progress"]["semantic_review"] = "not assessable; no terminal answer"
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        conn.close()
        from backend.llm.embed_server import embedding_server
        from backend.llm.rerank_server import rerank_server

        embedding_server.stop()
        rerank_server.stop()


if __name__ == "__main__":
    main()
