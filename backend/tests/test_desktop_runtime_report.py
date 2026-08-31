from __future__ import annotations

from pathlib import Path

from scripts import desktop_runtime_report as report


def test_descendants_keep_only_the_owned_tree() -> None:
    rows = [
        {"pid": 10, "ppid": 1, "rss_bytes": 1, "cpu_percent": 0.0, "executable": "Lyra"},
        {"pid": 11, "ppid": 10, "rss_bytes": 2, "cpu_percent": 0.0, "executable": "backend"},
        {"pid": 12, "ppid": 11, "rss_bytes": 3, "cpu_percent": 0.0, "executable": "helper"},
        {"pid": 99, "ppid": 1, "rss_bytes": 4, "cpu_percent": 0.0, "executable": "other"},
    ]

    assert [row["pid"] for row in report._descendants(10, rows)] == [10, 11, 12]


def test_elapsed_process_time_supports_days_and_hours() -> None:
    assert report._elapsed_seconds("01:02") == 62
    assert report._elapsed_seconds("02:03:04") == 7_384
    assert report._elapsed_seconds("1-02:03:04") == 93_784


def test_summary_names_local_evidence_without_claiming_release_gate() -> None:
    payload = {
        "commit": "abc",
        "package": {"size_bytes": 1024},
        "aggregate": {
            "process_count": 2,
            "rss_bytes": 2048,
            "cpu_percent": 0.0,
            "forbidden_idle_processes": [],
        },
        "machine": {"architecture": "arm64", "memory_bytes": 8 * 1024**3},
    }

    text = report.summary(payload)

    assert "local build evidence" in text
    assert "not the clean 8 GB" in text


def test_total_bytes_never_follows_directories_outside_root(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"abc")

    assert report._total_bytes(tmp_path) == 3
