from __future__ import annotations

from pathlib import Path

import pytest

from scripts import desktop_runtime_report as report


def test_descendants_keep_only_the_owned_tree_and_correlated_webkit() -> None:
    rows = [
        {
            "pid": 10,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 1,
            "cpu_percent": 0.0,
            "executable": "lyra-desktop",
        },
        {
            "pid": 11,
            "ppid": 10,
            "elapsed_seconds": 4,
            "rss_bytes": 2,
            "cpu_percent": 0.0,
            "executable": "lyra-backend",
        },
        {
            "pid": 12,
            "ppid": 11,
            "elapsed_seconds": 4,
            "rss_bytes": 3,
            "cpu_percent": 0.0,
            "executable": "helper",
        },
        {
            "pid": 13,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 4,
            "cpu_percent": 0.0,
            "executable": "com.apple.WebKit.GPU",
        },
        {
            "pid": 14,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 5,
            "cpu_percent": 0.0,
            "executable": "com.apple.WebKit.WebContent",
        },
        {
            "pid": 99,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 6,
            "cpu_percent": 0.0,
            "executable": "other",
        },
    ]

    assert [
        row["pid"]
        for row in report._descendants(
            10, rows, sibling_evidence={13: "lsappinfo-coalition-membership"}
        )
    ] == [10, 11, 12, 13]


def test_webkit_ownership_uses_lsappinfo_responsibility_and_excludes_unrelated_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "pid": 10,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 1,
            "cpu_percent": 0.0,
            "executable": "lyra-desktop",
        },
        {
            "pid": 11,
            "ppid": 10,
            "elapsed_seconds": 4,
            "rss_bytes": 2,
            "cpu_percent": 0.0,
            "executable": "lyra-backend",
        },
        {
            "pid": 13,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 4,
            "cpu_percent": 0.0,
            "executable": "com.apple.WebKit.GPU",
        },
        {
            "pid": 14,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 5,
            "cpu_percent": 0.0,
            "executable": "com.apple.WebKit.WebContent",
        },
    ]
    snapshot = """
1) "Lyra" ASN:0x0-0x1001:
    bundleID="com.lyra.desktop"
    pid = 10 token=[sess=1 pid=10 uid:1,1,1 g:1,1 pV:1]
2) "Lyra Graphics and Media" ASN:0x0-0x1002:
    bundleID="com.apple.WebKit.GPU"
    pid = 13 token=[sess=1 pid=13 uid:1,1,1 g:1,1 pV:2]
3) "Other Web Content" ASN:0x0-0x2002:
    bundleID="com.apple.WebKit.WebContent"
    pid = 14 token=[sess=1 pid=14 uid:1,1,1 g:1,1 pV:3]
""".strip()

    monkeypatch.setattr(report, "_lsappinfo_snapshot", lambda: snapshot)

    ownership = report._webkit_ownership(10, rows)

    assert ownership["status"] == "verified"
    assert ownership["source"] == "lsappinfo-application-association"
    assert ownership["correlated_pids"] == {13: "lsappinfo-application-association"}
    assert [
        row["pid"]
        for row in report._descendants(10, rows, sibling_evidence=ownership["correlated_pids"])
    ] == [10, 11, 13]


def test_elapsed_process_time_supports_days_and_hours() -> None:
    assert report._elapsed_seconds("01:02") == 62
    assert report._elapsed_seconds("02:03:04") == 7_384
    assert report._elapsed_seconds("1-02:03:04") == 93_784


def test_build_report_marks_retained_and_unexpected_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "Lyra.app"
    package_root.mkdir()
    rows = [
        {
            "pid": 10,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 120 * 1024**2,
            "cpu_percent": 0.1,
            "executable": "lyra-desktop",
        },
        {
            "pid": 11,
            "ppid": 10,
            "elapsed_seconds": 4,
            "rss_bytes": 110 * 1024**2,
            "cpu_percent": 0.2,
            "executable": "lyra-backend",
        },
        {
            "pid": 12,
            "ppid": 11,
            "elapsed_seconds": 3,
            "rss_bytes": 7 * 1024**2,
            "cpu_percent": 0.0,
            "executable": "helper-agent",
        },
        {
            "pid": 13,
            "ppid": 1,
            "elapsed_seconds": 4,
            "rss_bytes": 60 * 1024**2,
            "cpu_percent": 0.0,
            "executable": "com.apple.WebKit.GPU",
        },
    ]
    metrics = {
        10: {
            "thread_count": 8,
            "port_count": 3,
            "private_memory_bytes": 90 * 1024**2,
            "private_memory_source": "vmmap-physical-footprint",
            "open_file_count": 40,
            "connection_count": 1,
            "listening_ports": [],
        },
        11: {
            "thread_count": 6,
            "port_count": 2,
            "private_memory_bytes": 80 * 1024**2,
            "private_memory_source": "vmmap-physical-footprint",
            "open_file_count": 30,
            "connection_count": 2,
            "listening_ports": [8000],
        },
        12: {
            "thread_count": 1,
            "port_count": 0,
            "private_memory_bytes": None,
            "private_memory_source": "unavailable",
            "open_file_count": 9,
            "connection_count": 0,
            "listening_ports": [],
        },
        13: {
            "thread_count": 5,
            "port_count": 1,
            "private_memory_bytes": 45 * 1024**2,
            "private_memory_source": "vmmap-physical-footprint",
            "open_file_count": 22,
            "connection_count": 1,
            "listening_ports": [],
        },
    }

    monkeypatch.setattr(report, "_process_rows", lambda: rows)
    monkeypatch.setattr(report, "_runtime_metrics", lambda pid: metrics[pid])
    monkeypatch.setattr(report, "_memory_bytes", lambda: 8 * 1024**3)
    monkeypatch.setattr(report, "_total_bytes", lambda _root: 140 * 1024**2)
    monkeypatch.setattr(report, "_git_commit", lambda: "abc123")
    monkeypatch.setattr(
        report,
        "_webkit_ownership",
        lambda _root_pid, _rows: {
            "source": "lsappinfo-coalition-membership",
            "status": "verified",
            "reason": "retained only WebKit siblings listed in the root app coalition",
            "correlated_pids": {13: "lsappinfo-coalition-membership"},
        },
    )

    payload = report.build_report(10, package_root)

    assert payload["schema_version"] == 2
    assert payload["selection"]["ownership_model"] == "ancestry-plus-verified-webkit-responsibility"
    assert payload["selection"]["webkit_ownership"]["status"] == "verified"
    assert payload["aggregate"]["unexpected_process_count"] == 1
    assert payload["aggregate"]["retained_process_count"] == 4
    assert payload["aggregate"]["private_memory_complete"] is False
    assert payload["aggregate"]["listening_ports"] == [8000]
    assert payload["observations"]["cold_launch"]["status"] == "preliminary"
    assert payload["observations"]["warm_launch"]["status"] == "open"
    assert payload["release_gates"]["physical_8_gib"]["status"] == "open"
    assert payload["release_gates"]["sleep_wake"]["status"] == "open"

    process_map = {item["pid"]: item for item in payload["process_tree"]}
    assert process_map[10]["role"] == "tauri-shell"
    assert process_map[10]["ownership_reason"] == "root-process"
    assert process_map[11]["role"] == "backend"
    assert process_map[12]["retained"] is True
    assert process_map[12]["unexpected"] is True
    assert process_map[12]["ownership_reason"] == "descendant-of-owned-process"
    assert process_map[12]["ownership_evidence_source"] == "process-ancestry"
    assert process_map[13]["role"] == "webkit-gpu"
    assert process_map[13]["ownership_reason"] == "lsappinfo-verified-webkit-sibling"
    assert process_map[13]["ownership_evidence_source"] == "lsappinfo-coalition-membership"

    cold_launch = payload["observations"]["cold_launch"]
    assert cold_launch["measurements"]["aggregate_rss_bytes"] == 297 * 1024**2
    assert cold_launch["helper_state"]["backend"] == "running"
    assert cold_launch["helper_state"]["embedding"] == "not-running"
    assert cold_launch["helper_state"]["rerank"] == "not-running"
    assert cold_launch["helper_state"]["ocr"] == "not-running"


def test_summary_names_preliminary_local_evidence_without_claiming_release_gate() -> None:
    payload = {
        "commit": "abc",
        "package": {"size_bytes": 1024},
        "aggregate": {
            "process_count": 2,
            "rss_bytes": 359_907_328,
            "cpu_percent": 0.0,
            "forbidden_idle_processes": [],
        },
        "machine": {"architecture": "arm64", "memory_bytes": 8 * 1024**3},
        "observations": {
            "cold_launch": {
                "status": "preliminary",
                "usable_shell": {
                    "status": "preliminary",
                    "observed": True,
                    "elapsed_seconds": 3.1,
                },
            }
        },
        "release_gates": {
            "physical_8_gib": {"status": "open"},
            "sleep_wake": {"status": "open"},
            "memory_pressure": {"status": "open"},
            "live_provider": {"status": "open"},
            "packaged_soak": {"status": "open"},
        },
    }

    text = report.summary(payload)

    assert "preliminary local build evidence" in text
    assert "343.2 MiB RSS" in text
    assert "~3.1 s usable shell" in text
    assert "still-open physical gates" in text


def test_total_bytes_never_follows_directories_outside_root(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"abc")

    assert report._total_bytes(tmp_path) == 3
