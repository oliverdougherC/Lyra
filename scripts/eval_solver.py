"""End-to-end evaluation of the solver against real problem sets and their answer keys.

The backend test suite defends contracts against a faked model. It cannot tell you whether
segmentation finds the problems a real sheet contains, whether a solve arrives at the
answer the professor published, or whether either is stable across runs. Those are the
questions this script answers, and they are the ones that decide whether the solver is
worth shipping.

It drives the real code path, in process: `core.ingestion`, `core.solver`, and the
configured tutor endpoint. Nothing here reimplements what the product does, so a result is
evidence about the product rather than about the harness.

**It never touches the student's own data.** Every run works in its own workspace
directory with its own database, set before anything opens a connection. The settings row
is copied from the real database so the endpoint, model, and context window match what the
app would use, and the API key still comes from the keychain the way the app reads it.

Usage:

    python scripts/eval_solver.py ingest --corpus '/path/to/course_files_export'
    python scripts/eval_solver.py segment --repeat 2
    python scripts/eval_solver.py solve --sets homework_1 homework_5
    python scripts/eval_solver.py grade
    python scripts/eval_solver.py report

Each stage writes JSON into the workspace, so a later stage reads what an earlier one
produced rather than redoing it, and a run interrupted after forty minutes of solving is
not a run thrown away.
"""

import argparse
import asyncio
import json
import logging
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.core import artifacts, ingestion, solver  # noqa: E402
from backend.core.app_settings import resolve_tutor_config  # noqa: E402
from backend.llm import client  # noqa: E402
from backend.storage.database import connect, migrate  # noqa: E402

logger = logging.getLogger("lyra.eval")

DEFAULT_WORKSPACE = ROOT / "data" / "eval"
DEFAULT_SOURCE_DB = ROOT / "data" / "lyra.db"

# Corpus layout, by directory name. Problem sets are what gets solved, keys are what a
# solve is graded against, and the rest is the course material retrieval runs over.
PROBLEM_SETS = "Homework"
ANSWER_KEYS = "Homework Solutions"
COURSE_MATERIAL = ("Lecturre Notes", "Lab", "Practice Midterm", "Practice Final")

# A homework file and its key share a set number and nothing else, so that is what pairs
# them. Anchored on the word so a course number in the filename cannot be read as one.
_SET_NUMBER = re.compile(r"homework[_ -]?(\d+)", re.IGNORECASE)

_GRADER_PROMPT = """\
You are marking one homework answer against the answer key the professor published.

You are given the problem, the final answer a solver produced, and the relevant part of
the key. Decide whether the solver's answer says the same thing as the key.

Mathematically equivalent forms agree: $1/(2+j\\omega)$ and $\\frac{1}{2+j\\omega}$ are the
same answer, and so are a factored and an expanded polynomial. Presentation, ordering, and
wording do not matter. A missing sub-part is a disagreement, not a pass.

Reply with JSON and nothing else:
- "verdict": "agrees", "disagrees", or "key_not_found" when the key does not appear to
  cover this problem at all.
- "detail": one sentence saying what differs, or what matched.

Do not solve the problem yourself. The key is the authority."""


@dataclass(frozen=True)
class Workspace:
    """Where one evaluation run keeps its database, its uploads, and its reports."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "lyra.db"

    def report(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def read(self, name: str) -> dict[str, object]:
        path = self.report(name)
        if not path.exists():
            raise SystemExit(f"{path.name} is missing. Run the earlier stage first.")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, payload: dict[str, object]) -> None:
        self.report(name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def prepare(workspace: Workspace, source_db: Path) -> sqlite3.Connection:
    """Point the whole backend at the workspace, then open and migrate its database.

    The settings object is mutated rather than re-read from the environment because every
    module already holds a reference to it. Done before the first `connect`, so nothing
    can have opened the real database by the time this returns.
    """
    settings.data_dir = workspace.root
    settings.db_path = workspace.db_path
    workspace.root.mkdir(parents=True, exist_ok=True)
    _link_models(workspace)
    settings.ensure_directories()

    conn = connect()
    migrate(conn)
    _copy_settings(conn, source_db)
    return conn


def _link_models(workspace: Workspace) -> None:
    """Share the real installation's model directory rather than downloading a second one.

    Everything else about the workspace is its own, but the embedding binary and its
    weights are hundreds of megabytes and are identical by construction. Without this the
    workspace has no embedding server, every document fails at the embedding stage, and
    the harness reports a fault it created itself.
    """
    link = workspace.root / "models"
    real = ROOT / "data" / "models"
    if link.exists() or not real.is_dir():
        return
    link.symlink_to(real, target_is_directory=True)


def _copy_settings(conn: sqlite3.Connection, source_db: Path) -> None:
    """Copy the endpoint configuration from the real database into the workspace.

    Profile extraction is turned off on the copy. It is a model call per document that
    tells the solver evaluation nothing, and leaving it on makes ingesting a course take
    longer than solving one.
    """
    if not source_db.exists():
        raise SystemExit(f"No database at {source_db}. Configure Lyra first, or pass --source-db.")

    source = sqlite3.connect(source_db)
    source.row_factory = sqlite3.Row
    row = source.execute("select * from settings where id = 1").fetchone()
    source.close()
    if row is None:
        raise SystemExit("The source database has no settings row.")

    conn.execute(
        "update settings set endpoint_url = ?, model = ?, context_window = ?, "
        "remote_ack = ?, extraction_enabled = 0, embedding_model = ?, embedding_dim = ? "
        "where id = 1",
        (
            row["endpoint_url"],
            row["model"],
            row["context_window"],
            row["remote_ack"],
            row["embedding_model"],
            row["embedding_dim"],
        ),
    )
    conn.commit()


def _mapping(value: object) -> dict[str, dict[str, object]]:
    """A JSON object read back from a report, or an empty one when it is not one."""
    return value if isinstance(value, dict) else {}


def _class_id(conn: sqlite3.Connection, name: str) -> int:
    """The evaluation class, created once and reused."""
    row = conn.execute("select id from classes where name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute("insert into classes (name) values (?)", (name,))
    conn.commit()
    return int(cursor.lastrowid or 0)


def _number_in(name: str) -> str:
    """Which set a file belongs to, from its name.

    The number that follows the word `homework`, because a course number is a run of
    digits too: `ECE203_homework1_solution` is set 1, not set 203.
    """
    match = _SET_NUMBER.search(name)
    return match.group(1) if match else ""


def _stem(path: Path) -> str:
    return path.stem


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest a course corpus into the workspace, exactly as an upload would."""
    workspace = Workspace(Path(args.workspace).resolve())
    if args.fresh and workspace.root.exists():
        shutil.rmtree(workspace.root)
    conn = prepare(workspace, Path(args.source_db))
    corpus = Path(args.corpus).resolve()
    class_id = _class_id(conn, args.class_name)

    documents: dict[str, dict[str, object]] = {}
    for role, directory in _corpus_directories(corpus, args):
        for path in sorted(directory.glob("*.pdf")):
            document_id = _upload(conn, class_id, path)
            started = time.monotonic()
            ingestion.run_ingestion(document_id)
            state = conn.execute(
                "select state, error_message from documents where id = ?", (document_id,)
            ).fetchone()
            documents[_stem(path)] = {
                "document_id": document_id,
                "role": role,
                "filename": path.name,
                "number": _number_in(path.stem),
                "state": state["state"],
                "error": state["error_message"],
                "seconds": round(time.monotonic() - started, 1),
            }
            print(f"{path.name}: {state['state']} in {documents[_stem(path)]['seconds']}s")

    workspace.write("manifest", {"class_id": class_id, "documents": documents})
    failed = [name for name, entry in documents.items() if entry["state"] != ingestion.READY]
    print(f"\nIngested {len(documents)} documents, {len(failed)} not ready.")
    for name in failed:
        print(f"  not ready: {name} ({documents[name]['state']})")
    conn.close()
    return 0


def _corpus_directories(corpus: Path, args: argparse.Namespace) -> list[tuple[str, Path]]:
    """The corpus subdirectories to ingest, paired with the role they play."""
    directories = [("problem_set", corpus / PROBLEM_SETS), ("answer_key", corpus / ANSWER_KEYS)]
    if not args.sets_only:
        directories += [("material", corpus / name) for name in COURSE_MATERIAL]
    missing = [str(path) for _, path in directories if not path.is_dir()]
    if missing:
        raise SystemExit("Missing corpus directories:\n  " + "\n  ".join(missing))
    return directories


def _upload(conn: sqlite3.Connection, class_id: int, path: Path) -> int:
    """Store one file the way the upload route does, and return its document id."""
    payload = path.read_bytes()
    document_id = int(
        conn.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, ?, '', 'application/pdf', ?, ?)",
            (class_id, path.name, len(payload), ingestion.PENDING),
        ).lastrowid
        or 0
    )
    stored = settings.uploads_dir / str(class_id) / f"{document_id}-{path.name}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    conn.execute("update documents set stored_path = ? where id = ?", (str(stored), document_id))
    conn.commit()
    return document_id


def _problem_sets(manifest: dict[str, object], selected: list[str] | None) -> list[str]:
    """The problem-set document names to work on, in order."""
    documents = _mapping(manifest["documents"])
    names = [
        name
        for name, entry in documents.items()
        if entry["role"] == "problem_set" and entry["state"] == ingestion.READY
    ]
    if selected:
        names = [name for name in names if name in selected]
        unknown = sorted(set(selected) - set(names))
        if unknown:
            raise SystemExit(f"Not an ingested problem set: {', '.join(unknown)}")
    return sorted(names)


def cmd_segment(args: argparse.Namespace) -> int:
    """Segment every problem set, optionally several times, and record what came back."""
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    manifest = workspace.read("manifest")
    documents = _mapping(manifest["documents"])

    # Merged into what is already recorded, so segmenting one set again to check a fix
    # does not discard the other seven.
    runs: dict[str, object] = _existing(workspace, "segmentation").get("runs", {})
    for name in _problem_sets(manifest, args.sets):
        entry = documents[name]
        attempts: list[dict[str, object]] = []
        runs[name] = attempts
        for attempt in range(args.repeat):
            artifact = artifacts.create_artifact(
                conn,
                int(manifest["class_id"]),
                title=f"{name} (run {attempt + 1})",
                sources=[artifacts.SourceSpec(int(entry["document_id"]), artifacts.PROBLEM_SET)],
            )
            artifact_id = int(artifact["id"])
            started = time.monotonic()
            solver.run_segmentation(artifact_id)
            elapsed = round(time.monotonic() - started, 1)
            attempts.append(_segmentation_record(conn, artifact_id, elapsed))
            record = attempts[-1]
            print(
                f"{name} run {attempt + 1}: {record['state']}, "
                f"{record['problem_count']} problems, {elapsed}s"
            )

        workspace.write("segmentation", {"runs": runs})
    conn.close()
    return 0


def _segmentation_record(
    conn: sqlite3.Connection, artifact_id: int, elapsed: float
) -> dict[str, object]:
    """Everything worth judging about one segmentation run."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    parts = artifacts.list_parts(conn, artifact_id)
    roots = [part for part in parts if part["parent_part_id"] is None]
    problems = [
        {
            "part_id": part["id"],
            "label": part["label"],
            "statement": part["content"],
            "page": _page_of(conn, int(part["id"])),
            "parts": [
                {"label": child["label"], "statement": child["content"]}
                for child in parts
                if child["parent_part_id"] == part["id"]
            ],
        }
        for part in roots
    ]
    return {
        "artifact_id": artifact_id,
        "state": artifact["state"],
        "error": artifact["error_message"],
        "seconds": elapsed,
        "problem_count": len(problems),
        "problems": problems,
    }


def _page_of(conn: sqlite3.Connection, part_id: int) -> int | None:
    entries = artifacts.list_provenance(conn, part_id)
    return next((entry["page_number"] for entry in entries if entry["page_number"]), None)


def cmd_solve(args: argparse.Namespace) -> int:
    """Solve the segmented sets, one problem at a time, and record every verdict."""
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    manifest = workspace.read("manifest")
    documents = _mapping(manifest["documents"])
    runs = _mapping(workspace.read("segmentation")["runs"])

    solved: dict[str, dict[str, object]] = _existing(workspace, "solutions")
    for name in _problem_sets(manifest, args.sets):
        if name in solved and not args.again:
            print(f"{name}: already solved, skipping")
            continue
        artifact_id = int(runs[name][0]["artifact_id"])
        if args.reference:
            _attach_reference(conn, artifact_id, documents, str(documents[name]["number"]))
        started = time.monotonic()
        solver.run_solve(artifact_id)
        record = _solution_record(conn, artifact_id, round(time.monotonic() - started, 1))
        solved[name] = record
        # Written after every set, so an interrupted run keeps the sets that finished.
        workspace.write("solutions", solved)
        print(
            f"{name}: {record['state']} in {record['minutes']} min, "
            f"{record['solved_count']}/{record['problem_count']} solved, "
            f"verdicts {record['verdicts']}"
        )

    conn.close()
    return 0


def _existing(workspace: Workspace, name: str) -> dict[str, dict[str, object]]:
    path = workspace.report(name)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _attach_reference(
    conn: sqlite3.Connection,
    artifact_id: int,
    documents: dict[str, dict[str, object]],
    number: str,
) -> None:
    """Attach this set's own answer key as a reference solution, if there is one."""
    key = next(
        (
            entry
            for entry in documents.values()
            if entry["role"] == "answer_key" and entry["number"] == number
        ),
        None,
    )
    if key is None:
        return
    conn.execute(
        "insert or ignore into artifact_sources (artifact_id, document_id, role, ordinal) "
        "values (?, ?, ?, 1)",
        (artifact_id, key["document_id"], artifacts.REFERENCE_SOLUTIONS),
    )
    conn.commit()


def _solution_record(
    conn: sqlite3.Connection, artifact_id: int, elapsed: float
) -> dict[str, object]:
    """Everything worth judging about one solved set."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    parts = artifacts.list_parts(conn, artifact_id)
    roots = [
        part
        for part in parts
        if part["parent_part_id"] is None and part["kind"] == artifacts.PROBLEM
    ]

    problems: list[dict[str, object]] = []
    verdicts: dict[str, int] = {}
    for problem in roots:
        children = [part for part in parts if part["parent_part_id"] == problem["id"]]
        steps = [child for child in children if child["kind"] == artifacts.STEP]
        answer = next((child for child in children if child["kind"] == artifacts.ANSWER), None)
        verdict = str(problem["verdict"])
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        problems.append(
            {
                "part_id": problem["id"],
                "label": problem["label"],
                "statement": problem["content"],
                "status": problem["status"],
                "error": problem["error_message"],
                "verdict": verdict,
                "verdict_detail": problem["verdict_detail"],
                "step_count": len(steps),
                "grounded_steps": sum(
                    1 for step in steps if artifacts.list_provenance(conn, int(step["id"]))
                ),
                "check_count": len(artifacts.list_checks(conn, int(problem["id"]))),
                "answer": answer["content"] if answer else "",
                "steps": [step["content"] for step in steps],
            }
        )

    return {
        "artifact_id": artifact_id,
        "state": artifact["state"],
        "error": artifact["error_message"],
        "minutes": round(elapsed / 60, 1),
        "problem_count": len(problems),
        "solved_count": sum(1 for one in problems if one["status"] == artifacts.PART_COMPLETE),
        "verdicts": verdicts,
        "problems": problems,
    }


def cmd_grade(args: argparse.Namespace) -> int:
    """Mark every solved problem against the professor's published answer key."""
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    manifest = workspace.read("manifest")
    documents = _mapping(manifest["documents"])
    solutions = workspace.read("solutions")
    config = resolve_tutor_config(conn)

    graded: dict[str, object] = _existing(workspace, "grades")
    for name, record in solutions.items():
        if name in graded and not args.again:
            continue
        key_text = _key_text(documents, str(documents[name]["number"]))
        if not key_text:
            print(f"{name}: no answer key, skipped")
            continue
        results = []
        for problem in record["problems"]:
            if problem["status"] != artifacts.PART_COMPLETE:
                results.append({"label": problem["label"], "verdict": "not_solved", "detail": ""})
                continue
            outcome = _grade_one(config, problem, key_text)
            results.append({"label": problem["label"], **outcome})
            print(f"{name} {problem['label']}: {outcome['verdict']}")
        graded[name] = results
        workspace.write("grades", graded)

    conn.close()
    return 0


def _key_text(documents: dict[str, dict[str, object]], number: str) -> str:
    """The extracted text of the answer key belonging to one problem set."""
    key = next(
        (
            entry
            for entry in documents.values()
            if entry["role"] == "answer_key" and entry["number"] == number
        ),
        None,
    )
    if key is None:
        return ""
    path = settings.text_dir / f"{key['document_id']}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _grade_one(config: object, problem: dict[str, object], key_text: str) -> dict[str, str]:
    """Ask the model whether one answer agrees with the key, and read its reply."""
    messages = [
        {"role": "system", "content": _GRADER_PROMPT},
        {
            "role": "user",
            "content": (
                f"The problem, {problem['label']}:\n{problem['statement']}\n\n"
                f"The solver's final answer:\n{problem['answer']}\n\n"
                f"The answer key:\n{key_text[:60000]}"
            ),
        },
    ]
    try:
        content = asyncio.run(
            client.complete(
                config.endpoint_url,  # type: ignore[attr-defined]
                config.api_key,  # type: ignore[attr-defined]
                config.model,  # type: ignore[attr-defined]
                messages,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a grading failure is a datum, not a crash
        return {"verdict": "grader_failed", "detail": str(exc)[:200]}

    stripped = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        payload = json.loads(stripped)
    except ValueError:
        return {"verdict": "grader_unreadable", "detail": content[:200]}
    return {
        "verdict": str(payload.get("verdict", "grader_unreadable")),
        "detail": str(payload.get("detail", ""))[:400],
    }


def _rate(part: int, whole: int) -> float | None:
    """A fraction rounded to three places, or None when there is nothing to divide.

    None rather than zero, so a stage that was never exercised reads as unmeasured rather
    than as a perfect zero or a division that crashes the report.
    """
    return round(part / whole, 3) if whole else None


def score_stages(
    segmentation_runs: dict[str, object],
    solutions: dict[str, object],
    grades: dict[str, object],
) -> dict[str, object]:
    """Reduce the recorded reports to one score per pipeline stage.

    Pure by construction: it reads the JSON the stages wrote and returns numbers, so the
    arithmetic a run is read by is testable without an endpoint or a corpus. Parsing,
    reasoning, deterministic verification, and final-answer accuracy are scored apart
    because a solver can be strong at one and weak at another, and a single blended number
    hides which stage is the one to fix.
    """
    return {
        "parsing": _parsing_score(segmentation_runs),
        "reasoning": _reasoning_score(solutions),
        "verification": _verification_score(solutions),
        "final_answer": _final_answer_score(grades),
    }


def _parsing_score(runs: dict[str, object]) -> dict[str, object]:
    """How segmentation did: how many problems it found, and how stably.

    Stability is only meaningful where a set was segmented more than once, so it is scored
    over exactly those sets; the problem counts come from each set's first run so repeats
    do not inflate the total.
    """
    attempts = [attempt for runlist in runs.values() for attempt in list(runlist)]
    failures = sum(1 for a in attempts if a.get("error") or a.get("state") == "failed")
    repeated = [list(runlist) for runlist in runs.values() if len(list(runlist)) >= 2]
    stable = sum(1 for runlist in repeated if len({a["problem_count"] for a in runlist}) == 1)
    first_counts = [list(runlist)[0]["problem_count"] for runlist in runs.values() if list(runlist)]
    problems = sum(int(count) for count in first_counts)
    return {
        "sets": len(runs),
        "runs": len(attempts),
        "failures": failures,
        "stable_sets": stable,
        "sets_measured_for_stability": len(repeated),
        "stability_rate": _rate(stable, len(repeated)),
        "problems_found": problems,
        "mean_problems_per_set": (round(problems / len(first_counts), 2) if first_counts else None),
    }


def _reasoning_score(solutions: dict[str, object]) -> dict[str, object]:
    """How solving did: how many problems reached a complete solution, and how grounded.

    Grounded steps are the ones that cite a source; the rate is the model-derived-vs-cited
    split the lifecycle contract cares about, aggregated over every step written.
    """
    records = list(solutions.values())
    problems = sum(int(record["problem_count"]) for record in records)
    solved = sum(int(record["solved_count"]) for record in records)
    steps = sum(int(p["step_count"]) for record in records for p in record["problems"])
    grounded = sum(int(p["grounded_steps"]) for record in records for p in record["problems"])
    return {
        "sets": len(records),
        "set_failures": sum(1 for record in records if record.get("state") == "failed"),
        "problems": problems,
        "solved": solved,
        "solve_rate": _rate(solved, problems),
        "steps": steps,
        "grounded_steps": grounded,
        "grounded_rate": _rate(grounded, steps),
    }


def _verification_score(solutions: dict[str, object]) -> dict[str, object]:
    """How deterministic checking did: how often a tool could run, and how often it agreed.

    A verdict counts as checked only when a tool actually ran (verified or refuted);
    uncheckable and unchecked did not settle by calculation and are kept out of the
    verified rate rather than counted as failures against it.
    """
    verdicts: dict[str, int] = {}
    checks = 0
    problems = 0
    for record in solutions.values():
        for problem in record["problems"]:
            problems += 1
            verdict = str(problem["verdict"])
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            checks += int(problem["check_count"])
    verified = verdicts.get(artifacts.VERIFIED, 0)
    refuted = verdicts.get(artifacts.REFUTED, 0)
    checked = verified + refuted
    return {
        "problems": problems,
        "verdicts": verdicts,
        "checks": checks,
        "checked": checked,
        "coverage_rate": _rate(checked, problems),
        "verified": verified,
        "refuted": refuted,
        "verified_rate": _rate(verified, checked),
    }


def _final_answer_score(grades: dict[str, object]) -> dict[str, object]:
    """How final answers did against the key: agreement over the problems the key covered.

    key_not_found, not_solved, and the grader's own failures are reported but kept out of
    the denominator: the agreement rate is over problems that were both solved and covered
    by the key, which is the only set the number can honestly speak for.
    """
    verdicts: dict[str, int] = {}
    for results in grades.values():
        for result in list(results):
            verdict = str(result["verdict"])
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
    agrees = verdicts.get("agrees", 0)
    disagrees = verdicts.get("disagrees", 0)
    marked = agrees + disagrees
    return {
        "verdicts": verdicts,
        "graded": sum(verdicts.values()),
        "marked": marked,
        "agrees": agrees,
        "agreement_rate": _rate(agrees, marked),
    }


def cmd_report(args: argparse.Namespace) -> int:
    """Print what the recorded runs say, which is the only output anyone reads twice."""
    workspace = Workspace(Path(args.workspace).resolve())
    segmentation = _existing(workspace, "segmentation").get("runs", {})
    solutions = _existing(workspace, "solutions")
    grades = _existing(workspace, "grades")
    scores = score_stages(segmentation, solutions, grades)

    if segmentation:
        parsing = scores["parsing"]
        print("Parsing (segmentation)")
        for name, runs in sorted(segmentation.items()):
            counts = [run["problem_count"] for run in runs]
            seconds = [run["seconds"] for run in runs]
            stable = "stable" if len(set(counts)) == 1 else "UNSTABLE"
            print(f"  {name}: {counts} problems, {seconds}s, {stable}")
        print(
            f"  score: {parsing['problems_found']} problems across {parsing['sets']} sets "
            f"({parsing['mean_problems_per_set']}/set), stability "
            f"{parsing['stable_sets']}/{parsing['sets_measured_for_stability']} "
            f"({parsing['stability_rate']}), {parsing['failures']} failed"
        )

    if solutions:
        reasoning = scores["reasoning"]
        print("\nReasoning (solving)")
        for name, record in sorted(solutions.items()):
            print(
                f"  {name}: {record['solved_count']}/{record['problem_count']} solved in "
                f"{record['minutes']} min"
            )
        print(
            f"  score: {reasoning['solved']}/{reasoning['problems']} solved "
            f"({reasoning['solve_rate']}), {reasoning['grounded_steps']}/{reasoning['steps']} "
            f"steps grounded ({reasoning['grounded_rate']}), "
            f"{reasoning['set_failures']} sets failed"
        )

        verification = scores["verification"]
        print("\nDeterministic verification")
        print(
            f"  score: {verification['checked']}/{verification['problems']} checked "
            f"({verification['coverage_rate']}), {verification['verified']}/"
            f"{verification['checked']} verified ({verification['verified_rate']}), "
            f"verdicts {verification['verdicts']}"
        )

    if grades:
        final = scores["final_answer"]
        print("\nFinal answer (against the key)")
        for name, results in sorted(grades.items()):
            counts: dict[str, int] = {}
            for result in results:
                counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
            print(f"  {name}: {counts}")
        print(
            f"  score: {final['agrees']}/{final['marked']} agreed "
            f"({final['agreement_rate']}), verdicts {final['verdicts']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Ingest a course corpus")
    ingest.add_argument("--corpus", required=True)
    ingest.add_argument("--class-name", default="Evaluation")
    ingest.add_argument("--fresh", action="store_true", help="Delete the workspace first")
    ingest.add_argument("--sets-only", action="store_true", help="Skip lectures and labs")
    ingest.set_defaults(func=cmd_ingest)

    segment = subparsers.add_parser("segment", help="Segment every ingested problem set")
    segment.add_argument("--sets", nargs="*")
    segment.add_argument("--repeat", type=int, default=1)
    segment.set_defaults(func=cmd_segment)

    solve = subparsers.add_parser("solve", help="Solve the segmented sets")
    solve.add_argument("--sets", nargs="*")
    solve.add_argument("--again", action="store_true", help="Re-solve sets already recorded")
    solve.add_argument(
        "--reference", action="store_true", help="Attach each set's own key as a reference"
    )
    solve.set_defaults(func=cmd_solve)

    grade = subparsers.add_parser("grade", help="Mark solved sets against their answer keys")
    grade.add_argument("--again", action="store_true")
    grade.set_defaults(func=cmd_grade)

    report = subparsers.add_parser("report", help="Summarise the recorded runs")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
