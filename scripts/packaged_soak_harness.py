"""Prepare and update a versioned packaged desktop soak-harness run directory.

The harness owns only the disposable profile, artifact/log folders, and the plan document.
Physical execution stays separate: the plan records which steps require a real packaged app
launch on a real Mac, and the `record` subcommand captures their outcome without pretending
the harness executed them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_WORK_ROOT = Path(".desktop-soaks")


def display_name(path: Path) -> str:
    expanded = path.expanduser()
    return expanded.name or str(expanded)


def run_root(work_root: Path, run_id: str) -> Path:
    return work_root / run_id


def base_steps(app_name: str) -> list[dict[str, object]]:
    return [
        {
            "id": "prepare-disposable-profile",
            "executor": "harness",
            "status": "completed",
            "notes": "Created the disposable profile, log, and artifact directories.",
        },
        {
            "id": "capture-packaged-resource-inventory",
            "executor": "harness",
            "status": "pending",
            "notes": (
                "Run scripts/desktop_resource_report.py against the packaged app and write the "
                "JSON report into artifacts/."
            ),
        },
        {
            "id": "launch-packaged-app",
            "executor": "physical",
            "status": "pending",
            "notes": f"Launch {app_name} with the disposable profile environment below.",
        },
        {
            "id": "verify-first-launch",
            "executor": "physical",
            "status": "pending",
            "notes": "Confirm the packaged app reaches its first usable state on a clean profile.",
        },
        {
            "id": "verify-authenticated-bootstrap-and-duplicate-launch",
            "executor": "physical",
            "status": "pending",
            "notes": "Verify readiness authentication and that a second launch focuses the first.",
        },
        {
            "id": "exercise-multiple-classes-and-documents",
            "executor": "physical",
            "status": "pending",
            "notes": "Create multiple classes; ingest realistic PDF, text, and Markdown sources.",
        },
        {
            "id": "exercise-retrieval-and-remote-tutor",
            "executor": "physical",
            "status": "pending",
            "notes": (
                "Run grounded retrieval/chat, cancellation, retry, disconnect, and rate limits."
            ),
        },
        {
            "id": "exercise-exa-research-and-failures",
            "executor": "physical",
            "status": "pending",
            "notes": (
                "Cover missing key, 401/429/5xx/timeout, recovery, content status, provenance."
            ),
        },
        {
            "id": "exercise-agent-solutions-study-writing",
            "executor": "physical",
            "status": "pending",
            "notes": "Run agent tools, solutions, decks/quizzes, and long edit/review workflows.",
        },
        {
            "id": "verify-helper-lazy-start-and-eviction",
            "executor": "physical",
            "status": "pending",
            "notes": (
                "Measure embedding/rerank/OCR leases, crashes, fallback, eviction, and recovery."
            ),
        },
        {
            "id": "verify-restart-recovery",
            "executor": "physical",
            "status": "pending",
            "notes": "Restart the packaged app and confirm durable work reconciles correctly.",
        },
        {
            "id": "verify-provider-outage-behavior",
            "executor": "physical",
            "status": "pending",
            "notes": "Exercise degraded behavior when optional remote services are unavailable.",
        },
        {
            "id": "verify-migration-interruption-and-retry",
            "executor": "physical",
            "status": "pending",
            "notes": (
                "Interrupt staged source-data migration; verify original, retry, and integrity."
            ),
        },
        {
            "id": "verify-backup-and-restore",
            "executor": "physical",
            "status": "pending",
            "notes": "Export, corrupt disposable data, restore, and reopen representative work.",
        },
        {
            "id": "verify-background-sleep-wake-and-force-quit",
            "executor": "physical",
            "status": "pending",
            "notes": "Cover background/foreground, sleep/wake, force quit, and relaunch recovery.",
        },
        {
            "id": "measure-repeated-session-drift",
            "executor": "physical",
            "status": "pending",
            "notes": (
                "Capture startup, RSS, CPU, process tree, files, cache/disk growth, and quit drift."
            ),
        },
        {
            "id": "verify-final-integrity-and-privacy",
            "executor": "harness",
            "status": "pending",
            "notes": (
                "Record SQLite/incomplete-publication/process/privacy assertions and failures."
            ),
        },
        {
            "id": "capture-manual-evidence",
            "executor": "physical",
            "status": "pending",
            "notes": "Attach screenshots, logs, and short notes for any physical observations.",
        },
        {
            "id": "summarize-run",
            "executor": "harness",
            "status": "pending",
            "notes": "Update the plan with final pass/fail notes and collected artifact paths.",
        },
    ]


def build_plan(app_root: Path, work_root: Path, run_id: str) -> dict[str, object]:
    root = run_root(work_root, run_id).expanduser().resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "packaged_soak_harness",
        "scenario": "pla-147-packaged-desktop",
        "run_id": run_id,
        "run_root": root.name,
        "app": {
            "name": display_name(app_root),
            "exists": app_root.expanduser().exists(),
        },
        "paths": {
            "profile": "profile",
            "artifacts": "artifacts",
            "logs": "logs",
            "cache": "cache",
        },
        # This local execution plan contains private absolute paths. Redact them before
        # publishing evidence. Relative paths depend on the app's launch directory.
        "launch_environment": {
            "LYRA_DATA_DIR": str(root / "profile"),
            "LYRA_DB_PATH": str(root / "profile" / "lyra.db"),
            "LYRA_CACHE_DIR": str(root / "cache"),
            "LYRA_LOGS_DIR": str(root / "logs"),
            "LYRA_MODELS_DIR": str(root / "profile" / "models"),
        },
        "steps": base_steps(display_name(app_root)),
    }


def write_plan(plan_path: Path, payload: dict[str, object]) -> None:
    plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_run(app_root: Path, work_root: Path, run_id: str) -> Path:
    root = run_root(work_root, run_id).expanduser().resolve()
    if root.exists():
        raise FileExistsError(f"run directory already exists: {root}")
    (root / "profile").mkdir(parents=True)
    (root / "artifacts").mkdir()
    (root / "logs").mkdir()
    (root / "cache").mkdir()
    plan = build_plan(app_root, work_root, run_id)
    plan_path = root / "plan.json"
    write_plan(plan_path, plan)
    return plan_path


def load_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported plan schema {payload.get('schema_version')}; expected {SCHEMA_VERSION}"
        )
    return payload


def update_step(
    plan_path: Path,
    step_id: str,
    status: str,
    note: str | None,
    artifact: str | None,
) -> None:
    payload = load_plan(plan_path)
    steps = payload.get("steps")
    if not isinstance(steps, list):
        raise ValueError("plan steps are missing or invalid")
    for step in steps:
        if isinstance(step, dict) and step.get("id") == step_id:
            step["status"] = status
            if note is not None:
                step["notes"] = note
            if artifact is not None:
                artifacts = step.setdefault("artifacts", [])
                if not isinstance(artifacts, list):
                    raise ValueError("step artifacts must be a list")
                artifacts.append(artifact)
            write_plan(plan_path, payload)
            return
    raise KeyError(f"unknown step id: {step_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="create a disposable packaged-soak run")
    prepare.add_argument("--app-root", type=Path, required=True, help="packaged app bundle or root")
    prepare.add_argument("--run-id", required=True, help="stable identifier for this soak run")
    prepare.add_argument(
        "--work-root",
        type=Path,
        default=DEFAULT_WORK_ROOT,
        help="parent directory for prepared soak runs",
    )

    record = subparsers.add_parser("record", help="record the result of one soak-plan step")
    record.add_argument("--plan", type=Path, required=True, help="plan.json written by prepare")
    record.add_argument("--step", required=True, help="step id to update")
    record.add_argument(
        "--status",
        required=True,
        choices=("pending", "completed", "blocked", "not-run"),
        help="new step status",
    )
    record.add_argument("--note", help="replace the step note with a concise observation")
    record.add_argument("--artifact", help="artifact path relative to the run root")

    args = parser.parse_args(argv)
    if args.command == "prepare":
        plan_path = prepare_run(args.app_root, args.work_root, args.run_id)
        print(plan_path)
        return 0

    update_step(args.plan, args.step, args.status, args.note, args.artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
