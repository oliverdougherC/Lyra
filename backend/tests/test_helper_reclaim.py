"""Tests for the trusted helper-reclamation CLI."""

from __future__ import annotations

import io
import json

import pytest

from backend.llm import helper_reclaim


class _FakeHelper:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.closed = False
        self._error = error

    def close_admission_for_app_quit(self) -> None:
        self.closed = True

    def stop_for_app_quit(self) -> None:
        assert self.closed
        self.calls += 1
        if self._error is not None:
            raise self._error


def test_reclaim_owned_helpers_calls_every_selected_helper_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = _FakeHelper()
    broken = _FakeHelper(error=RuntimeError("stuck"))
    reranking = _FakeHelper()
    monkeypatch.setattr(
        helper_reclaim,
        "_HELPERS",
        {
            "embedding": embedding,
            "text-recognition": broken,
            "reranking": reranking,
        },
    )

    states = {
        ("embedding", 0): "live",
        ("embedding", 1): "absent",
        ("text-recognition", 0): "live",
        ("reranking", 0): "stale",
        ("reranking", 1): "absent",
    }
    seen: dict[str, int] = {}

    def fake_record_state(service: str) -> str:
        count = seen.get(service, 0)
        seen[service] = count + 1
        return states[(service, count)]

    monkeypatch.setattr(helper_reclaim, "_record_state", fake_record_state)

    payload = helper_reclaim.reclaim_owned_helpers()

    assert payload["status"] == "error"
    assert embedding.calls == 1
    assert broken.calls == 1
    assert reranking.calls == 1
    assert payload["services"] == [
        {"service": "embedding", "before": "live", "after": "absent", "ok": True},
        {"service": "text-recognition", "ok": False, "error": "RuntimeError"},
        {"service": "reranking", "before": "stale", "after": "absent", "ok": True},
    ]


def test_reclaim_owned_helpers_fails_when_owned_helper_remains_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _FakeHelper()
    monkeypatch.setattr(helper_reclaim, "_HELPERS", {"reranking": helper})

    states = {
        ("reranking", 0): "live",
        ("reranking", 1): "live",
    }
    seen: dict[str, int] = {}

    def fake_record_state(service: str) -> str:
        count = seen.get(service, 0)
        seen[service] = count + 1
        return states[(service, count)]

    monkeypatch.setattr(helper_reclaim, "_record_state", fake_record_state)

    payload = helper_reclaim.reclaim_owned_helpers()

    assert helper.calls == 1
    assert payload == {
        "status": "error",
        "services": [
            {
                "service": "reranking",
                "before": "live",
                "after": "live",
                "ok": False,
                "error": "still_live",
            }
        ],
    }


def test_main_emits_one_json_line_and_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    captured: list[list[str] | None] = []

    def fake_reclaim(*, services: list[str] | None = None) -> dict[str, object]:
        captured.append(services)
        return {"status": "ok", "services": [{"service": "embedding", "ok": True}]}

    monkeypatch.setattr(helper_reclaim, "reclaim_owned_helpers", fake_reclaim)

    exit_code = helper_reclaim.main(["--service", "embedding"], stream=stream)

    assert exit_code == 0
    assert captured == [["embedding"]]
    assert json.loads(stream.getvalue()) == {
        "status": "ok",
        "services": [{"service": "embedding", "ok": True}],
    }
    assert stream.getvalue().count("\n") == 1


def test_main_returns_nonzero_when_any_helper_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        helper_reclaim,
        "reclaim_owned_helpers",
        lambda *, services=None: {
            "status": "error",
            "services": [{"service": "reranking", "ok": False, "error": "unreadable"}],
        },
    )

    assert helper_reclaim.main([]) == 1


def test_main_reports_live_helper_as_failed_json_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _FakeHelper()
    stream = io.StringIO()
    monkeypatch.setattr(helper_reclaim, "_HELPERS", {"reranking": helper})
    monkeypatch.setattr(helper_reclaim, "_record_state", lambda _service: "live")

    exit_code = helper_reclaim.main(["--service", "reranking"], stream=stream)

    assert exit_code == 1
    assert json.loads(stream.getvalue()) == {
        "status": "error",
        "services": [
            {
                "service": "reranking",
                "before": "live",
                "after": "live",
                "ok": False,
                "error": "still_live",
            }
        ],
    }


def test_reclamation_closes_all_admission_before_first_stop(monkeypatch):
    helpers = {name: _FakeHelper() for name in ("embedding", "reranking")}
    monkeypatch.setattr(helper_reclaim, "_HELPERS", helpers)
    monkeypatch.setattr(helper_reclaim, "_record_state", lambda service: "absent")

    def first_stop():
        assert all(helper.closed for helper in helpers.values())
        raise RuntimeError("first helper cleanup failed")

    monkeypatch.setattr(helpers["embedding"], "stop_for_app_quit", first_stop)
    result = helper_reclaim.reclaim_owned_helpers()
    assert result["status"] == "error"
    assert helpers["reranking"].calls == 1
