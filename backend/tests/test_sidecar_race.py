"""Race-safe WAL sidecar hardening regressions (PLA-317).

SQLite's -shm and -wal files are transient: a legitimate connection may remove them at
any moment. The create/open/harden path must tolerate this disappearance without leaking
an unhandled FileNotFoundError as a request 500, and without weakening the privacy
contract (O_NOFOLLOW, exclusive safe creation, regular-file validation, 0o600, symlink
refusal).
"""

import errno
import os
import stat
import threading
from pathlib import Path

import pytest

from backend.storage import private
from backend.storage.database import connect, migrate

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# Part 3: deterministic regression for the exact EEXIST -> ENOENT race
# ---------------------------------------------------------------------------


class TestEnsurePrivateFileRace:
    """Deterministic interleaving tests for the sidecar disappearance race."""

    def test_recovers_from_eexist_then_enoent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact race captured in PR #55: EEXIST -> ENOENT -> success on retry."""
        sidecar = tmp_path / "lyra.db-shm"
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                if call_index == 2:
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        private.ensure_private_file(sidecar)

        assert call_index == 3
        assert sidecar.is_file()
        assert not sidecar.is_symlink()
        assert _mode(sidecar) == 0o600

    def test_recovers_after_two_disappearance_cycles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple EEXIST -> ENOENT cycles before the final create succeeds."""
        sidecar = tmp_path / "lyra.db-wal"
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index in (1, 3):
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                if call_index in (2, 4):
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        private.ensure_private_file(sidecar)

        assert call_index == 5
        assert sidecar.is_file()
        assert _mode(sidecar) == 0o600

    def test_fallback_open_succeeds_on_existing_regular_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After EEXIST, the fallback open finds the real file and hardens it."""
        sidecar = tmp_path / "lyra.db-shm"
        sidecar.write_bytes(b"")
        os.chmod(sidecar, 0o644)
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        private.ensure_private_file(sidecar)

        assert call_index == 2
        assert _mode(sidecar) == 0o600


# ---------------------------------------------------------------------------
# Part 4: security invariants preserved across retries
# ---------------------------------------------------------------------------


class TestEnsurePrivateFileSecurity:
    """Privacy contract holds during and after retry attempts."""

    def test_symlink_planted_after_disappearance_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An attacker swaps in a symlink between retry attempts."""
        sidecar = tmp_path / "lyra.db-shm"
        external = tmp_path / "external-target"
        external.write_bytes(b"attacker controlled")
        os.chmod(external, 0o644)
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                if call_index == 2:
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
                if call_index == 3:
                    sidecar.symlink_to(external)
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        with pytest.raises(private.PrivacyContractError):
            private.ensure_private_file(sidecar)

        assert external.read_bytes() == b"attacker controlled"
        assert _mode(external) == 0o644

    def test_non_regular_entry_refused_after_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FIFO planted during a retry is rejected by the regular-file check."""
        sidecar = tmp_path / "lyra.db-shm"
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                if call_index == 2:
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
                if call_index == 3:
                    os.mkfifo(sidecar)
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        with pytest.raises(private.PrivacyContractError, match="not a regular file"):
            private.ensure_private_file(sidecar)

    def test_permission_error_propagates_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EACCES on the fallback open propagates immediately, not retried."""
        sidecar = tmp_path / "lyra.db-shm"
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                if call_index == 2:
                    raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        with pytest.raises(private.PrivacyContractError, match="private descriptor"):
            private.ensure_private_file(sidecar)

        assert call_index == 2

    def test_eloop_on_fallback_propagates_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ELOOP (symlink) on the fallback open is a contract violation, not a retry."""
        sidecar = tmp_path / "lyra.db-shm"
        real_open = os.open
        call_index = 0

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                if call_index == 2:
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(path))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", racing_open)

        with pytest.raises(private.PrivacyContractError, match="symlink"):
            private.ensure_private_file(sidecar)

        assert call_index == 2

    def test_retry_count_is_bounded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """After the bounded retry count, FileNotFoundError propagates."""
        sidecar = tmp_path / "lyra.db-shm"
        real_open = os.open
        call_index = 0

        def always_racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal call_index
            if str(path) == str(sidecar):
                call_index += 1
                if call_index % 2 == 1:
                    raise FileExistsError(errno.EEXIST, "File exists", str(path))
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", always_racing_open)

        with pytest.raises(FileNotFoundError):
            private.ensure_private_file(sidecar)

        expected_calls = (private._SIDECAR_RACE_RETRIES + 1) * 2
        assert call_index == expected_calls

    def test_eexist_on_exclusive_create_without_disappearance_still_works(
        self, tmp_path: Path
    ) -> None:
        """A real existing file (no disappearance) is hardened normally."""
        sidecar = tmp_path / "lyra.db-shm"
        sidecar.write_bytes(b"")
        os.chmod(sidecar, 0o644)

        private.ensure_private_file(sidecar)

        assert _mode(sidecar) == 0o600


# ---------------------------------------------------------------------------
# Part 2: harden_file_if_present — TOCTOU-safe post-open hardening
# ---------------------------------------------------------------------------


class TestHardenFileIfPresent:
    """The post-open hardening helper tolerates transient sidecar absence."""

    def test_hardens_existing_regular_file(self, tmp_path: Path) -> None:
        target = tmp_path / "lyra.db-wal"
        target.write_bytes(b"")
        os.chmod(target, 0o644)

        private.harden_file_if_present(target)

        assert _mode(target) == 0o600

    def test_tolerates_absent_file(self, tmp_path: Path) -> None:
        absent = tmp_path / "lyra.db-shm"

        private.harden_file_if_present(absent)

        assert not absent.exists()

    def test_refuses_symlink(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        external.write_bytes(b"data")
        os.chmod(external, 0o644)
        link = tmp_path / "lyra.db-shm"
        link.symlink_to(external)

        with pytest.raises(private.PrivacyContractError, match="symlink"):
            private.harden_file_if_present(link)

        assert _mode(external) == 0o644

    def test_refuses_non_regular_entry(self, tmp_path: Path) -> None:
        fifo = tmp_path / "lyra.db-wal"
        os.mkfifo(fifo)

        with pytest.raises(private.PrivacyContractError, match="not a regular file"):
            private.harden_file_if_present(fifo)


# ---------------------------------------------------------------------------
# Part 5: concurrent SQLite WAL stress regression
# ---------------------------------------------------------------------------


class TestConcurrentWALStress:
    """Concurrent connect()/close() under real WAL sidecar churn."""

    def test_concurrent_connections_no_race_exceptions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "stress.db"
        init_conn = connect(db_path)
        migrate(init_conn)
        init_conn.close()

        worker_count = 8
        iterations = 20
        errors: list[tuple[int, Exception]] = []
        barrier = threading.Barrier(worker_count, timeout=10)

        def worker(worker_id: int) -> None:
            try:
                barrier.wait()
                for _ in range(iterations):
                    conn = connect(db_path)
                    try:
                        conn.execute("select 1")
                    finally:
                        conn.close()
            except Exception as exc:
                errors.append((worker_id, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"race exceptions: {errors}"

        final = connect(db_path)
        try:
            assert final.execute("pragma integrity_check").fetchone()[0] == "ok"
        finally:
            final.close()

        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                assert _mode(sidecar) == 0o600

    def test_concurrent_connections_all_execute_queries(self, tmp_path: Path) -> None:
        db_path = tmp_path / "query-stress.db"
        init_conn = connect(db_path)
        migrate(init_conn)
        init_conn.close()

        worker_count = 6
        iterations = 15
        results: list[int] = []
        lock = threading.Lock()
        errors: list[tuple[int, Exception]] = []
        barrier = threading.Barrier(worker_count, timeout=10)

        def worker(worker_id: int) -> None:
            try:
                barrier.wait()
                for _i in range(iterations):
                    conn = connect(db_path)
                    try:
                        row = conn.execute("select 1 + 1").fetchone()
                        with lock:
                            results.append(row[0])
                    finally:
                        conn.close()
            except Exception as exc:
                errors.append((worker_id, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"race exceptions: {errors}"
        assert len(results) == worker_count * iterations
        assert all(r == 2 for r in results)


# ---------------------------------------------------------------------------
# Part 6: per-request connection pattern (acceptance relevance)
# ---------------------------------------------------------------------------


class TestPerRequestConnectionPattern:
    """The production per-request connect/use/close pattern under sidecar churn."""

    def test_sequential_per_request_connections_survive_wal_sidecar_churn(
        self, tmp_path: Path
    ) -> None:
        """Simulates the GET /api/classes/.../agent/activity production pattern."""
        db_path = tmp_path / "per-request.db"
        init_conn = connect(db_path)
        migrate(init_conn)
        init_conn.close()

        for _ in range(50):
            conn = connect(db_path)
            try:
                conn.execute("select count(*) from classes")
            finally:
                conn.close()

    def test_interleaved_per_request_connections_survive_sidecar_churn(
        self, tmp_path: Path
    ) -> None:
        """Multiple overlapping per-request connections with real WAL activity."""
        db_path = tmp_path / "interleaved.db"
        init_conn = connect(db_path)
        migrate(init_conn)
        init_conn.close()

        worker_count = 4
        iterations = 25
        errors: list[tuple[int, Exception]] = []
        barrier = threading.Barrier(worker_count, timeout=10)

        def worker(worker_id: int) -> None:
            try:
                barrier.wait()
                for i in range(iterations):
                    conn = connect(db_path)
                    try:
                        conn.execute(
                            "insert into classes (name, code) values (?, ?)",
                            (f"w{worker_id}-c{i}", f"W{worker_id}C{i}"),
                        )
                        conn.commit()
                    finally:
                        conn.close()
            except Exception as exc:
                errors.append((worker_id, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"race exceptions: {errors}"

        final = connect(db_path)
        try:
            count = final.execute("select count(*) from classes").fetchone()[0]
            assert count == worker_count * iterations
            assert final.execute("pragma integrity_check").fetchone()[0] == "ok"
        finally:
            final.close()
