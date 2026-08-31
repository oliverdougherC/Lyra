"""Capture privacy-safe package and process evidence for a running Lyra.app."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
FORBIDDEN_IDLE_NAMES = (
    "node",
    "docker",
    "firecrawl",
    "redis",
    "rabbitmq",
    "postgres",
    "playwright",
    "llama-server",
)


def _command(argv: list[str]) -> str:
    return subprocess.run(  # noqa: S603 - fixed system/git commands only
        argv,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _total_bytes(root: Path) -> int:
    return sum(path.lstat().st_size for path in root.rglob("*") if path.is_file())


def _memory_bytes() -> int | None:
    if platform.system() == "Darwin":
        try:
            return int(_command(["sysctl", "-n", "hw.memsize"]))
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        return None


def _process_rows() -> list[dict[str, object]]:
    output = _command(["ps", "-axo", "pid=,ppid=,etime=,rss=,%cpu=,comm="])
    rows: list[dict[str, object]] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) != 6:
            continue
        try:
            rows.append(
                {
                    "pid": int(fields[0]),
                    "ppid": int(fields[1]),
                    "elapsed_seconds": _elapsed_seconds(fields[2]),
                    "rss_bytes": int(fields[3]) * 1024,
                    "cpu_percent": float(fields[4]),
                    "executable": Path(fields[5]).name,
                }
            )
        except ValueError:
            continue
    return rows


def _elapsed_seconds(value: str) -> int:
    day_text, separator, clock = value.partition("-")
    days = int(day_text) if separator else 0
    if not separator:
        clock = day_text
    parts = [int(part) for part in clock.split(":")]
    if len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("invalid elapsed process time")
    return days * 86_400 + hours * 3_600 + minutes * 60 + seconds


def _descendants(root_pid: int, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    included = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if int(row["ppid"]) in included and int(row["pid"]) not in included:
                included.add(int(row["pid"]))
                changed = True
    root = next((row for row in rows if int(row["pid"]) == root_pid), None)
    if root is not None and platform.system() == "Darwin":
        root_elapsed = int(root.get("elapsed_seconds", -10_000))
        for row in rows:
            executable = str(row["executable"])
            if (
                int(row["ppid"]) == 1
                and executable.startswith("com.apple.WebKit.")
                and abs(int(row.get("elapsed_seconds", 10_000)) - root_elapsed) <= 2
            ):
                included.add(int(row["pid"]))
    return sorted(
        (row for row in rows if int(row["pid"]) in included),
        key=lambda row: int(row["pid"]),
    )


def _open_file_count(pid: int) -> int | None:
    try:
        output = _command(["lsof", "-nP", "-p", str(pid), "-Fn"])
    except (OSError, subprocess.SubprocessError):
        return None
    return sum(1 for line in output.splitlines() if line.startswith("f"))


def build_report(root_pid: int, package_root: Path) -> dict[str, object]:
    processes = _descendants(root_pid, _process_rows())
    if not processes or int(processes[0]["pid"]) != root_pid:
        raise ValueError("root PID is not running")
    for process in processes:
        process["open_file_count"] = _open_file_count(int(process["pid"]))
    names = [str(process["executable"]).lower() for process in processes]
    forbidden = sorted(
        {name for name in FORBIDDEN_IDLE_NAMES if any(name in executable for executable in names)}
    )
    try:
        commit = _command(["git", "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit,
        "machine": {
            "architecture": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
            "memory_bytes": _memory_bytes(),
            "cpu_count": os.cpu_count(),
        },
        "package": {
            "root_name": package_root.name,
            "size_bytes": _total_bytes(package_root),
        },
        "process_tree": processes,
        "aggregate": {
            "rss_bytes": sum(int(process["rss_bytes"]) for process in processes),
            "cpu_percent": round(sum(float(process["cpu_percent"]) for process in processes), 3),
            "process_count": len(processes),
            "forbidden_idle_processes": forbidden,
        },
        "privacy": {
            "command_arguments_collected": False,
            "file_paths_collected": False,
            "prompts_or_document_text_collected": False,
            "stable_device_identifier_collected": False,
            "unrelated_processes_retained": False,
            "webkit_attribution": "launch-time-correlated on macOS",
        },
    }


def summary(report: dict[str, object]) -> str:
    package = report["package"]
    aggregate = report["aggregate"]
    machine = report["machine"]
    if (
        not isinstance(package, dict)
        or not isinstance(aggregate, dict)
        or not isinstance(machine, dict)
    ):
        raise TypeError("runtime report sections are invalid")
    memory_gib = int(machine["memory_bytes"] or 0) / (1024**3)
    package_mib = int(package["size_bytes"]) / (1024**2)
    rss_mib = int(aggregate["rss_bytes"]) / (1024**2)
    forbidden = aggregate["forbidden_idle_processes"] or "none"
    return "\n".join(
        (
            "# Lyra desktop resource summary",
            "",
            f"- Commit: `{report['commit']}`",
            f"- Machine: {machine['architecture']}, {memory_gib:.1f} GiB RAM",
            f"- App bundle: {package_mib:.1f} MiB",
            f"- Process tree: {aggregate['process_count']} processes, {rss_mib:.1f} MiB RSS",
            f"- Aggregate sampled CPU: {aggregate['cpu_percent']}%",
            f"- Forbidden ordinary-idle processes: {forbidden}",
            "",
            "This sample is local build evidence, not the clean 8 GB physical release gate.",
            "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-pid", type=int, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.root_pid, args.package_root)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.summary_output.write_text(summary(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
