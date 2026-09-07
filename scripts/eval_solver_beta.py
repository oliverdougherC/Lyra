"""Synthetic production segmentation/solve/check evaluation; never reads a student profile.

Pass an explicitly authorized isolated configuration database. Output contains only the
synthetic corpus, model identity and response/tool evidence, never endpoint or credential.
Semantic acceptance is a separate review, not inferred from successful requests.
"""

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-db", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--cases", nargs="*")
    parser.add_argument("--skip-segmentation", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 3:
        parser.error("repeat must be 1..3")
    workspace = args.workspace.resolve()
    if workspace.exists():
        parser.error("workspace must be fresh")
    os.environ["LYRA_DATA_DIR"] = str(workspace)
    os.environ["LYRA_CACHE_DIR"] = str(workspace / "cache")
    os.environ["LYRA_LOGS_DIR"] = str(workspace / "logs")
    os.environ["LYRA_MODELS_DIR"] = str(workspace / "models")
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    from backend.config import settings
    from backend.core import artifacts, solver
    from backend.core.segmentation import SegmentedPart, SegmentedProblem
    from backend.llm import client, tools
    from backend.storage.database import connect, migrate

    settings.data_dir = workspace
    settings.db_path = workspace / "lyra.db"
    settings.ensure_directories()
    conn = connect()
    migrate(conn)
    source = sqlite3.connect(f"file:{args.config_db.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    config = source.execute("select * from settings where id=1").fetchone()
    source.close()
    conn.execute(
        "update settings set endpoint_url=?,model=?,context_window=?,remote_ack=?,"
        "tools_supported=?,extraction_enabled=0 where id=1",
        tuple(
            config[k]
            for k in ("endpoint_url", "model", "context_window", "remote_ack", "tools_supported")
        ),
    )
    conn.commit()
    corpus_path = ROOT / "scripts/eval_corpora/solver_beta.json"
    corpus = json.loads(corpus_path.read_text())
    cases = [c for c in corpus["cases"] if not args.cases or c["id"] in args.cases]
    report = {
        "corpus": corpus,
        "metadata": {
            "sha": subprocess.check_output(  # noqa: S603 - fixed read-only Git command
                [shutil.which("git") or "/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "app_version": json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())["version"],
            "code_hashes": {
                p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                for p in [
                    "backend/core/solver.py",
                    "backend/core/solving.py",
                    "backend/core/verification.py",
                    "backend/core/segmentation.py",
                    "backend/llm/tools.py",
                    "backend/llm/client.py",
                    "backend/llm/prompts.py",
                    "scripts/eval_solver_beta.py",
                ]
            },
            "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
            "model": config["model"],
            "endpoint_locality": "loopback"
            if "127.0.0.1" in config["endpoint_url"] or "localhost" in config["endpoint_url"]
            else "remote",
            "context_window": config["context_window"],
            "tools_capability": "configured true, actual calls retained"
            if config["tools_supported"]
            else "not established",
            "generation_temperature": client.DETERMINISTIC_TEMPERATURE,
            "verification_max_depth": tools.MAX_DEPTH,
            "verification_timeout_seconds": tools.TIMEOUT_SECONDS,
            "generation_max_tokens": "production default (omitted)",
            "concurrency": 1,
            "actual_human_review": "not_run",
            "model_self_judging": "production verification only; not independent acceptance",
            "scope": (
                "Synthetic extracted text, empty retrieval class; "
                "no ingestion/OCR/embedding acceptance"
            ),
        },
        "runs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(
            json.dumps(report, indent=2).replace(config["endpoint_url"], "[redacted endpoint]")
        )

    original_complete = client.complete
    original_loop = tools.run_tool_loop
    transcript = []

    async def observed_complete(*a, **kw):
        started = time.monotonic()
        try:
            answer = await original_complete(*a, **kw)
        except Exception as exc:
            transcript.append(
                {
                    "kind": "completion",
                    "error_type": type(exc).__name__,
                    "latency_seconds": round(time.monotonic() - started, 3),
                }
            )
            raise
        transcript.append(
            {
                "kind": "completion",
                "schema": getattr(kw.get("schema"), "name", None),
                "messages": a[3],
                "answer": answer,
                "latency_seconds": round(time.monotonic() - started, 3),
                "stop_reason": "not exposed by complete API",
            }
        )
        return answer

    async def observed_loop(*a, **kw):
        started = time.monotonic()
        result = await original_loop(*a, **kw)
        transcript.append(
            {
                "kind": "tool_loop",
                "messages": a[3],
                "answer": result.content,
                "stopped": result.stopped,
                "detail": result.detail,
                "calls": [dataclasses.asdict(c) for c in result.calls],
                "latency_seconds": round(time.monotonic() - started, 3),
            }
        )
        return result

    client.complete = observed_complete
    tools.run_tool_loop = observed_loop
    for repeat in range(args.repeat):
        class_id = conn.execute(
            "insert into classes(name) values ('Synthetic beta solver')"
        ).lastrowid
        sheet = "\n\n".join(
            f"{n}. {c['statement']}\n" + "\n".join(" ".join(p) for p in c.get("parts", []))
            for n, c in enumerate(cases, 1)
        )
        doc_id = conn.execute(
            "insert into documents(class_id,filename,stored_path,mime,byte_size,state) "
            "values (?, 'synthetic.txt', '/synthetic', 'text/plain', ?, 'ready')",
            (class_id, len(sheet)),
        ).lastrowid
        conn.commit()
        (settings.text_dir / f"{doc_id}.txt").write_text(sheet)
        if not args.skip_segmentation:
            aid = artifacts.create_artifact(
                conn, class_id, "Segmentation evaluation", [artifacts.SourceSpec(doc_id)]
            )["id"]
            transcript.clear()
            started = time.monotonic()
            solver.run_segmentation(aid)
            report["runs"].append(
                {
                    "kind": "segmentation",
                    "repeat": repeat + 1,
                    "latency_seconds": round(time.monotonic() - started, 3),
                    "artifact": artifacts.get_artifact(conn, aid),
                    "parts": artifacts.list_parts(conn, aid),
                    "transcript": list(transcript),
                    "semantic_review": "pending",
                }
            )
            save()
        for case in cases:
            aid = artifacts.create_artifact(
                conn, class_id, case["id"], [artifacts.SourceSpec(doc_id)]
            )["id"]
            solver.write_problems(
                conn,
                aid,
                [
                    SegmentedProblem(
                        case["id"],
                        "1",
                        case["statement"],
                        doc_id,
                        parts=tuple(SegmentedPart(*p) for p in case.get("parts", [])),
                    )
                ],
            )
            artifacts.set_artifact_state(conn, aid, artifacts.AWAITING_REVIEW)
            transcript.clear()
            started = time.monotonic()
            solver.run_solve(aid)
            parts = artifacts.list_parts(conn, aid)
            report["runs"].append(
                {
                    "kind": "solve",
                    "case_id": case["id"],
                    "repeat": repeat + 1,
                    "latency_seconds": round(time.monotonic() - started, 3),
                    "artifact": artifacts.get_artifact(conn, aid),
                    "parts": parts,
                    "checks": {
                        str(p["id"]): artifacts.list_checks(conn, p["id"])
                        for p in parts
                        if p["kind"] == artifacts.PROBLEM
                    },
                    "transcript": list(transcript),
                    "semantic_review": "pending",
                }
            )
            save()
            print(
                f"{case['id']} repeat {repeat + 1}: {report['runs'][-1]['artifact']['state']}",
                flush=True,
            )
    conn.close()
    save()


if __name__ == "__main__":
    main()
