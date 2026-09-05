"""Guards for the shared weights download: metadata, atomicity, deduplication, failure.

Everything runs without a network: `hf_hub_download` is replaced with a fake that either
lands a file in the models directory or fails. The contract under test is what a
clean-install first use depends on (PLA-402): the file lands where `settings` says it
lands, an interrupted download can never look installed, a burst of concurrent first
uses starts exactly one download, and a failure is a retryable sentence rather than a
command to run.
"""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import model_provisioning
from backend.llm.model_provisioning import (
    EMBEDDING_REVISION,
    EMBEDDING_WEIGHTS,
    RERANK_WEIGHTS,
    ensure_weight,
    require_disk_space,
)


def _land_download(*, repo_id: str, filename: str, local_dir: object, revision: str | None) -> str:
    """A successful `hf_hub_download`: the file is moved to its final name."""
    path = local_dir / filename  # type: ignore[operator]
    path.write_bytes(b"GGUF fake")
    return str(path)


def test_metadata_names_the_paths_the_settings_already_use() -> None:
    """The two must stay one fact: a drift here means a download to a path Lyra never reads."""
    assert EMBEDDING_WEIGHTS.path == settings.embedding_model_path
    assert RERANK_WEIGHTS.path == settings.rerank_model_path


def test_the_embedding_model_is_pinned_to_an_immutable_commit() -> None:
    """A fresh install downloads from a content address, not from whatever the repository's
    default branch holds today: the revision is a full 40-character SHA, and it is the same
    fact in the constant and in the spec the download consumes."""
    revision = EMBEDDING_WEIGHTS.revision
    assert revision is not None
    assert len(revision) == 40
    assert all(c in "0123456789abcdef" for c in revision)
    assert revision == EMBEDDING_REVISION
    # The optional reranker is deliberately not pinned (it is not load-bearing for first
    # use); the field says so rather than leaving it implicit.
    assert RERANK_WEIGHTS.revision is None


def test_an_existing_file_is_never_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(b"weights")
    monkeypatch.setattr(
        model_provisioning,
        "hf_hub_download",
        lambda *args, **kwargs: pytest.fail("an existing file was downloaded again"),
    )

    assert ensure_weight(EMBEDDING_WEIGHTS) == target


def test_a_missing_file_is_downloaded_to_the_models_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, object, str | None]] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        calls.append((repo_id, filename, local_dir, revision))
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    assert not EMBEDDING_WEIGHTS.path.exists()
    assert ensure_weight(EMBEDDING_WEIGHTS) == EMBEDDING_WEIGHTS.path

    assert calls == [
        (
            "nomic-ai/nomic-embed-text-v1.5-GGUF",
            "nomic-embed-text-v1.5.Q8_0.gguf",
            settings.models_dir,
            # The exact commit the weights were downloaded and tested on - the full SHA,
            # never a branch or a tag.
            "0188c9bf409793f810680a5a431e7b899c46104c",
        )
    ]
    assert EMBEDDING_WEIGHTS.path.exists()


def test_concurrent_first_uses_share_one_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of first-use requests must start one download, not one per thread."""
    calls: list[str] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        calls.append(filename)
        time.sleep(0.1)  # long enough that every other thread is waiting on it
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    together = threading.Event()
    results: list[object] = []
    errors: list[Exception] = []

    def worker() -> None:
        together.wait()
        try:
            results.append(ensure_weight(EMBEDDING_WEIGHTS))
        except Exception as exc:  # noqa: BLE001 - the test records the outcome
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    together.set()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == [EMBEDDING_WEIGHTS.path] * 4
    assert calls == [EMBEDDING_WEIGHTS.filename]


def test_a_failed_download_leaves_no_file_and_a_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user gets a retryable sentence: no command, no path, no stack trace."""

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    with pytest.raises(ConfigurationError) as caught:
        ensure_weight(EMBEDDING_WEIGHTS)

    assert not EMBEDDING_WEIGHTS.path.exists()
    message = caught.value.message
    assert "could not be downloaded" in message
    assert "try again" in message
    assert "fetch_models" not in message
    assert str(settings.models_dir) not in message
    assert "httpx" not in message
    assert "ConnectError" not in message


def test_a_waiter_retries_after_a_failed_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request blocked behind a failed download is not dead with it: it retries."""
    hold_first_attempt = threading.Event()
    first_attempt_started = threading.Event()
    attempts = 0

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            hold_first_attempt.wait()
            raise httpx.ConnectError("network unreachable")
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    leader_errors: list[Exception] = []

    def leader() -> None:
        try:
            ensure_weight(EMBEDDING_WEIGHTS)
        except Exception as exc:  # noqa: BLE001 - the test records the outcome
            leader_errors.append(exc)

    waiter_results: list[object] = []
    waiter_errors: list[Exception] = []

    def waiter() -> None:
        try:
            waiter_results.append(ensure_weight(EMBEDDING_WEIGHTS))
        except Exception as exc:  # noqa: BLE001 - the test records the outcome
            waiter_errors.append(exc)

    leader_thread = threading.Thread(target=leader)
    leader_thread.start()
    first_attempt_started.wait(timeout=5)
    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    hold_first_attempt.set()  # the leader fails
    leader_thread.join()
    waiter_thread.join()

    assert len(leader_errors) == 1
    assert isinstance(leader_errors[0], ConfigurationError)
    assert not waiter_errors
    assert waiter_results == [EMBEDDING_WEIGHTS.path]
    assert attempts == 2


def test_after_a_failure_the_next_caller_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        attempts.append(filename)
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    with pytest.raises(ConfigurationError):
        ensure_weight(EMBEDDING_WEIGHTS)
    with pytest.raises(ConfigurationError):
        ensure_weight(EMBEDDING_WEIGHTS)

    assert attempts == [EMBEDDING_WEIGHTS.filename, EMBEDDING_WEIGHTS.filename]


def test_download_in_progress_is_true_only_while_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    download_started = threading.Event()

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        download_started.set()
        release.wait()
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    thread = threading.Thread(target=lambda: ensure_weight(EMBEDDING_WEIGHTS))
    thread.start()
    try:
        download_started.wait(timeout=5)
        assert model_provisioning.download_in_progress(EMBEDDING_WEIGHTS.filename)
        release.set()
    finally:
        thread.join()

    assert not model_provisioning.download_in_progress(EMBEDDING_WEIGHTS.filename)


def test_a_leftover_temporary_file_is_not_treated_as_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted download leaves a process-unique temporary file, never the final
    name: the next `ensure_weight` starts fresh rather than adopting the partial."""
    leftover = settings.models_dir / (EMBEDDING_WEIGHTS.filename + ".deadbeef.incomplete")
    leftover.write_bytes(b"partial")

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    assert ensure_weight(EMBEDDING_WEIGHTS) == EMBEDDING_WEIGHTS.path
    assert EMBEDDING_WEIGHTS.path.exists()


def test_progress_reports_the_download_line(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None
    ) -> str:
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

    ensure_weight(EMBEDDING_WEIGHTS, progress=lines.append)

    assert lines == [f"Downloading {EMBEDDING_WEIGHTS.filename} ..."]


def test_not_enough_free_space_refuses_before_any_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_provisioning.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 30),  # 1 GB free
    )

    def boom(*args: object, **kwargs: object) -> str:
        pytest.fail("a download started on a disk that cannot take it")

    monkeypatch.setattr(model_provisioning, "hf_hub_download", boom)

    with pytest.raises(ConfigurationError, match="disk space"):
        ensure_weight(EMBEDDING_WEIGHTS)


def test_enough_free_space_passes_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_provisioning.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 40),
    )

    require_disk_space(settings.models_dir, EMBEDDING_WEIGHTS.download_bytes)


def test_the_disk_error_names_both_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_provisioning.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 30),
    )

    with pytest.raises(ConfigurationError) as caught:
        require_disk_space(settings.models_dir, 200 * 1024 * 1024)

    message = caught.value.message
    assert "0.2 GB" in message
    assert "2 GB" in message
    # 1 << 30 bytes is 1.07 GB, which the message rounds to one decimal.
    assert "1.1 GB" in message
