import logging
from pathlib import Path

from backend.desktop_entry import configure_packaged_logging


def test_exception_output_and_rotation_never_contain_private_values(tmp_path, monkeypatch):
    monkeypatch.setenv("LYRA_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr("backend.desktop_entry._LOG_MAX_BYTES", 700)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = configure_packaged_logging()
        logger = logging.getLogger("privacy-test")
        for _ in range(8):
            try:
                try:
                    raise ValueError("synthetic-provider-body-secret")
                except ValueError as exc:
                    raise RuntimeError(
                        str(Path.home() / "private-essay") + " token=synthetic-token"
                    ) from exc
            except RuntimeError:
                logger.exception("failed at %s", Path.home() / "private-essay")
        record = logging.LogRecord("privacy-test", logging.ERROR, __file__, 1, "cached", (), None)
        record.exc_text = "synthetic-provider-body-secret"
        logger.handle(record)
        outputs = list(tmp_path.glob("backend.log*"))
        assert len(outputs) > 1
        for item in outputs:
            text = item.read_text()
            assert "synthetic-provider-body-secret" not in text
            assert "synthetic-token" not in text
            assert str(Path.home()) not in text
            assert item.stat().st_mode & 0o777 == 0o600
        assert path.exists()
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
