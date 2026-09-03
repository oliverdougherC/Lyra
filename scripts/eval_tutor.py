"""Semantic evaluation of tutor reply quality, against a versioned corpus.

The backend test suite proves the contract the prompt asks for: the mode says explain
directly, it says do not withhold, it says a question is a tool. Only a run against a real
model can tell you whether the model that answers with that prompt actually behaves that
way - whether "Explain convolution" gets the mental model or an interrogation, whether a
directly requested answer is delivered or deferred behind a hint. That gap is the whole
reason this exists, so the grading is semantic, not textual: no case asserts a substring
of the reply, and a paraphrase, a different valid ordering, or LaTeX where plain text was
expected all count the same.

Every reply is graded on the seven semantic qualities the corpus names (directness,
proportionality, prerequisite setup, questioning, withholding, correctness, pedagogical
usefulness), plus the case's own `must` / `must_not` behaviors. The grader is a model
call with a constrained reply; the pass/fail verdict, though, is computed here from the
grader's per-item judgments, so a case's outcome is checkable arithmetic rather than a
second model's opinion.

The corpus (`scripts/eval_corpora/tutor_semantic.json`) pins the version of the
model-facing contract it was written against. When the contract version moves, the corpus
needs a re-read: a case written against the Socratic contract grades the teaching contract
with the wrong ruler.

**Run it against more than one model.** The point of the prompt work is that it holds up
on a small local model, and a number from one model tells you nothing about that.

Usage:

    python scripts/eval_tutor.py run    [--corpus P] [--workspace W] [--source-db D] [--case ID]
    python scripts/eval_tutor.py grade  [--corpus P] [--workspace W] [--source-db D]
                                       [--judge-source-db D] [--judge-model M] [--case ID]
    python scripts/eval_tutor.py report [--corpus P] [--workspace W] [--fail-under 1.0]
    (--case is repeatable.)
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.app_settings import TutorConfig, resolve_tutor_config  # noqa: E402
from backend.llm import client  # noqa: E402
from backend.llm.client import JsonSchema  # noqa: E402
from backend.llm.locality import is_local_endpoint  # noqa: E402
from backend.llm.prompts import (  # noqa: E402
    TUTOR_PROMPT_CONTRACT_VERSION,
    build_system_prompt,
    format_context_block,
)
from backend.rag.tokens import estimate_tokens  # noqa: E402

DEFAULT_CORPUS = ROOT / "scripts" / "eval_corpora" / "tutor_semantic.json"
DEFAULT_WORKSPACE = ROOT / "data" / "eval-tutor"
DEFAULT_SOURCE_DB = ROOT / "data" / "lyra.db"

# The fixed semantic qualities every case is graded on (PLA-401). The scale and the
# direction of each one are defined in the grader prompt below; this tuple is the contract
# the corpus, the schema, and the report all read from.
DIMENSIONS: tuple[str, ...] = (
    "directness",
    "proportionality",
    "prerequisite_setup",
    "questioning",
    "withholding",
    "correctness",
    "pedagogical_usefulness",
)


@dataclass(frozen=True)
class Case:
    """One student request and the behaviors its reply must and must not show."""

    id: str
    mode: str
    user: str
    history: tuple[dict[str, str], ...]
    context: tuple[dict[str, object], ...]
    must: tuple[str, ...]
    must_not: tuple[str, ...]
    may: tuple[str, ...]
    notes: str


def _load_raw(path: Path) -> dict[str, object]:
    if not path.exists():
        raise SystemExit(f"No corpus at {path}. Pass --corpus.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path.name} must be a JSON object.")
    return payload


def load_corpus(path: Path) -> tuple[dict[str, object], list[Case]]:
    """The corpus header plus its cases, validated hard enough that a malformed case
    cannot silently change what an evaluation measures.

    Returns the header (version, contract version, dimensions) alongside the cases: the
    report needs the versions, and a case is only as meaningful as the contract it was
    written against.
    """
    payload = _load_raw(path)
    cases_raw = payload.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise SystemExit(f"{path.name} has no cases.")

    header = {
        "corpus_version": str(payload.get("corpus_version") or ""),
        "prompt_contract_version": str(payload.get("prompt_contract_version") or ""),
        "dimensions": tuple(payload.get("dimensions") or ()),
    }
    if header["dimensions"] != DIMENSIONS:
        raise SystemExit(
            "The corpus's dimensions do not match the harness. Regenerate the corpus or "
            "update DIMENSIONS in scripts/eval_tutor.py."
        )

    cases: list[Case] = []
    seen: set[str] = set()
    for raw in cases_raw:
        if not isinstance(raw, dict):
            raise SystemExit(f"{path.name} has a non-object case.")
        case_id = str(raw.get("id") or "")
        if not case_id or case_id in seen:
            raise SystemExit(f"{path.name}: case id is missing or duplicated: {case_id!r}")
        seen.add(case_id)
        mode = str(raw.get("mode") or "")
        if mode not in ("guide", "show"):
            raise SystemExit(f"{case_id}: mode must be guide or show, got {mode!r}")
        history = tuple(
            {
                "role": str(turn.get("role") or ""),
                "content": str(turn.get("content") or ""),
            }
            for turn in (raw.get("history") or [])
            if isinstance(turn, dict)
        )
        for turn in history:
            if turn["role"] not in ("user", "assistant") or not turn["content"]:
                raise SystemExit(f"{case_id}: history turns need a role and content.")
        context = tuple(
            chunk
            for chunk in (raw.get("context") or [])
            if isinstance(chunk, dict) and str(chunk.get("content") or "")
        )
        for item in (raw.get("must"), raw.get("must_not")):
            if not isinstance(item, list) or not all(
                isinstance(entry, str) and entry for entry in item
            ):
                raise SystemExit(f"{case_id}: must/must_not need non-empty string items.")
        cases.append(
            Case(
                id=case_id,
                mode=mode,
                user=str(raw.get("user") or ""),
                history=history,
                context=context,
                must=tuple(str(entry) for entry in (raw.get("must") or ())),
                must_not=tuple(str(entry) for entry in (raw.get("must_not") or ())),
                may=tuple(str(entry) for entry in (raw.get("may") or ())),
                notes=str(raw.get("notes") or ""),
            )
        )
    if not all(case.user for case in cases):
        raise SystemExit(f"{path.name}: every case needs a user message.")
    return header, cases


def case_messages(case: Case) -> list[dict[str, str]]:
    """The exact messages the chat route would send for this case.

    Same building blocks as `routes_chat._build_turn`: the mode's system prompt, the
    rendered context block (when the case carries one), the prior turns, and the request.
    The contract test in `backend/tests/test_eval_tutor.py` assembles the same case
    through the route's own `_build_turn` and asserts the two agree, so the harness
    cannot drift from what the product sends.
    """
    parts = [build_system_prompt(case.mode, [], [])]
    if case.context:
        parts.append(
            format_context_block(
                [
                    {
                        "content": str(chunk.get("content") or ""),
                        "filename": str(chunk.get("filename") or ""),
                        "page_number": chunk.get("page_number"),
                        "section_title": chunk.get("section_title"),
                        "section_path": chunk.get("section_path"),
                        "section_number": chunk.get("section_number"),
                        "problem_number": chunk.get("problem_number"),
                    }
                    for chunk in case.context
                ]
            )
        )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": "\n\n".join(part for part in parts if part)}
    ]
    messages += [{"role": turn["role"], "content": turn["content"]} for turn in case.history]
    messages.append({"role": "user", "content": case.user})
    return messages


def _transcript(case: Case) -> str:
    """The conversation as the grader reads it: prior turns, then the request."""
    lines: list[str] = []
    for turn in case.history:
        lines.append(f"{'Student' if turn['role'] == 'user' else 'Tutor'}:\n{turn['content']}\n")
    lines.append(f"Student:\n{case.user}")
    return "\n".join(lines)


_GRADER_PROMPT = """\
You are grading one reply from a study tutor against the case it was written for. You are
given the mode the reply ran in, the conversation so far, the student's request, the
tutor's reply, and the case's ground-truth notes.

Grade the reply's behavior, never its wording. Paraphrases, LaTeX where plain text was
expected, and different valid orderings all count the same. A different valid formalism,
notation, or layout for the same content counts the same too: the case's items decide
what the reply must teach, and they never require one particular presentation of it. A
reply that shows a required behavior counts even if it says useful extra things; a reply
that shows a forbidden behavior fails that item even if most of the reply is fine.

Score each of the seven dimensions on 0, 1, or 2, where 2 is clean:
- directness: 2 = the student's actual request is answered early and on its own terms;
  1 = answered, but behind setup, a detour, or a delay; 0 = a different question is
  answered first, or the request is not answered.
- proportionality: 2 = the detail matches the request and any stated time budget; 1 =
  noticeably more or less than the request earned; 0 = a lecture where a sentence was
  asked for, or a sentence where the work was asked for.
- prerequisite_setup: 2 = no prerequisite material the request did not ask for; 1 = a
  short, unnecessary setup before the substance; 0 = a prerequisite lecture that delays
  or replaces the substance.
- questioning: 2 = no question to the student, or a question that demonstrably changes
  what should be taught next; 1 = a question that delays the requested content; 0 = the
  reply is a question, or a chain of questions, in place of the requested content.
- withholding: 2 = every explanation or answer the student asked for outright is present;
  1 = a requested part is offered only as an exercise or after pressure; 0 = a directly
  requested answer or explanation is withheld, deferred behind a hint, or made into a
  test for the student.
- correctness: 2 = no mathematical or factual error; 1 = a minor error that does not
  change the conclusion; 0 = a substantive error. Judge against the ground-truth notes;
  do not solve the problem yourself beyond what the notes state.
- pedagogical_usefulness: 2 = the reply teaches - it is organized around the idea, it
  gives the reason behind the result when understanding was the ask, and it leaves the
  student able to continue without a hand; 1 = on point and correct, but a list of
  statements the student cannot act on or build on; 0 = inert or misleading - skipping
  it costs the student nothing, or it leaves a wrong mental model behind.

Then answer the case's items, numbered as given. For each `must` item report whether the
reply meets it; for each `must_not` item report whether the reply violates it. One
sentence of evidence per item, quoting the reply where it settles the question.

Reply with JSON and nothing else:
- "dimensions": one entry per dimension, each {"score": an integer 0-2, "note": one
  sentence}.
- "must": one entry per numbered item, each {"index": the item number, "met": true or
  false, "why": one sentence}.
- "must_not": one entry per numbered item, each {"index": the item number, "violated":
  true or false, "why": one sentence}."""


def _dimension_schema() -> dict[str, object]:
    return {
        "properties": {
            dimension: {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": 0, "maximum": 2},
                    "note": {"type": "string"},
                },
                "required": ["score", "note"],
                "additionalProperties": False,
            }
            for dimension in DIMENSIONS
        },
        "required": list(DIMENSIONS),
        "additionalProperties": False,
        "type": "object",
    }


def _item_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "minimum": 1},
            "met": {"type": "boolean"},
            "violated": {"type": "boolean"},
            "why": {"type": "string"},
        },
        "required": ["index", "why"],
        "additionalProperties": False,
    }


GRADING_SCHEMA = JsonSchema(
    name="tutor_case_grading",
    schema={
        "type": "object",
        "properties": {
            "dimensions": _dimension_schema(),
            "must": {"type": "array", "items": _item_schema()},
            "must_not": {"type": "array", "items": _item_schema()},
        },
        "required": ["dimensions", "must", "must_not"],
        "additionalProperties": False,
    },
)


def grading_messages(case: Case, reply: str) -> list[dict[str, object]]:
    """The grader's messages: the rubric, the case, the reply, the ground truth."""
    must_lines = "\n".join(f"{index}. {text}" for index, text in enumerate(case.must, 1))
    must_not_lines = "\n".join(f"{index}. {text}" for index, text in enumerate(case.must_not, 1))
    user_content = (
        f"The mode: {case.mode}\n\n"
        f"The conversation so far:\n{_transcript(case)}\n\n"
        f"The tutor's reply:\n{reply}\n\n"
        f"Ground-truth notes:\n{case.notes}\n\n"
        f"must items:\n{must_lines}\n\n"
        f"must_not items:\n{must_not_lines}"
    )
    return [
        {"role": "system", "content": _GRADER_PROMPT},
        {"role": "user", "content": user_content},
    ]


def verdict_for(case: Case, grading: dict[str, object]) -> str:
    """The case's outcome, computed from the grader's judgments rather than taken from
    the model.

    A case passes when every `must` item is met, no `must_not` item is violated, and
    correctness is not a hard failure (score >= 1). Missing or out-of-range judgments are
    treated as failures, because an unreadable grade is not a pass.
    """
    dimensions = grading.get("dimensions")
    if not isinstance(dimensions, dict):
        return "fail"
    correctness = dimensions.get("correctness")
    score = correctness.get("score") if isinstance(correctness, dict) else None
    if not isinstance(score, int) or not 0 <= score <= 2 or score < 1:
        return "fail"

    must = grading.get("must")
    must_not = grading.get("must_not")
    if not isinstance(must, list) or not isinstance(must_not, list):
        return "fail"

    def _judged(items: list[object], count: int, key: str, bad: bool) -> bool:
        """Every expected index appears, with the judgment the case needs."""
        by_index = {item.get("index"): item for item in items if isinstance(item, dict)}
        for expected in range(1, count + 1):
            item = by_index.get(expected)
            if not isinstance(item, dict) or item.get(key) is not bad:
                return False
        return True

    if not _judged(must, len(case.must), "met", True):
        return "fail"
    if not _judged(must_not, len(case.must_not), "violated", False):
        return "fail"
    return "pass"


def _parse_grading(content: str) -> dict[str, object] | None:
    """Read the grader's JSON, tolerating the fence a server that drops constrained
    decoding still adds."""
    stripped = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class Workspace:
    """Where one evaluation run keeps its reports."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def report(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def read(self, name: str) -> dict[str, object]:
        path = self.report(name)
        if not path.exists():
            raise SystemExit(f"{path.name} is missing. Run the earlier stage first.")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, payload: dict[str, object]) -> None:
        self.report(name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _endpoint_config(source_db: Path) -> TutorConfig:
    """The configured tutor endpoint, read from the app's database.

    A plain connection is enough: `resolve_tutor_config` only reads the settings row, and
    the source database is the migrated one the app itself uses.
    """
    if not source_db.exists():
        raise SystemExit(f"No database at {source_db}. Configure Lyra first, or pass --source-db.")
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        config = resolve_tutor_config(conn)
    finally:
        conn.close()
    return config


def _locality(endpoint: str) -> str:
    """The locality class a report may carry for an endpoint: local or remote.

    The URL itself never enters a report - the class is all a later reader can check -
    and `is_local_endpoint` is the same conservative rule the app's consent gate applies
    (anything not provably loopback is remote).
    """
    return "local" if is_local_endpoint(endpoint) else "remote"


async def _run_one(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    case: Case,
    messages: list[dict[str, str]],
) -> tuple[str, float]:
    """Stream one case's reply, answer channel only, and return it with its cost."""
    chunks: list[str] = []
    async for delta in client.stream_chat(endpoint, api_key, model, messages):
        if delta.channel == "answer":
            chunks.append(delta.content)
    return "".join(chunks), estimate_tokens(messages[0]["content"])


def _selected(cases: list[Case], wanted: list[str] | None) -> list[Case]:
    if not wanted:
        return cases
    known = {case.id for case in cases}
    unknown = [entry for entry in wanted if entry not in known]
    if unknown:
        raise SystemExit(f"No case(s) in the corpus: {', '.join(unknown)}")
    order = {entry: index for index, entry in enumerate(wanted)}
    return [case for case in cases if case.id in order]


def cmd_run(args: argparse.Namespace) -> int:
    """Send every selected case to the configured endpoint and record the replies."""
    corpus_path = Path(args.corpus).resolve()
    header, cases = load_corpus(corpus_path)
    config = _endpoint_config(Path(args.source_db).resolve())
    selected = _selected(cases, args.case)

    workspace = Workspace(Path(args.workspace).resolve())
    runs = workspace.read("runs") if workspace.report("runs").exists() else {}
    cases_run = runs.get("cases")
    cases_run = cases_run if isinstance(cases_run, dict) else {}

    try:
        for case in selected:
            messages = case_messages(case)
            try:
                reply, system_tokens = asyncio.run(
                    _run_one(config.endpoint_url, config.api_key, config.model, case, messages)
                )
            except Exception as exc:  # noqa: BLE001 - a failed case is a datum, not a crash
                print(f"{case.id}: run failed: {exc}")
                cases_run[case.id] = {"status": "error", "error": str(exc)[:300]}
                continue
            if not reply.strip():
                print(f"{case.id}: empty reply")
                cases_run[case.id] = {"status": "error", "error": "empty reply"}
                continue
            cases_run[case.id] = {
                "status": "ok",
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "response": reply,
                "system_prompt_tokens": system_tokens,
                "user": case.user,
                "mode": case.mode,
            }
            print(f"{case.id}: {len(reply)} chars, system prompt {system_tokens} tokens")
    finally:
        # One terminal write, guaranteed: ok, empty, and failed cases all land in
        # runs.json, so a run in which every case failed still leaves a record
        # `grade` can consume, and an interrupted run keeps the cases it finished.
        workspace.write("runs", {**runs, "cases": cases_run})

    workspace.write(
        "meta",
        {
            "corpus": str(corpus_path),
            "corpus_version": header["corpus_version"],
            "prompt_contract_version": header["prompt_contract_version"],
            "expected_contract_version": TUTOR_PROMPT_CONTRACT_VERSION,
            "model": config.model,
            "context_window": config.context_window,
            "endpoint_locality": _locality(config.endpoint_url),
        },
    )
    if header["prompt_contract_version"] != TUTOR_PROMPT_CONTRACT_VERSION:
        print(
            f"warning: corpus is written against contract "
            f"{header['prompt_contract_version']}, but the code carries "
            f"{TUTOR_PROMPT_CONTRACT_VERSION}. Re-grade with a matching corpus."
        )
    return 0


async def _grade_one(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    case: Case,
    reply: str,
) -> dict[str, object]:
    content = await client.complete(
        endpoint,
        api_key,
        model,
        grading_messages(case, reply),
        schema=GRADING_SCHEMA,
    )
    return _parse_grading(content) or {"unreadable": content[:300]}


def cmd_grade(args: argparse.Namespace) -> int:
    """Grade every recorded reply, and compute each verdict deterministically."""
    corpus_path = Path(args.corpus).resolve()
    header, cases = load_corpus(corpus_path)
    config = _endpoint_config(Path(args.source_db).resolve())
    workspace = Workspace(Path(args.workspace).resolve())
    if not workspace.report("runs").exists():
        raise SystemExit("No runs.json. Run `run` first.")
    runs = workspace.read("runs")
    cases_run = runs.get("cases")
    cases_run = cases_run if isinstance(cases_run, dict) else {}

    # The judge defaults to the tutor's own configuration - the model that answered is
    # the one that grades - and that default is recorded in grade_meta rather than
    # assumed. `--judge-source-db` points at a second configured endpoint;
    # `--judge-model` picks another model on the resolved endpoint.
    judge_endpoint, judge_api_key, judge_model = config.endpoint_url, config.api_key, config.model
    if args.judge_source_db:
        judge_config = _endpoint_config(Path(args.judge_source_db).resolve())
        judge_endpoint, judge_api_key, judge_model = (
            judge_config.endpoint_url,
            judge_config.api_key,
            judge_config.model,
        )
    if args.judge_model:
        judge_model = args.judge_model

    by_id = {case.id: case for case in cases}
    grades: dict[str, object] = {}
    if workspace.report("grades").exists():
        existing = workspace.read("grades")
        if isinstance(existing, dict):
            grades.update(existing)
    try:
        for case_id, record in cases_run.items():
            if case_id not in by_id:
                print(f"{case_id}: not in the corpus, skipped")
                continue
            case = by_id[case_id]
            if args.case and case_id not in args.case:
                continue
            if not isinstance(record, dict) or record.get("status") != "ok":
                grades[case_id] = {"verdict": "not_run", "grading": record}
                continue
            reply = str(record.get("response") or "")
            try:
                grading = asyncio.run(
                    _grade_one(judge_endpoint, judge_api_key, judge_model, case, reply)
                )
            except Exception as exc:  # noqa: BLE001
                grading = {"grader_failed": str(exc)[:300]}
            verdict = verdict_for(case, grading) if "unreadable" not in grading else "fail"
            grades[case_id] = {
                "verdict": verdict,
                "grading": grading,
                "generated_at": record.get("generated_at"),
            }
            print(f"{case_id}: {verdict}")
    finally:
        # One terminal write, always: merging with any earlier grades means re-grading
        # a case updates its entry instead of dropping the others.
        workspace.write("grades", grades)

    workspace.write(
        "grade_meta",
        {
            "corpus_version": header["corpus_version"],
            "prompt_contract_version": header["prompt_contract_version"],
            "tutor": {
                "model": config.model,
                "locality": _locality(config.endpoint_url),
            },
            "judge": {
                "model": judge_model,
                "locality": _locality(judge_endpoint),
                "same_as_tutor": judge_endpoint == config.endpoint_url
                and judge_model == config.model,
            },
            "graded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Print what the recorded grades say, and fail the run's exit code below the bar."""
    workspace = Workspace(Path(args.workspace).resolve())
    grades = workspace.read("grades")
    grade_meta = workspace.read("grade_meta") if workspace.report("grade_meta").exists() else {}
    header, cases = load_corpus(Path(args.corpus).resolve())

    rows: list[tuple[str, str, list[int]]] = []
    totals = {dimension: 0 for dimension in DIMENSIONS}
    counts = {dimension: 0 for dimension in DIMENSIONS}
    passed = 0
    graded = 0
    for case in cases:
        record = grades.get(case.id)
        if not isinstance(record, dict) or record.get("verdict") in (None, "not_run"):
            error = ""
            if isinstance(record, dict):
                grading = record.get("grading")
                if isinstance(grading, dict):
                    error = str(grading.get("error") or grading.get("grader_failed") or "")
            rows.append((case.id, "not_run" + (f" ({error[:40]})" if error else ""), []))
            continue
        grading = record.get("grading")
        grading = grading if isinstance(grading, dict) else {}
        dimensions = grading.get("dimensions")
        dimensions = dimensions if isinstance(dimensions, dict) else {}
        scores: list[int] = []
        for dimension in DIMENSIONS:
            entry = dimensions.get(dimension)
            score = entry.get("score") if isinstance(entry, dict) else None
            if isinstance(score, int) and 0 <= score <= 2:
                totals[dimension] += score
                counts[dimension] += 1
                scores.append(score)
            else:
                scores.append(0)
        graded += 1
        if record.get("verdict") == "pass":
            passed += 1
        rows.append((case.id, str(record.get("verdict") or "fail"), scores))

    width = max(len(case.id) for case in cases)
    judge = grade_meta.get("judge")
    judge = judge if isinstance(judge, dict) else {}
    judge_model = str(judge.get("model") or grade_meta.get("model") or "unknown")
    judge_label = judge_model
    if judge.get("locality"):
        judge_label += f" [{judge['locality']}"
        if judge.get("same_as_tutor"):
            judge_label += ", same as tutor"
        judge_label += "]"
    header_row = (
        f"tutor semantic eval (corpus {header['corpus_version']}, "
        f"contract {header['prompt_contract_version']}, "
        f"judge {judge_label})"
    )
    print(header_row)
    dimension_head = "  ".join(f"{dimension[:4]:>4}" for dimension in DIMENSIONS)
    print(f"{'case':<{width}}  {'result':<9}  " + dimension_head)
    for case_id, verdict, scores in rows:
        score_row = "  ".join(f"{score:>4}" for score in scores)
        print(f"{case_id:<{width}}  {verdict:<9}  " + score_row)

    if graded:
        averages = {
            dimension: (
                round(totals[dimension] / counts[dimension], 2) if counts[dimension] else None
            )
            for dimension in DIMENSIONS
        }
        average_row = "  ".join(f"{dimension}={averages[dimension]}" for dimension in DIMENSIONS)
        print("\naverage (0-2): " + average_row)
        rate = passed / graded
        print(f"pass rate: {passed}/{graded} ({rate:.3f})")
        return 0 if rate >= args.fail_under else 1
    print("\nnothing graded yet; run `grade` first.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(entry: argparse.ArgumentParser) -> None:
        entry.add_argument("--corpus", default=str(DEFAULT_CORPUS))
        entry.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
        entry.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
        entry.add_argument(
            "--case",
            action="append",
            default=None,
            help="Grade or run only this case id; repeatable.",
        )

    run = sub.add_parser("run")
    _common(run)
    run.set_defaults(func=cmd_run)

    grade = sub.add_parser("grade")
    _common(grade)
    grade.add_argument(
        "--judge-source-db",
        default=None,
        help="Grade with the tutor configuration of a second Lyra database.",
    )
    grade.add_argument(
        "--judge-model",
        default=None,
        help="Grade with this model (default: the tutor's own model, recorded as such).",
    )
    grade.set_defaults(func=cmd_grade)

    entry = sub.add_parser("report")
    entry.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    entry.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    entry.add_argument(
        "--fail-under",
        type=float,
        default=1.0,
        help="Exit nonzero when the pass rate drops below this (default: all cases).",
    )
    entry.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
