"""Guards for the shared weights download: metadata, atomicity, deduplication, failure.

Everything runs without a network: `hf_hub_download` is replaced with a fake that either
lands a file in the models directory or fails. The contract under test is what a
clean-install first use depends on (PLA-402): the file lands where `settings` says it
lands, an interrupted download can never look installed, a burst of concurrent first
uses starts exactly one download, and a failure is a retryable sentence rather than a
command to run.
"""

import hashlib
import threading
import time
from dataclasses import replace
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


@pytest.fixture(autouse=True)
def _tiny_embedding_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.llm import embed_server

    spec = replace(
        model_provisioning.EMBEDDING_WEIGHTS,
        expected_bytes=len(b"GGUF fake"),
        sha256=hashlib.sha256(b"GGUF fake").hexdigest(),
    )
    monkeypatch.setattr(model_provisioning, "EMBEDDING_WEIGHTS", spec)
    monkeypatch.setattr(embed_server, "EMBEDDING_WEIGHTS", spec)
    monkeypatch.setitem(globals(), "EMBEDDING_WEIGHTS", spec)


@pytest.mark.parametrize("content", [b"", b"truncated", b"wrong model"])
def test_unverified_existing_embedding_requires_repair_without_losing_it_offline(
    content: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(content)
    attempts: list[str] = []

    def offline(spec, path, progress):
        attempts.append(spec.filename)
        raise ConfigurationError("Offline. Check your connection and try again.")

    monkeypatch.setattr(model_provisioning, "_download", offline)
    with pytest.raises(ConfigurationError, match="try again"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert attempts == [EMBEDDING_WEIGHTS.filename]
    assert target.read_bytes() == content


def test_symlinked_weights_are_rejected_without_following_target(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "other-model"
    outside.write_bytes(b"do not touch")
    EMBEDDING_WEIGHTS.path.symlink_to(outside)
    monkeypatch.setattr(model_provisioning, "_download", lambda *args: pytest.fail("download"))
    with pytest.raises(ConfigurationError):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert outside.read_bytes() == b"do not touch"


def _land_download(
    *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
) -> str:
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
    target.write_bytes(b"GGUF fake")
    monkeypatch.setattr(
        model_provisioning,
        "_fetch_weight",
        lambda *args, **kwargs: pytest.fail("an existing file was downloaded again"),
    )

    assert ensure_weight(EMBEDDING_WEIGHTS) == target


def test_a_missing_file_is_downloaded_to_the_models_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, object, str | None]] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        calls.append((repo_id, filename, local_dir, revision))
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

    assert not EMBEDDING_WEIGHTS.path.exists()
    assert ensure_weight(EMBEDDING_WEIGHTS) == EMBEDDING_WEIGHTS.path

    assert len(calls) == 1
    repo, filename, stage, revision = calls[0]
    assert repo == EMBEDDING_WEIGHTS.repo_id
    assert filename == EMBEDDING_WEIGHTS.filename
    assert stage.parent == settings.models_dir
    assert stage != settings.models_dir
    assert revision == EMBEDDING_REVISION
    assert EMBEDDING_WEIGHTS.path.exists()


def test_concurrent_first_uses_share_one_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of first-use requests must start one download, not one per thread."""
    calls: list[str] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        calls.append(filename)
        time.sleep(0.1)  # long enough that every other thread is waiting on it
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

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
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

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


def test_a_waiter_shares_failed_leader_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent cohort must not amplify an outage into serial attempts."""
    hold_first_attempt = threading.Event()
    first_attempt_started = threading.Event()
    attempts = 0

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
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

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

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
    time.sleep(0.05)  # allow the waiter to join the held attempt
    hold_first_attempt.set()  # the leader fails
    leader_thread.join()
    waiter_thread.join()

    assert len(leader_errors) == 1
    assert isinstance(leader_errors[0], ConfigurationError)
    assert len(waiter_errors) == 1
    assert isinstance(waiter_errors[0], ConfigurationError)
    assert not waiter_results
    assert attempts == 1
    assert ensure_weight(EMBEDDING_WEIGHTS) == EMBEDDING_WEIGHTS.path
    assert attempts == 2


def test_after_a_failure_the_next_caller_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        attempts.append(filename)
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

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
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        download_started.set()
        release.wait()
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

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
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

    assert ensure_weight(EMBEDDING_WEIGHTS) == EMBEDDING_WEIGHTS.path
    assert EMBEDDING_WEIGHTS.path.exists()


def test_progress_reports_the_download_line(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []

    def fake_download(
        *, repo_id: str, filename: str, local_dir: object, revision: str | None, max_bytes: int = 0
    ) -> str:
        return _land_download(
            repo_id=repo_id, filename=filename, local_dir=local_dir, revision=revision
        )

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fake_download)

    ensure_weight(EMBEDDING_WEIGHTS, progress=lines.append)

    assert lines == [model_provisioning.download_description(EMBEDDING_WEIGHTS)]


def test_not_enough_free_space_refuses_before_any_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_provisioning.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 30),  # 1 GB free
    )

    def boom(*args: object, **kwargs: object) -> str:
        pytest.fail("a download started on a disk that cannot take it")

    monkeypatch.setattr(model_provisioning, "_fetch_weight", boom)

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


def test_release_manifest_identity_is_pinned() -> None:
    # The fixture swaps runtime constants, so inspect the dataclass default captured
    # by the disclosure helper before test substitution.
    production = model_provisioning.download_description.__defaults__[0]
    assert production.expected_bytes == 146_146_432
    assert production.sha256 == "3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7"
    assert "146 MB" in model_provisioning.download_description(production)


def test_wrong_same_size_identity_is_repaired_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(b"GGUF evil")

    def fetch(**kwargs):
        assert target.read_bytes() == b"GGUF evil"
        return _land_download(**kwargs)

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fetch)
    assert ensure_weight(EMBEDDING_WEIGHTS) == target
    assert target.read_bytes() == b"GGUF fake"
    assert not list(settings.models_dir.glob(".lyra-model-*"))


def test_corrupt_download_cannot_replace_old_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(b"old damaged cache")

    def fetch(**kwargs):
        candidate = kwargs["local_dir"] / kwargs["filename"]
        candidate.write_bytes(b"GGUF evil")
        return str(candidate)

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fetch)
    with pytest.raises(ConfigurationError, match="verification"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert target.read_bytes() == b"old damaged cache"
    assert not list(settings.models_dir.glob(".lyra-model-*"))


def test_interrupted_download_cannot_publish_partial_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(**kwargs):
        (kwargs["local_dir"] / kwargs["filename"]).write_bytes(b"partial")
        raise httpx.ReadTimeout("interrupted")

    monkeypatch.setattr(model_provisioning, "_fetch_weight", fetch)
    with pytest.raises(ConfigurationError, match="try again"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert not EMBEDDING_WEIGHTS.path.exists()
    assert not list(settings.models_dir.glob(".lyra-model-*"))
    monkeypatch.setattr(model_provisioning, "_fetch_weight", _land_download)
    assert ensure_weight(EMBEDDING_WEIGHTS) == EMBEDDING_WEIGHTS.path


def test_cache_verification_is_reused_and_invalidated_after_same_size_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(b"GGUF fake")
    sha256 = model_provisioning.hashlib.sha256
    calls = []

    def digest():
        calls.append(True)
        return sha256()

    monkeypatch.setattr(model_provisioning.hashlib, "sha256", digest)
    assert model_provisioning.verified_weight(EMBEDDING_WEIGHTS)
    assert model_provisioning.verified_weight(EMBEDDING_WEIGHTS)
    assert len(calls) == 1
    previous = target.stat()
    target.write_bytes(b"GGUF evil")
    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert not model_provisioning.verified_weight(EMBEDDING_WEIGHTS)
    assert len(calls) == 2


def test_restart_revalidates_previously_published_file(monkeypatch: pytest.MonkeyPatch) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(b"GGUF fake")
    assert model_provisioning.verified_weight(EMBEDDING_WEIGHTS)
    model_provisioning._VERIFIED.clear()
    monkeypatch.setattr(
        model_provisioning, "_fetch_weight", lambda **kwargs: pytest.fail("network")
    )
    assert ensure_weight(EMBEDDING_WEIGHTS) == target


def test_directory_target_is_rejected_without_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.mkdir()
    marker = target / "keep"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(
        model_provisioning, "_fetch_weight", lambda **kwargs: pytest.fail("network")
    )
    with pytest.raises(ConfigurationError, match="regular file"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert marker.read_bytes() == b"keep"


def test_replacement_failure_preserves_existing_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    target = EMBEDDING_WEIGHTS.path
    target.write_bytes(b"old damaged cache")
    monkeypatch.setattr(model_provisioning, "_fetch_weight", _land_download)

    def denied(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(model_provisioning.os, "replace", denied)
    with pytest.raises(ConfigurationError, match="permissions"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert target.read_bytes() == b"old damaged cache"


def test_embedding_installed_check_reaches_corrupt_cache_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.llm.embed_server import EmbeddingServer

    EMBEDDING_WEIGHTS.path.write_bytes(b"GGUF evil")
    monkeypatch.setattr(model_provisioning, "_fetch_weight", _land_download)
    EmbeddingServer()._ensure_weights()
    assert EMBEDDING_WEIGHTS.path.read_bytes() == b"GGUF fake"
    assert not RERANK_WEIGHTS.path.exists()
    assert not settings.ocr_model_path.exists()


def test_transport_enforces_size_limit_before_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(
            200,
            stream=httpx.ByteStream(b"too many bytes"),
            request=httpx.Request("GET", "https://huggingface.co"),
        )

    monkeypatch.setattr(model_provisioning.httpx, "stream", stream)
    with pytest.raises(ConfigurationError, match="try again"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert not EMBEDDING_WEIGHTS.path.exists()


def test_transport_enforces_wall_clock_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(
            200,
            stream=httpx.ByteStream(b"GGUF fake"),
            request=httpx.Request("GET", "https://huggingface.co"),
        )

    clock = iter([0.0, model_provisioning.DOWNLOAD_DEADLINE_SECONDS + 1])
    monkeypatch.setattr(model_provisioning.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(model_provisioning.httpx, "stream", stream)
    with pytest.raises(ConfigurationError, match="try again"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert not EMBEDDING_WEIGHTS.path.exists()


def test_waiter_timeout_is_bounded_without_starting_second_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = model_provisioning._DownloadInProgress()
    waits = []
    monkeypatch.setattr(entry.finished, "wait", lambda timeout: waits.append(timeout) or False)
    key = str(EMBEDDING_WEIGHTS.path.absolute())
    monkeypatch.setitem(model_provisioning._IN_FLIGHT, key, entry)
    with pytest.raises(ConfigurationError, match="timed out"):
        ensure_weight(EMBEDDING_WEIGHTS)
    assert waits == [615.0]


def test_setup_disclosure_derives_size_from_manifest() -> None:
    text = model_provisioning.setup_disclosure()
    assert "Hugging Face" in text
    assert "does not upload course documents" in text
    assert "Optional OCR and reranking weights are not downloaded automatically" in text
    assert "0 MB" in text  # tiny fixture, not a hardcoded product size
