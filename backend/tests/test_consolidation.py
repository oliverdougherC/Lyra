"""The class-scope consolidation pass: what it merges, what it demotes, what it refuses.

No test reaches a live model. `complete` is replaced at the reference
`backend.core.consolidation` actually calls, and the default stub fails the test the moment
it is reached, so a rule that is supposed to send nothing cannot pass by accident.
"""

import json
import socket
import sqlite3

import keyring.errors
import pytest

from backend.core import consolidation
from backend.core.app_settings import update_settings_row
from backend.storage import secrets

LOCAL_ENDPOINT = "http://127.0.0.1:8080/v1"
REMOTE_ENDPOINT = "https://tutor.example.com/v1"

RESOLUTIONS = {"127.0.0.1": ["127.0.0.1"], "tutor.example.com": ["203.0.113.10"]}


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
        raise AssertionError("Consolidation reached the tutor endpoint")

    monkeypatch.setattr(consolidation.client, "complete", _refuse)


@pytest.fixture
def local_extraction(db: sqlite3.Connection) -> None:
    """Settings that permit the pass: extraction enabled, against a loopback endpoint."""
    update_settings_row(db, {"endpoint_url": LOCAL_ENDPOINT, "extraction_enabled": 1})


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

    monkeypatch.setattr(consolidation.client, "complete", _complete)
    return sent


def _document(db: sqlite3.Connection, class_id: int, filename: str) -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, ?, 'application/pdf', 2048, 'ready')",
        (class_id, filename, f"{class_id}/{filename}"),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _fact(
    db: sqlite3.Connection,
    class_id: int,
    value: str,
    kind: str = "topic",
    confidence: str = "high",
    confirmed: int = 0,
    edited: int = 0,
) -> int:
    cursor = db.execute(
        "insert into profile_facts (class_id, kind, label, value, confidence, confirmed, edited) "
        "values (?, ?, ?, ?, ?, ?, ?)",
        (class_id, kind, kind.capitalize(), value, confidence, confirmed, edited),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _seed(db: sqlite3.Connection, class_id: int, *values: str) -> list[int]:
    """Enough facts to clear `MIN_ENTRIES`, in the order the pass will number them."""
    return [_fact(db, class_id, value) for value in values]


def _values(db: sqlite3.Connection, class_id: int) -> list[str]:
    return [
        str(row["value"])
        for row in db.execute(
            "select value from profile_facts where class_id = ? order by id", (class_id,)
        )
    ]


SEVEN = (
    "Time Shift",
    "Time-Shift Property",
    "Convolution",
    "Fourier series",
    "Laplace transform",
    "Homework Assignment 5",
    "Region of convergence",
)


def test_a_duplicate_group_folds_onto_its_first_member(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed(db, class_id, *SEVEN)
    _reply(monkeypatch, json.dumps({"duplicates": [[1, 2]]}))

    consolidation.consolidate_class(db, class_id)

    assert "Time-Shift Property" not in _values(db, class_id)
    assert "Time Shift" in _values(db, class_id)
    assert ids[1] not in {int(row["id"]) for row in db.execute("select id from profile_facts")}


def test_a_merge_carries_the_losers_evidence_to_the_winner(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count is the ordering and the corroboration, so it cannot be dropped on the floor."""
    ids = _seed(db, class_id, *SEVEN)
    first = _document(db, class_id, "homework_1.pdf")
    second = _document(db, class_id, "homework_7.pdf")
    db.executemany(
        "insert into profile_fact_sources (fact_id, document_id) values (?, ?)",
        [(ids[0], first), (ids[1], second)],
    )
    db.commit()
    _reply(monkeypatch, json.dumps({"duplicates": [[1, 2]]}))

    consolidation.consolidate_class(db, class_id)

    sources = db.execute(
        "select document_id from profile_fact_sources where fact_id = ? order by document_id",
        (ids[0],),
    ).fetchall()
    assert [int(row["document_id"]) for row in sources] == [first, second]


def test_document_metadata_is_set_aside_rather_than_deleted(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`low` already means stored, visible, and out of every prompt. A wrong call costs a click."""
    ids = _seed(db, class_id, *SEVEN)
    _reply(monkeypatch, json.dumps({"not_about_the_course": [6]}))

    consolidation.consolidate_class(db, class_id)

    row = db.execute(
        "select value, confidence from profile_facts where id = ?", (ids[5],)
    ).fetchone()
    assert row["value"] == "Homework Assignment 5"
    assert row["confidence"] == "low"


def test_a_number_that_was_never_sent_changes_nothing(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acting only on entries it was given is the whole safety of the pass."""
    _seed(db, class_id, *SEVEN)
    _reply(
        monkeypatch,
        json.dumps({"duplicates": [[99, 100], [1, 42]], "not_about_the_course": [0, "eight"]}),
    )

    consolidation.consolidate_class(db, class_id)

    assert _values(db, class_id) == list(SEVEN)
    assert not db.execute("select 1 from profile_facts where confidence = 'low' limit 1").fetchone()


def test_a_fact_the_user_has_ruled_on_is_never_touched(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmation, a rejection, and a correction each take a fact out of the model's reach."""
    _seed(db, class_id, *SEVEN)
    _fact(db, class_id, "Confirmed by hand", confirmed=1)
    _fact(db, class_id, "Corrected by hand", edited=1)
    sent = _reply(monkeypatch, json.dumps({}))

    consolidation.consolidate_class(db, class_id)

    listed = sent[0][1]["content"]
    assert "Confirmed by hand" not in listed
    assert "Corrected by hand" not in listed


def test_nothing_new_costs_no_model_call(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(db, class_id, *SEVEN)
    _reply(monkeypatch, json.dumps({}))
    consolidation.consolidate_class(db, class_id)

    async def _refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("A second pass over an unchanged profile sent a request")

    monkeypatch.setattr(consolidation.client, "complete", _refuse)
    consolidation.consolidate_class(db, class_id)


def test_a_profile_too_small_to_have_duplicates_is_not_sent(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
) -> None:
    _seed(db, class_id, "Convolution", "Fourier series")

    consolidation.consolidate_class(db, class_id)

    assert (
        db.execute("select count(*) from profile_facts where consolidated = 0").fetchone()[0] == 0
    )


def test_an_unusable_reply_leaves_the_profile_as_it_was(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass is not load-bearing: the deterministically merged profile is still a profile."""
    _seed(db, class_id, *SEVEN)
    _reply(monkeypatch, "I merged a few of those for you.")

    consolidation.consolidate_class(db, class_id)

    assert _values(db, class_id) == list(SEVEN)


def test_a_failure_mid_apply_rolls_the_merges_back(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge is only real once the whole pass commits.

    `_apply_merges` deletes losing rows without committing; the commit lives at the end,
    inside `_mark_consolidated`. An exception in between used to leave those deletes
    sitting uncommitted on the shared worker connection, where the next unrelated commit
    would silently make half a consolidation real. Now the pass rolls back and the
    profile is exactly what it was.
    """
    ids = _seed(db, class_id, *SEVEN)
    _reply(monkeypatch, json.dumps({"duplicates": [[1, 2]]}))
    monkeypatch.setattr(
        consolidation,
        "_apply_demotions",
        lambda conn, payload, rows: (_ for _ in ()).throw(RuntimeError("died mid-apply")),
    )

    with pytest.raises(RuntimeError):
        consolidation.consolidate_class(db, class_id)
    # The next unrelated commit on the shared connection, which is the write that used to
    # smuggle the uncommitted deletes in.
    db.commit()

    assert _values(db, class_id) == list(SEVEN)
    assert ids[1] in {int(row["id"]) for row in db.execute("select id from profile_facts")}
    # Nothing was marked either, so the pass runs in full on the next upload.
    assert (
        db.execute("select count(*) from profile_facts where consolidated = 1").fetchone()[0] == 0
    )


def test_an_unacknowledged_remote_endpoint_is_never_sent_to(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Fact labels are course content, and the gate covers them for the reason it covers text."""
    update_settings_row(db, {"endpoint_url": REMOTE_ENDPOINT, "extraction_enabled": 1})
    _seed(db, class_id, *SEVEN)

    consolidation.consolidate_class(db, class_id)

    # `refuse_completions` would already have failed this. Nothing was marked either, so the
    # pass runs in full once the acknowledgement is given rather than treating these as seen.
    assert (
        db.execute("select count(*) from profile_facts where consolidated = 1").fetchone()[0] == 0
    )


def test_deadlines_and_grading_are_left_alone(
    db: sqlite3.Connection,
    class_id: int,
    local_extraction: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking a model whether two exams are one exam risks a date the student needs."""
    _seed(db, class_id, *SEVEN)
    _fact(db, class_id, "2026-03-04", kind="deadline")
    _fact(db, class_id, "30%", kind="grading")
    sent = _reply(monkeypatch, json.dumps({}))

    consolidation.consolidate_class(db, class_id)

    listed = sent[0][1]["content"]
    assert "2026-03-04" not in listed
    assert "30%" not in listed
