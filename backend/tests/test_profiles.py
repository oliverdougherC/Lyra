"""Profile facts: the active-fact filter, extraction, confirmation, and the routes.

No test reaches a live model. `complete` is replaced at the reference
`backend.core.profiles` actually calls, and the default stub fails the test the moment it
is reached, so a skip rule that is supposed to send nothing cannot pass by accident.
"""

import json
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import keyring.errors
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_profile
from backend.core import profiles
from backend.core.app_settings import update_settings_row
from backend.core.errors import LyraError, NotFoundError
from backend.llm.client import JsonSchema
from backend.llm.prompts import build_system_prompt
from backend.storage import secrets
from backend.storage.database import MIGRATIONS_DIR, connect, get_db, migrate

LOCAL_ENDPOINT = "http://127.0.0.1:8080/v1"
REMOTE_ENDPOINT = "https://tutor.example.com/v1"

RESOLUTIONS = {
    "127.0.0.1": ["127.0.0.1"],
    "tutor.example.com": ["203.0.113.10"],
}

# The document every extraction test is read against. A quote is checked against this, so
# a reply claiming something it does not say is a reply that lands `low`.
SYLLABUS_TEXT = (
    "MATH 201 course syllabus.\n"
    "Midterm 1 will be held on 2026-03-04 in the main hall.\n"
    "Attendance at the weekly lab is required.\n"
)

MIDTERM_REPLY = json.dumps(
    {
        "deadlines": [
            {
                "label": "Midterm 1",
                "date": "2026-03-04",
                "quote": "Midterm 1 will be held on 2026-03-04 in the main hall.",
            }
        ]
    }
)


class FakeKeyring:
    """Stands in for the `keyring` module, so no test reaches the login keychain."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("no such password")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    return fake


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer only from the table above, so locality is decided offline."""

    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        try:
            addresses = RESOLUTIONS[host]
        except KeyError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", (address, 0)) for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def refuse_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that sends to the tutor endpoint without opting in through `_reply`."""

    async def _refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("Document text was sent to the tutor endpoint")

    monkeypatch.setattr(profiles.client, "complete", _refuse)


def _reply(monkeypatch: pytest.MonkeyPatch, content: str) -> list[list[dict[str, str]]]:
    """Answer the next completions with `content`, recording the messages sent."""
    sent: list[list[dict[str, str]]] = []

    async def _complete(
        endpoint: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> str:
        sent.append(messages)
        return content

    monkeypatch.setattr(profiles.client, "complete", _complete)
    return sent


@pytest.fixture
def local_extraction(db: sqlite3.Connection) -> None:
    """Settings that permit extraction: enabled, against a loopback endpoint."""
    update_settings_row(db, {"endpoint_url": LOCAL_ENDPOINT, "extraction_enabled": 1})


def _insert_document(
    db: sqlite3.Connection,
    class_id: int,
    filename: str = "syllabus.pdf",
    state: str = "ready",
    stage_detail: str | None = None,
) -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state, "
        "stage_detail) values (?, ?, ?, 'application/pdf', 2048, ?, ?)",
        (class_id, filename, f"{class_id}/{filename}", state, stage_detail),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _insert_fact(
    db: sqlite3.Connection,
    class_id: int | None,
    kind: str = "deadline",
    label: str = "Midterm 1",
    value: str = "2026-03-04",
    confidence: str = "low",
    confirmed: int = 0,
    source_document_id: int | None = None,
) -> int:
    cursor = db.execute(
        "insert into profile_facts (class_id, kind, label, value, confidence, confirmed, "
        "source_document_id) values (?, ?, ?, ?, ?, ?, ?)",
        (class_id, kind, label, value, confidence, confirmed, source_document_id),
    )
    fact_id = int(cursor.lastrowid or 0)
    # Extraction records the document as evidence as well as naming it on the row, and the
    # evidence is what the profile counts, orders, and prunes by.
    if source_document_id is not None:
        db.execute(
            "insert into profile_fact_sources (fact_id, document_id) values (?, ?)",
            (fact_id, source_document_id),
        )
    db.commit()
    return fact_id


@pytest.fixture
def document_id(db: sqlite3.Connection, class_id: int) -> int:
    """One document for the class, since every extracted fact is attributed to one."""
    return _insert_document(db, class_id)


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the profile router.

    The override pins the app to the `db` fixture's database rather than to its
    connection object: handlers are sync, so they run in a threadpool, and a `sqlite3`
    connection may only be used from the thread that opened it.
    """

    def request_db() -> Iterator[sqlite3.Connection]:
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()

    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_profile.router)
    app.dependency_overrides[get_db] = request_db
    with TestClient(app) as test_client:
        yield test_client


def _facts(db: sqlite3.Connection, class_id: int) -> list[dict[str, object]]:
    """Just the fact list out of a class profile, which is what most assertions want."""
    return cast(list[dict[str, object]], profiles.get_class_profile(db, class_id)["facts"])


def _identities(facts: list[dict[str, object]]) -> set[tuple[object, object, object]]:
    """The `(kind, label, value)` of each fact, which is what extraction is judged on."""
    return {(fact["kind"], fact["label"], fact["value"]) for fact in facts}


def _fact_count(db: sqlite3.Connection) -> int:
    return int(db.execute("select count(*) from profile_facts").fetchone()[0])


def test_low_confidence_fact_enters_the_prompt_only_once_confirmed(
    db: sqlite3.Connection, class_id: int
) -> None:
    fact_id = _insert_fact(db, class_id, confidence="low", confirmed=0)

    before = build_system_prompt("guide", [], profiles.select_active_facts(db, class_id))
    assert "Midterm 1" not in before
    assert "2026-03-04" not in before

    profiles.confirm_fact(db, fact_id)

    after = build_system_prompt("guide", [], profiles.select_active_facts(db, class_id))
    assert "Midterm 1: 2026-03-04" in after


def test_high_confidence_fact_is_active_without_confirmation(
    db: sqlite3.Connection, class_id: int
) -> None:
    _insert_fact(
        db, class_id, kind="topic", label="Series", value="Convergence tests", confidence="high"
    )

    facts = profiles.select_active_facts(db, class_id)

    assert [row["value"] for row in facts] == ["Convergence tests"]
    assert "Series: Convergence tests" in build_system_prompt("guide", [], facts)


def test_rejected_fact_never_becomes_active(db: sqlite3.Connection, class_id: int) -> None:
    fact_id = _insert_fact(db, class_id, confidence="high", confirmed=1)

    profiles.reject_fact(db, fact_id)

    assert profiles.select_active_facts(db, class_id) == []
    # The row survives rejection: it is the record that blocks re-proposal.
    assert profiles.get_fact(db, fact_id)["rejected"] == 1


def test_user_facts_are_the_facts_belonging_to_no_class(
    db: sqlite3.Connection, class_id: int
) -> None:
    _insert_fact(
        db, None, kind="note", label="Style", value="Prefers worked examples", confidence="high"
    )
    _insert_fact(
        db, class_id, kind="note", label="Lab", value="Attendance required", confidence="high"
    )

    assert [row["value"] for row in profiles.select_user_facts(db)] == ["Prefers worked examples"]
    assert [row["value"] for row in profiles.select_active_facts(db, class_id)] == [
        "Attendance required"
    ]


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({"extraction_enabled": 0, "endpoint_url": LOCAL_ENDPOINT}, "extraction_disabled"),
        ({"extraction_enabled": 1, "endpoint_url": None}, "no_endpoint"),
        (
            {"extraction_enabled": 1, "endpoint_url": REMOTE_ENDPOINT, "remote_ack": 0},
            "remote_unacknowledged",
        ),
    ],
)
def test_extraction_skips_before_anything_is_sent(
    db: sqlite3.Connection,
    document_id: int,
    configuration: dict[str, object],
    expected: str,
) -> None:
    update_settings_row(db, configuration)

    # `refuse_completions` is still in place, so a request here fails the test outright.
    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) == expected
    assert _fact_count(db) == 0


def test_acknowledged_remote_endpoint_is_allowed(
    db: sqlite3.Connection, class_id: int, document_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    update_settings_row(
        db, {"extraction_enabled": 1, "endpoint_url": REMOTE_ENDPOINT, "remote_ack": 1}
    )
    _reply(monkeypatch, MIDTERM_REPLY)

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) is None
    assert _fact_count(db) == 1


@pytest.mark.parametrize(
    "wrapper",
    ["{body}", "```json\n{body}\n```", "```\n{body}\n```", "  ```json\n{body}\n```  "],
)
def test_a_fenced_reply_parses(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
) -> None:
    _reply(monkeypatch, wrapper.format(body=MIDTERM_REPLY))

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) is None

    facts = _facts(db, class_id)
    assert _identities(facts) == {("deadline", "Midterm 1", "2026-03-04")}
    assert facts[0]["confidence"] == "high"


@pytest.mark.parametrize(
    "content",
    [
        "I could not find anything to extract.",
        "",
        '["Midterm 1"]',
        '{"deadlines": [',
    ],
)
def test_an_unusable_reply_is_reported_and_stores_nothing(
    db: sqlite3.Connection,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    _reply(monkeypatch, content)

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) == "unparseable_response"
    assert _fact_count(db) == 0


def test_an_unmarked_item_lands_low_and_stays_out_of_prompts(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(
        monkeypatch,
        json.dumps(
            {
                "notes": [{"label": "Lab", "value": "Bring a calculator"}],
                "topics": ["Series convergence"],
                "deadlines": [{"label": "Final", "date": "2026-05-01", "confidence": "unsure"}],
            }
        ),
    )

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) is None

    assert {fact["label"]: fact["confidence"] for fact in _facts(db, class_id)} == {
        "Lab": "low",
        "Topic": "low",
        "Final": "low",
    }
    assert profiles.select_active_facts(db, class_id) == []


def test_every_key_maps_onto_its_kind_across_shapes(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(
        monkeypatch,
        json.dumps(
            {
                "deadlines": [{"label": "Midterm 1", "date": "2026-03-04", "confidence": "high"}],
                "topics": ["Series convergence", "Taylor polynomials"],
                "grading": {"Midterm": "30%", "Final": "40%"},
                "professor_info": {"name": "Dr Chen", "email": "chen@example.edu"},
                "prerequisites": [
                    {"title": "Calculus I", "value": "MATH 101", "quote": "not in the document"}
                ],
                "notes": "Attendance is required",
            }
        ),
    )

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) is None

    facts = _facts(db, class_id)
    assert _identities(facts) == {
        ("deadline", "Midterm 1", "2026-03-04"),
        ("topic", "Topic", "Series convergence"),
        ("topic", "Topic", "Taylor polynomials"),
        ("grading", "Midterm", "30%"),
        ("grading", "Final", "40%"),
        # No value key, so the object is read as labels and values rather than dropped.
        ("professor", "name", "Dr Chen"),
        ("professor", "email", "chen@example.edu"),
        # A prerequisite is a name, so the entry names one thing however many fields the
        # model wrapped it in. `MATH 101` is that course's code, not a second prerequisite.
        ("prerequisite", "Prerequisite", "Calculus I"),
        ("note", "Note", "Attendance is required"),
    }
    # Not one of these entries quotes anything this document says, so none is promoted:
    # the shape tolerance above decides what a reply *means*, never whether to believe it.
    assert {str(fact["confidence"]) for fact in facts} == {"low"}


def test_items_without_a_value_are_skipped(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(
        monkeypatch,
        json.dumps(
            {
                "topics": ["", "   ", "Series convergence"],
                "notes": [{"label": "Blank", "value": "  "}],
                "grading": {"Participation": None},
            }
        ),
    )

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) is None
    assert _identities(_facts(db, class_id)) == {("topic", "Topic", "Series convergence")}


def test_a_rejected_fact_is_not_proposed_again(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(monkeypatch, MIDTERM_REPLY)
    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)
    fact_id = int(db.execute("select id from profile_facts").fetchone()["id"])
    profiles.reject_fact(db, fact_id)

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)

    rows = db.execute("select id, rejected from profile_facts").fetchall()
    assert [(row["id"], row["rejected"]) for row in rows] == [(fact_id, 1)]
    assert _facts(db, class_id) == []


def test_reingesting_does_not_double_the_profile(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same fact twice in one reply, then the whole reply a second time.
    _reply(
        monkeypatch,
        json.dumps(
            {
                "deadlines": [
                    {"label": "Midterm 1", "date": "2026-03-04", "confidence": "high"},
                    {"label": "Midterm 1", "date": "2026-03-04", "confidence": "high"},
                ]
            }
        ),
    )

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)
    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)

    assert _fact_count(db) == 1


def _topics(*names: str) -> str:
    """An extraction reply proposing each name as a high-confidence topic."""
    return json.dumps({"topics": [{"name": name, "confidence": "high"} for name in names]})


def test_a_second_document_saying_the_same_thing_adds_evidence_not_a_row(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: sixteen uploads make one profile, not sixteen stacked on each other."""
    second = _insert_document(db, class_id, filename="homework_2.pdf")
    _reply(monkeypatch, _topics("Fourier series"))
    profiles.extract_facts(db, document_id, "Homework 1")
    profiles.extract_facts(db, second, "Homework 2")

    facts = _facts(db, class_id)

    assert len(facts) == 1
    assert facts[0]["value"] == "Fourier series"
    assert facts[0]["sources"] == ["syllabus.pdf", "homework_2.pdf"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Punctuation and case are formatting, so they merge with certainty.
        ("Linearity and Time-Invariance", "Linearity and time invariance"),
        # A trailing parenthetical glosses the subject rather than naming it.
        ("Fourier Transform", "Fourier Transform (computation of X(jw))"),
        ("Convolution Property", "Convolution property (periodic convolution)"),
    ],
)
def test_wordings_that_differ_only_by_formatting_are_one_fact(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
) -> None:
    _reply(monkeypatch, _topics(first, second))

    profiles.extract_facts(db, document_id, "Homework 1")

    facts = _facts(db, class_id)
    # The shortest wording survives: among variants that already agree, it is the name.
    assert [fact["value"] for fact in facts] == [min(first, second, key=len)]


def test_a_topic_list_answered_as_a_mapping_keeps_every_entry(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt asks for a list; a model may answer with a numbered object instead."""
    _reply(
        monkeypatch,
        json.dumps({"topics": {"1": "Convolution", "2": "Fourier series"}}),
    )

    profiles.extract_facts(db, document_id, "Homework 1")

    assert _identities(_facts(db, class_id)) == {
        ("topic", "Topic", "Convolution"),
        ("topic", "Topic", "Fourier series"),
    }


def test_wordings_that_differ_by_wording_are_left_for_consolidation(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing here may merge two things a student would call distinct.

    `Time Shift` and `Time-Shift Property` really are one topic, and `Fourier series` and
    `Inverse Fourier series` really are two. Telling those pairs apart takes judgment about
    the subject, so both are left alone and `backend.core.consolidation` decides.
    """
    _reply(monkeypatch, _topics("Time Shift", "Time-Shift Property", "Inverse Fourier series"))

    profiles.extract_facts(db, document_id, "Homework 7")

    assert len(_facts(db, class_id)) == 3


def test_two_documents_corroborate_a_fact_neither_was_sure_of(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = _insert_document(db, class_id, filename="homework_2.pdf")
    _reply(monkeypatch, json.dumps({"topics": ["Convolution"]}))

    profiles.extract_facts(db, document_id, "Homework 1")
    assert profiles.select_active_facts(db, class_id) == []

    profiles.extract_facts(db, second, "Homework 2")

    active = profiles.select_active_facts(db, class_id)
    assert [row["value"] for row in active] == ["Convolution"]
    # Corroboration is evidence, not confirmation. The fact still says nobody has checked it.
    assert active[0]["confirmed"] == 0


def test_the_most_attested_facts_lead_the_profile(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = _insert_document(db, class_id, filename="homework_2.pdf")
    _reply(monkeypatch, _topics("Mentioned once", "Convolution"))
    profiles.extract_facts(db, document_id, "Homework 1")
    _reply(monkeypatch, _topics("Convolution"))
    profiles.extract_facts(db, second, "Homework 2")

    assert [fact["value"] for fact in _facts(db, class_id)] == ["Convolution", "Mentioned once"]


def test_rejecting_a_fact_holds_against_every_later_document(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One rejection, not one per file. Rejecting per document is what made the sheet unusable."""
    second = _insert_document(db, class_id, filename="homework_2.pdf")
    _reply(monkeypatch, _topics("Continuous-Signal Processing"))
    profiles.extract_facts(db, document_id, "Homework 1")
    profiles.reject_fact(db, int(db.execute("select id from profile_facts").fetchone()["id"]))

    profiles.extract_facts(db, second, "Homework 2")

    assert _facts(db, class_id) == []
    assert _fact_count(db) == 1


def test_correcting_a_value_marks_the_fact_as_the_users(
    db: sqlite3.Connection, class_id: int
) -> None:
    """`edited` is what stops the consolidation pass merging a correction away."""
    fact_id = _insert_fact(db, class_id)

    profiles.update_fact_value(db, fact_id, "2026-03-11")

    assert profiles.get_fact(db, fact_id)["edited"] == 1


def test_deleting_a_document_withdraws_what_only_it_claimed(
    db: sqlite3.Connection, class_id: int
) -> None:
    shared = _insert_document(db, class_id, filename="homework_1.pdf")
    other = _insert_document(db, class_id, filename="homework_2.pdf")
    sole = _insert_fact(db, class_id, kind="topic", value="Only here", source_document_id=shared)
    corroborated = _insert_fact(
        db, class_id, kind="topic", value="Said twice", source_document_id=shared
    )
    db.execute(
        "insert into profile_fact_sources (fact_id, document_id) values (?, ?)",
        (corroborated, other),
    )
    mine = _insert_fact(db, class_id, kind="note", value="I kept this", source_document_id=shared)
    profiles.confirm_fact(db, mine)
    db.commit()

    assert profiles.forget_document_evidence(db, shared) == 1
    db.commit()

    remaining = {int(row["id"]) for row in db.execute("select id from profile_facts")}
    assert sole not in remaining
    # Still evidenced elsewhere, and still the user's own decision.
    assert {corroborated, mine} <= remaining


def test_upgrading_folds_the_duplicates_already_on_disk(tmp_path: Path) -> None:
    """The class that prompted this change already has sixteen copies of its course code.

    An upgrade that only changed the rule going forward would leave every one of them
    sitting there, so the migration folds the exact duplicates and carries their decisions.
    """
    conn = connect(tmp_path / "old.db")
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))[:8]:
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("insert into classes (name) values ('ECE203')")
        for index in range(3):
            conn.execute(
                "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
                "values (1, ?, ?, 'application/pdf', 1024, 'ready')",
                (f"homework_{index}.pdf", f"1/homework_{index}.pdf"),
            )
            conn.execute(
                "insert into profile_facts (class_id, kind, label, value, confidence, "
                "rejected, source_document_id) values (1, 'note', 'Course', 'ECE203', 'high', "
                "?, ?)",
                (1 if index == 2 else 0, index + 1),
            )
        conn.execute("pragma user_version = 8")
        conn.commit()

        migrate(conn)

        rows = conn.execute("select id, rejected from profile_facts").fetchall()
        assert len(rows) == 1
        # One copy was rejected, so the survivor is. A decision does not get lost in a fold.
        assert rows[0]["rejected"] == 1
        evidence = conn.execute(
            "select count(*) from profile_fact_sources where fact_id = ?", (rows[0]["id"],)
        ).fetchone()[0]
        assert evidence == 3
    finally:
        conn.close()


def test_document_text_is_truncated_to_the_extraction_budget(
    db: sqlite3.Connection,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_settings_row(db, {"context_window": 1000})
    sent = _reply(monkeypatch, "{}")

    profiles.extract_facts(db, document_id, "x" * 10_000)

    # 60 percent of a 1000 token window, at four characters per token.
    assert len(sent[0][1]["content"]) == 2400


def test_a_large_context_window_does_not_enlarge_the_extraction_prompt(
    db: sqlite3.Connection,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window sized for chat must not decide how much of a document extraction reads.

    Left uncapped, a 262144-token window sent 629,144 characters of a problem set per
    upload. That overran the client's read timeout, so the pass returned nothing at all,
    and because ingestion runs one document at a time it held every later upload behind it.
    """
    update_settings_row(db, {"context_window": 262_144})
    sent = _reply(monkeypatch, "{}")

    profiles.extract_facts(db, document_id, "x" * 1_000_000)

    assert len(sent[0][1]["content"]) == profiles.EXTRACTION_MAX_TOKENS * 4


def test_the_default_window_is_unaffected_by_the_cap(
    db: sqlite3.Connection,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap exists to stop a raised window inflating the prompt, not to shrink the
    # budget the extraction prompt was written against.
    update_settings_row(db, {"context_window": 8192})
    sent = _reply(monkeypatch, "{}")

    profiles.extract_facts(db, document_id, "x" * 1_000_000)

    assert len(sent[0][1]["content"]) == int(8192 * profiles.EXTRACTION_BUDGET_SHARE) * 4


def test_extracted_facts_carry_the_document_class_and_source(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(monkeypatch, MIDTERM_REPLY)

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)

    fact = _facts(db, class_id)[0]
    assert fact["class_id"] == class_id
    assert fact["source_document_id"] == document_id
    assert fact["source_filename"] == "syllabus.pdf"
    assert fact["confirmed"] is False
    assert fact["rejected"] is False


def test_class_profile_keeps_unconfirmed_facts_and_drops_rejected_ones(
    db: sqlite3.Connection, class_id: int
) -> None:
    _insert_fact(db, class_id, label="Kept", confidence="low")
    rejected = _insert_fact(db, class_id, label="Gone", confidence="high", confirmed=1)
    profiles.reject_fact(db, rejected)

    assert [fact["label"] for fact in _facts(db, class_id)] == ["Kept"]


def test_skip_reason_only_reflects_the_most_recent_ingestion(
    db: sqlite3.Connection, class_id: int
) -> None:
    _insert_document(db, class_id, filename="one.pdf", stage_detail="extraction_disabled")
    _insert_document(db, class_id, filename="two.pdf", stage_detail="remote_unacknowledged")
    # A later successful extraction must clear an older explanation in the profile UI.
    _insert_document(db, class_id, filename="three.pdf", stage_detail=None)

    profile = profiles.get_class_profile(db, class_id)

    assert profile["extraction_skipped_reason"] is None


def test_no_skip_reason_when_no_document_carries_one(db: sqlite3.Connection, class_id: int) -> None:
    _insert_document(db, class_id, stage_detail=None)

    assert profiles.get_class_profile(db, class_id)["extraction_skipped_reason"] is None


def _chunk_for(db: sqlite3.Connection, class_id: int, document_id: int) -> None:
    """One chunk row, standing in for an ingestion that reached the extraction stage.

    Chunk ids are the only per-ingestion write order the schema records, and every
    document whose `stage_detail` can carry a skip reason has chunks, because extraction
    runs only after the chunks are stored.
    """
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, doc_type, "
        "embedding_model, embedding_dim) values (?, ?, 'text', 1, 'notes', 'nomic', 768)",
        (document_id, class_id),
    )
    db.commit()


def test_skip_reason_follows_ingestion_order_not_document_id(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Re-ingesting the oldest document in a class is still the latest ingestion.

    `max(document id)` called the newest *row* the newest *run*, so re-ingesting an older
    upload most recently - after removing the endpoint, say - reported whatever the
    higher-id document's outcome had been instead of the run the student just watched.
    """
    older = _insert_document(db, class_id, filename="one.pdf", stage_detail="no_endpoint")
    newer = _insert_document(db, class_id, filename="two.pdf", stage_detail=None)
    # two.pdf ingested after one.pdf, then one.pdf re-ingested last: its chunks are
    # replaced, so its surviving chunk ids are the highest in the class.
    _chunk_for(db, class_id, older)
    _chunk_for(db, class_id, newer)
    db.execute("delete from chunks where document_id = ?", (older,))
    db.commit()
    _chunk_for(db, class_id, older)

    profile = profiles.get_class_profile(db, class_id)

    assert profile["extraction_skipped_reason"] == "no_endpoint"


def test_facts_are_never_attested_against_a_replaced_document(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete mid-extraction, re-upload, and the old file's facts must not land at all.

    The model call takes minutes and deleting the document mid-run is the de facto cancel
    for a long ingestion. A delete followed by an immediate re-upload can put a different
    file behind the same id, and attesting the old file's facts against it would
    contaminate the new document permanently.
    """

    async def replace_mid_call(*args: object, **kwargs: object) -> str:
        # The student deletes the document and uploads a different file that lands on the
        # same id, all while the model is reading the old one.
        other = connect()
        try:
            other.execute("delete from documents where id = ?", (document_id,))
            other.execute(
                "insert into documents (id, class_id, filename, stored_path, mime, "
                "byte_size, state, created_at) "
                "values (?, ?, 'newer.pdf', 'x/newer.pdf', 'application/pdf', 1, "
                "'pending', '2099-01-01 00:00:00')",
                (document_id, class_id),
            )
            other.commit()
        finally:
            other.close()
        return MIDTERM_REPLY

    monkeypatch.setattr(profiles.client, "complete", replace_mid_call)

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)

    assert db.execute("select count(*) from profile_facts").fetchone()[0] == 0
    assert db.execute("select count(*) from profile_fact_sources").fetchone()[0] == 0


def test_a_failed_extraction_records_its_reason_for_the_profile_to_explain(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reply(monkeypatch, "not json")

    reason = profiles.extract_facts(db, document_id, SYLLABUS_TEXT)
    # Standing in for the ingestion step, which is what writes the reason down.
    db.execute("update documents set stage_detail = ? where id = ?", (reason, document_id))
    db.commit()

    assert profiles.get_class_profile(db, class_id) == {
        "facts": [],
        "extraction_skipped_reason": "unparseable_response",
    }


def test_correcting_a_value_does_not_confirm_the_fact(
    db: sqlite3.Connection, class_id: int
) -> None:
    fact_id = _insert_fact(db, class_id, value="2026-03-05", confidence="low")

    profiles.update_fact_value(db, fact_id, "  2026-03-04  ")

    row = profiles.get_fact(db, fact_id)
    assert row["value"] == "2026-03-04"
    assert row["confirmed"] == 0
    assert profiles.select_active_facts(db, class_id) == []


def test_mutations_refuse_an_unknown_fact_id(db: sqlite3.Connection) -> None:
    with pytest.raises(NotFoundError):
        profiles.confirm_fact(db, 404)
    with pytest.raises(NotFoundError):
        profiles.reject_fact(db, 404)
    with pytest.raises(NotFoundError):
        profiles.update_fact_value(db, 404, "anything")


def test_class_profile_route_returns_facts_and_the_skip_reason(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    source = _insert_document(db, class_id, stage_detail="extraction_disabled")
    _insert_fact(db, class_id, confidence="low", source_document_id=source)

    body = client.get(f"/api/classes/{class_id}/profile").json()

    assert body["extraction_skipped_reason"] == "extraction_disabled"
    assert body["facts"][0]["source_filename"] == "syllabus.pdf"
    assert body["facts"][0]["confirmed"] is False


def test_class_profile_route_rejects_an_unknown_class(client: TestClient) -> None:
    assert client.get("/api/classes/404/profile").status_code == 404


def test_confirm_route_activates_a_low_confidence_fact(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    fact_id = _insert_fact(db, class_id, confidence="low")

    response = client.post(
        f"/api/classes/{class_id}/profile/confirm",
        json={"fact_id": fact_id, "action": "confirm"},
    )

    assert response.status_code == 200
    assert response.json()["facts"][0]["confirmed"] is True
    assert [row["id"] for row in profiles.select_active_facts(db, class_id)] == [fact_id]


def test_reject_route_drops_the_fact_from_the_profile(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    fact_id = _insert_fact(db, class_id, confidence="high")

    response = client.post(
        f"/api/classes/{class_id}/profile/confirm",
        json={"fact_id": fact_id, "action": "reject"},
    )

    assert response.json()["facts"] == []
    assert profiles.get_fact(db, fact_id)["rejected"] == 1


def test_patch_route_corrects_a_value(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    fact_id = _insert_fact(db, class_id, value="2026-03-05")

    body = client.patch(
        f"/api/classes/{class_id}/profile", json={"fact_id": fact_id, "value": " 2026-03-04 "}
    ).json()

    assert body["facts"][0]["value"] == "2026-03-04"


def test_patch_route_refuses_a_blank_value(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    fact_id = _insert_fact(db, class_id)

    response = client.patch(
        f"/api/classes/{class_id}/profile", json={"fact_id": fact_id, "value": "   "}
    )

    assert response.status_code == 422


def test_a_fact_from_another_class_cannot_be_reached_through_a_class_route(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    other_id = int(
        db.execute("insert into classes (name) values ('Linear Algebra')").lastrowid or 0
    )
    db.commit()
    foreign = _insert_fact(db, other_id, confidence="low")

    patched = client.patch(
        f"/api/classes/{class_id}/profile", json={"fact_id": foreign, "value": "changed"}
    )
    confirmed = client.post(
        f"/api/classes/{class_id}/profile/confirm",
        json={"fact_id": foreign, "action": "confirm"},
    )

    assert patched.status_code == 404
    assert confirmed.status_code == 404
    row = profiles.get_fact(db, foreign)
    assert row["value"] == "2026-03-04"
    assert row["confirmed"] == 0


def test_user_profile_routes_only_reach_facts_with_no_class(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    user_fact = _insert_fact(
        db, None, kind="note", label="Style", value="Prefers worked examples", confidence="high"
    )
    class_fact = _insert_fact(db, class_id, confidence="high")

    listed = client.get("/api/profile").json()
    corrected = client.patch("/api/profile", json={"fact_id": user_fact, "value": "Prefers hints"})
    refused = client.patch("/api/profile", json={"fact_id": class_fact, "value": "changed"})

    assert [fact["id"] for fact in listed["facts"]] == [user_fact]
    assert corrected.json()["facts"][0]["value"] == "Prefers hints"
    assert refused.status_code == 404


# --------------------------------------------------------------------------------------
# Quote verification: the check that replaced asking the model how sure it was.


def _quoted(quote: str, label: str = "Midterm 1") -> str:
    """An extraction reply proposing one deadline backed by `quote`."""
    return json.dumps({"deadlines": [{"label": label, "value": "2026-03-04", "quote": quote}]})


@pytest.mark.parametrize(
    ("quote", "expected", "reason"),
    [
        (
            "Midterm 1 will be held on 2026-03-04 in the main hall.",
            "high",
            "the document says exactly this",
        ),
        (
            "midterm 1 WILL be held on 2026-03-04",
            "high",
            "case is not a difference in what was said",
        ),
        (
            "Midterm 1 will be held   on 2026-03-04",
            "high",
            "a PDF's spacing is not a difference either",
        ),
        (
            "Midterm 1 will be held on 2026‑03‑04",
            "high",
            "a typographic dash retyped as a hyphen is the same sentence",
        ),
        (
            "The midterm is on the fourth of March.",
            "low",
            "a true paraphrase is still not something the document says",
        ),
        ("Midterm 1 is cancelled.", "low", "an invented sentence cannot be found"),
        ("", "low", "no evidence offered at all"),
        ("Midterm", "low", "too short to be evidence of anything"),
    ],
)
def test_a_fact_is_believed_only_when_its_quote_is_really_in_the_document(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
    quote: str,
    expected: str,
    reason: str,
) -> None:
    _reply(monkeypatch, _quoted(quote))

    assert profiles.extract_facts(db, document_id, SYLLABUS_TEXT) is None

    facts = _facts(db, class_id)
    assert [str(fact["confidence"]) for fact in facts] == [expected], reason


def test_an_unverified_fact_is_kept_and_shown_but_never_reaches_a_prompt(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demotion, not deletion. The row is what the student rules on."""
    _reply(monkeypatch, _quoted("The midterm was moved to April, per the announcement."))

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)

    assert len(_facts(db, class_id)) == 1
    assert profiles.select_active_facts(db, class_id) == []


def test_a_quote_is_checked_against_what_the_model_was_shown_not_the_whole_file(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model cannot have read past the truncation, so a quote from there is not evidence.

    Without this the budget cut would be the one place a hallucination could be laundered
    into a trusted fact: the model guesses a sentence, the sentence happens to occur on a
    page nobody sent it, and the guess is promoted straight into every chat prompt.
    """
    tail = "Final project proposals are due on 2026-04-20."
    monkeypatch.setattr(profiles, "extraction_budget_chars", lambda window: len(SYLLABUS_TEXT))
    _reply(monkeypatch, _quoted(tail, label="Final project"))

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT + tail)

    assert [str(fact["confidence"]) for fact in _facts(db, class_id)] == ["low"]


def test_the_quote_key_never_becomes_a_fact_of_its_own(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_read_object` reads an unrecognised object's own keys as labels, so it must skip it."""
    _reply(
        monkeypatch,
        json.dumps(
            {
                "professor_info": [
                    {"name": "Dr Chen", "quote": "Instructor: Dr Chen", "confidence": "high"}
                ]
            }
        ),
    )

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT)

    assert {str(fact["label"]) for fact in _facts(db, class_id)} == {"name"}


def test_extraction_asks_for_the_schema_and_temperature_its_document_type_earns(
    db: sqlite3.Connection,
    document_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring test: a prompt that constrains nothing is a prompt a small model ignores."""
    seen: dict[str, object] = {}

    async def _complete(*args: object, **kwargs: object) -> str:
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(profiles.client, "complete", _complete)

    profiles.extract_facts(db, document_id, SYLLABUS_TEXT, "homework")

    assert seen["temperature"] == 0.0
    schema = cast(JsonSchema, seen["schema"])
    assert set(schema.schema["properties"]) == {"topics", "notes", "deadlines"}
