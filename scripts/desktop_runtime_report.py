"""Capture privacy-safe package and process evidence for a running Lyra.app."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2
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
_BYTE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[KMGTP]?)(?:i?B?)?$")
_LISTEN_PORT_PATTERN = re.compile(r":(?P<port>\d+)\s+\(LISTEN\)$")
_LSAPPINFO_HEADER_PATTERN = re.compile(r'^\s*(?:\d+\)\s+)?"(?P<name>.+?)"\s+(?P<asn>ASN:[^ ]+:)')
_LSAPPINFO_PID_PATTERN = re.compile(r"\bpid\s*=\s*(?P<pid>\d+)\b")
_LSAPPINFO_BUNDLE_PATTERN = re.compile(r'^\s*bundleID="(?P<bundle_id>[^"]+)"')
_LSAPPINFO_COALITION_PATTERN = re.compile(
    r"^\s*coalition:\s*(?P<coalition_id>\d+)(?:\s+\{\s*(?P<members>[^}]*)\})?"
)
_GATE_SUMMARY_LABELS = {
    "physical_8_gib": "clean 8 GiB Mac",
    "sleep_wake": "sleep/wake",
    "memory_pressure": "memory pressure",
    "live_provider": "live provider",
    "packaged_soak": "packaged soak",
}


def _command(argv: list[str]) -> str:
    return subprocess.run(  # noqa: S603 - fixed system/git commands only
        argv,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _optional_command(argv: list[str], *, allowed_returncodes: tuple[int, ...] = (0, 1)) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed system commands only
        argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in allowed_returncodes:
        raise subprocess.SubprocessError(f"command exited {completed.returncode}: {' '.join(argv)}")
    return completed.stdout.strip()


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


def _git_commit() -> str:
    try:
        return _command(["git", "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _process_rows() -> list[dict[str, object]]:
    output = _command(["ps", "-axo", "pid=,ppid=,etime=,rss=,%cpu=,comm="])
    rows: list[dict[str, object]] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) != 6:
            continue
        try:
            executable_path = fields[5]
            rows.append(
                {
                    "pid": int(fields[0]),
                    "ppid": int(fields[1]),
                    "elapsed_seconds": _elapsed_seconds(fields[2]),
                    "rss_bytes": int(fields[3]) * 1024,
                    "cpu_percent": float(fields[4]),
                    "executable": Path(executable_path).name,
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


def _lsappinfo_snapshot() -> str:
    return _command(["lsappinfo", "list"])


def _parse_lsappinfo_records(output: str) -> dict[int, dict[str, object]]:
    records: dict[int, dict[str, object]] = {}
    current: dict[str, object] | None = None
    for raw_line in output.splitlines():
        header_match = _LSAPPINFO_HEADER_PATTERN.match(raw_line)
        if header_match is not None:
            current = {
                "name": header_match.group("name"),
                "asn": header_match.group("asn"),
                "bundle_id": None,
                "coalition_id": None,
                "coalition_pids": None,
            }
            continue
        if current is None:
            continue
        bundle_match = _LSAPPINFO_BUNDLE_PATTERN.match(raw_line)
        if bundle_match is not None:
            current["bundle_id"] = bundle_match.group("bundle_id")
            continue
        pid_match = _LSAPPINFO_PID_PATTERN.search(raw_line)
        if pid_match is not None:
            current["pid"] = int(pid_match.group("pid"))
            records[int(current["pid"])] = current
            continue
        coalition_match = _LSAPPINFO_COALITION_PATTERN.match(raw_line)
        if coalition_match is not None:
            members_text = coalition_match.group("members")
            coalition_pids = (
                {int(member) for member in members_text.split()}
                if members_text is not None
                else None
            )
            current["coalition_id"] = int(coalition_match.group("coalition_id"))
            current["coalition_pids"] = coalition_pids
    return records


def _webkit_ownership(root_pid: int, rows: list[dict[str, object]]) -> dict[str, object]:
    unavailable = {
        "source": "unavailable",
        "status": "unavailable",
        "reason": "no verified macOS app-level ownership evidence was available",
        "correlated_pids": {},
    }
    if platform.system() != "Darwin":
        return {
            **unavailable,
            "reason": "verified WebKit sibling ownership is only implemented on macOS",
        }
    try:
        records = _parse_lsappinfo_records(_lsappinfo_snapshot())
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            **unavailable,
            "reason": "lsappinfo evidence was unavailable",
        }
    root_record = records.get(root_pid)
    if root_record is None:
        return {
            **unavailable,
            "reason": "root process is not tracked by lsappinfo",
        }
    coalition_pids = root_record.get("coalition_pids")
    if isinstance(coalition_pids, set) and root_pid in coalition_pids:
        correlated_pids = {
            int(row["pid"]): "lsappinfo-coalition-membership"
            for row in rows
            if int(row["pid"]) in coalition_pids
            and int(row["ppid"]) == 1
            and str(row["executable"]).startswith("com.apple.WebKit.")
        }
        return {
            "source": "lsappinfo-coalition-membership",
            "status": "verified",
            "reason": "retained only WebKit siblings listed in the root app coalition",
            "correlated_pids": correlated_pids,
        }

    root_name = str(root_record.get("name") or "").strip()
    expected_children = {
        f"{root_name} Networking": "com.apple.WebKit.Networking",
        f"{root_name} Graphics and Media": "com.apple.WebKit.GPU",
        f"{root_name} Web Content": "com.apple.WebKit.WebContent",
    }
    row_pids = {
        int(row["pid"])
        for row in rows
        if int(row["ppid"]) == 1 and str(row["executable"]).startswith("com.apple.WebKit.")
    }
    correlated_pids = {
        pid: "lsappinfo-application-association"
        for pid, record in records.items()
        if pid in row_pids
        and expected_children.get(str(record.get("name") or ""))
        == str(record.get("bundle_id") or "")
    }
    if not root_name or not correlated_pids:
        return {
            **unavailable,
            "reason": "lsappinfo did not expose WebKit application responsibility",
        }
    return {
        "source": "lsappinfo-application-association",
        "status": "verified",
        "reason": "retained only WebKit services assigned to the root app by LaunchServices",
        "correlated_pids": correlated_pids,
    }


def _descendants(
    root_pid: int,
    rows: list[dict[str, object]],
    *,
    sibling_evidence: Mapping[int, str] | None = None,
) -> list[dict[str, object]]:
    included = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if int(row["ppid"]) in included and int(row["pid"]) not in included:
                included.add(int(row["pid"]))
                changed = True
    if sibling_evidence is not None:
        included.update(int(pid) for pid in sibling_evidence)
    return sorted(
        (row for row in rows if int(row["pid"]) in included),
        key=lambda row: int(row["pid"]),
    )


def _role_for_process(executable: str) -> tuple[str, bool]:
    lowered = executable.lower()
    if lowered == "lyra-desktop":
        return "tauri-shell", True
    if lowered == "lyra-backend":
        return "backend", True
    if lowered == "com.apple.webkit.gpu":
        return "webkit-gpu", True
    if lowered == "com.apple.webkit.webcontent":
        return "webkit-webcontent", True
    if lowered == "com.apple.webkit.networking":
        return "webkit-networking", True
    if "embed" in lowered:
        return "embedding-helper", True
    if "rerank" in lowered:
        return "rerank-helper", True
    if "ocr" in lowered:
        return "ocr-helper", True
    if "llama-server" in lowered:
        return "model-helper", True
    return "owned-helper", False


def _parse_byte_text(value: str) -> int | None:
    text = value.strip()
    if not text or text.upper() == "N/A":
        return None
    match = _BYTE_PATTERN.fullmatch(text)
    if match is None:
        return None
    amount = float(match.group("value"))
    unit = match.group("unit").upper()
    scale = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
    }[unit]
    return int(amount * scale)


def _top_metrics(pid: int) -> dict[str, object]:
    try:
        output = _command(
            [
                "top",
                "-l",
                "1",
                "-pid",
                str(pid),
                "-stats",
                "pid,command,mem,rprvt,ports,threads,cpu",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "thread_count": None,
            "port_count": None,
            "private_memory_bytes": None,
            "private_memory_source": "unavailable",
        }
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[0] != str(pid):
            continue
        return {
            "thread_count": int(fields[5]),
            "port_count": int(fields[4]),
            "private_memory_bytes": _parse_byte_text(fields[3]),
            "private_memory_source": "top-rprvt" if fields[3] != "N/A" else "unavailable",
        }
    return {
        "thread_count": None,
        "port_count": None,
        "private_memory_bytes": None,
        "private_memory_source": "unavailable",
    }


def _vmmap_physical_footprint(pid: int) -> int | None:
    if platform.system() != "Darwin":
        return None
    try:
        output = _command(["vmmap", "-summary", str(pid)])
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        if not line.startswith("Physical footprint:"):
            continue
        _, _, value = line.partition(":")
        return _parse_byte_text(value.strip().split()[0])
    return None


def _open_file_count(pid: int) -> int | None:
    try:
        output = _command(["lsof", "-nP", "-p", str(pid), "-Fn"])
    except (OSError, subprocess.SubprocessError):
        return None
    return sum(1 for line in output.splitlines() if line.startswith("f"))


def _network_metrics(pid: int) -> dict[str, object]:
    try:
        output = _optional_command(["lsof", "-a", "-nP", "-p", str(pid), "-i"])
    except (OSError, subprocess.SubprocessError):
        return {"connection_count": None, "listening_ports": []}
    if not output:
        return {"connection_count": 0, "listening_ports": []}
    listening_ports: set[int] = set()
    connection_count = 0
    for line in output.splitlines()[1:]:
        if not line.strip():
            continue
        connection_count += 1
        match = _LISTEN_PORT_PATTERN.search(line)
        if match is not None:
            listening_ports.add(int(match.group("port")))
    return {
        "connection_count": connection_count,
        "listening_ports": sorted(listening_ports),
    }


def _runtime_metrics(pid: int) -> dict[str, object]:
    top_metrics = _top_metrics(pid)
    private_memory_bytes = top_metrics.get("private_memory_bytes")
    private_memory_source = str(top_metrics.get("private_memory_source", "unavailable"))
    if private_memory_bytes is None:
        vmmap_bytes = _vmmap_physical_footprint(pid)
        if vmmap_bytes is not None:
            private_memory_bytes = vmmap_bytes
            private_memory_source = "vmmap-physical-footprint"
    network_metrics = _network_metrics(pid)
    return {
        "thread_count": top_metrics.get("thread_count"),
        "port_count": top_metrics.get("port_count"),
        "private_memory_bytes": private_memory_bytes,
        "private_memory_source": private_memory_source,
        "open_file_count": _open_file_count(pid),
        "connection_count": network_metrics["connection_count"],
        "listening_ports": network_metrics["listening_ports"],
    }


def _ownership_reason(
    row: dict[str, object],
    *,
    root_pid: int,
    included_pids: set[int],
    sibling_evidence: Mapping[int, str] | None,
) -> str:
    pid = int(row["pid"])
    ppid = int(row["ppid"])
    if pid == root_pid:
        return "root-process"
    if ppid in included_pids:
        return "descendant-of-owned-process"
    if sibling_evidence is not None and pid in sibling_evidence:
        return "lsappinfo-verified-webkit-sibling"
    return "included-by-fallback"


def _ownership_evidence_source(
    row: dict[str, object], *, root_pid: int, sibling_evidence: Mapping[int, str] | None
) -> str:
    pid = int(row["pid"])
    if pid == root_pid:
        return "root-pid"
    if sibling_evidence is not None and pid in sibling_evidence:
        return str(sibling_evidence[pid])
    return "process-ancestry"


def _helper_state(processes: list[dict[str, object]]) -> dict[str, str]:
    roles = {str(process["role"]) for process in processes}
    return {
        "backend": "running" if "backend" in roles else "not-running",
        "embedding": "running"
        if "embedding-helper" in roles or "model-helper" in roles
        else "not-running",
        "rerank": "running" if "rerank-helper" in roles else "not-running",
        "ocr": "running" if "ocr-helper" in roles else "not-running",
    }


def _open_observation(title: str) -> dict[str, object]:
    return {
        "status": "open",
        "title": title,
        "reason": (
            "requires physical packaged-app evidence not collected by this deterministic sample"
        ),
    }


def build_report(
    root_pid: int,
    package_root: Path,
    *,
    usable_shell_seconds: float | None = None,
    observation: str = "cold_launch",
) -> dict[str, object]:
    rows = _process_rows()
    webkit_ownership = _webkit_ownership(root_pid, rows)
    sibling_evidence = webkit_ownership.get("correlated_pids")
    if not isinstance(sibling_evidence, Mapping):
        sibling_evidence = {}
    processes = _descendants(root_pid, rows, sibling_evidence=sibling_evidence)
    if not processes or int(processes[0]["pid"]) != root_pid:
        raise ValueError("root PID is not running")

    included_pids = {int(process["pid"]) for process in processes}
    enriched: list[dict[str, object]] = []
    for process in processes:
        role, expected = _role_for_process(str(process["executable"]))
        metrics = _runtime_metrics(int(process["pid"]))
        enriched_process = {
            **process,
            **metrics,
            "role": role,
            "retained": True,
            "unexpected": not expected,
            "ownership_reason": _ownership_reason(
                process,
                root_pid=root_pid,
                included_pids=included_pids,
                sibling_evidence=sibling_evidence,
            ),
            "ownership_evidence_source": _ownership_evidence_source(
                process, root_pid=root_pid, sibling_evidence=sibling_evidence
            ),
        }
        enriched.append(enriched_process)

    names = [str(process["executable"]).lower() for process in enriched]
    forbidden = sorted(
        {name for name in FORBIDDEN_IDLE_NAMES if any(name in executable for executable in names)}
    )
    private_values = [
        int(process["private_memory_bytes"])
        for process in enriched
        if process.get("private_memory_bytes") is not None
    ]
    connection_counts = [
        int(process["connection_count"])
        for process in enriched
        if process.get("connection_count") is not None
    ]
    thread_counts = [
        int(process["thread_count"])
        for process in enriched
        if process.get("thread_count") is not None
    ]
    open_file_counts = [
        int(process["open_file_count"])
        for process in enriched
        if process.get("open_file_count") is not None
    ]
    port_counts = [
        int(process["port_count"]) for process in enriched if process.get("port_count") is not None
    ]
    listening_ports = sorted(
        {
            port
            for process in enriched
            for port in process.get("listening_ports", [])
            if isinstance(port, int)
        }
    )
    package_size_bytes = _total_bytes(package_root)
    observation_rss = sum(int(process["rss_bytes"]) for process in enriched)
    helper_state = _helper_state(enriched)
    sample_titles = {
        "cold_launch": "Preliminary cold-launch sample",
        "warm_launch": "Preliminary warm-launch sample",
        "idle_60s": "Preliminary 60-second settled-idle sample",
        "post_task": "Preliminary post-task sample",
        "post_eviction": "Preliminary post-eviction sample",
    }
    if observation not in sample_titles:
        raise ValueError("unsupported runtime observation")
    sample_observation = {
        "status": "preliminary",
        "title": sample_titles[observation],
        "evidence_scope": "deterministic local build evidence",
        "usable_shell": {
            "status": "preliminary" if usable_shell_seconds is not None else "open",
            "observed": usable_shell_seconds is not None,
            "elapsed_seconds": usable_shell_seconds,
        },
        "measurements": {
            "aggregate_rss_bytes": observation_rss,
            "aggregate_private_memory_bytes": sum(private_values) if private_values else None,
            "package_size_bytes": package_size_bytes,
            "cache_size_bytes": None,
            "cache_growth_bytes": None,
        },
        "helper_state": helper_state,
        "notes": [
            "This sample is preliminary and local to the development machine.",
            (
                "Cache growth, post-task drift, idle-eviction, and quit cleanup remain open "
                "unless captured separately."
            ),
        ],
    }
    observations = {
        "cold_launch": _open_observation("Cold-launch sample"),
        "warm_launch": _open_observation("Warm relaunch sample"),
        "idle_60s": _open_observation("60-second ordinary-idle sample"),
        "post_task": _open_observation("Post-task workload sample"),
        "post_eviction": _open_observation("Post-helper-eviction sample"),
        "post_quit": _open_observation("Post-quit cleanup sample"),
    }
    observations[observation] = sample_observation
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "desktop_runtime_report",
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git_commit(),
        "machine": {
            "architecture": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
            "memory_bytes": _memory_bytes(),
            "cpu_count": os.cpu_count(),
        },
        "package": {
            "root_name": package_root.name,
            "size_bytes": package_size_bytes,
        },
        "selection": {
            "root_pid": root_pid,
            "ownership_model": "ancestry-plus-verified-webkit-responsibility",
            "retained_process_count": len(enriched),
            "unexpected_retained_process_count": sum(
                1 for process in enriched if bool(process["unexpected"])
            ),
            "webkit_ownership": {
                "source": webkit_ownership["source"],
                "status": webkit_ownership["status"],
                "reason": webkit_ownership["reason"],
                "included_pid_count": len(sibling_evidence),
            },
        },
        "process_tree": enriched,
        "aggregate": {
            "rss_bytes": observation_rss,
            "private_memory_bytes": sum(private_values) if private_values else None,
            "private_memory_complete": len(private_values) == len(enriched),
            "cpu_percent": round(sum(float(process["cpu_percent"]) for process in enriched), 3),
            "process_count": len(enriched),
            "thread_count": sum(thread_counts) if len(thread_counts) == len(enriched) else None,
            "open_file_count": (
                sum(open_file_counts) if len(open_file_counts) == len(enriched) else None
            ),
            "connection_count": (
                sum(connection_counts) if len(connection_counts) == len(enriched) else None
            ),
            "port_count": sum(port_counts) if len(port_counts) == len(enriched) else None,
            "listening_ports": listening_ports,
            "forbidden_idle_processes": forbidden,
            "unexpected_process_count": sum(
                1 for process in enriched if bool(process["unexpected"])
            ),
            "retained_process_count": len(enriched),
        },
        "observations": observations,
        "release_gates": {
            "physical_8_gib": {
                "status": "open",
                "reason": "requires a clean physical 8 GiB release-reference Mac",
            },
            "sleep_wake": {
                "status": "open",
                "reason": "requires physical background, sleep, wake, and relaunch evidence",
            },
            "memory_pressure": {
                "status": "open",
                "reason": "requires physical memory-pressure evidence under constrained hardware",
            },
            "live_provider": {
                "status": "open",
                "reason": (
                    "requires a live optional-provider exercise rather than mocked configuration"
                ),
            },
            "packaged_soak": {
                "status": "open",
                "reason": "requires the manual packaged-app soak plan to complete",
            },
        },
        "privacy": {
            "command_arguments_collected": False,
            "file_paths_collected": False,
            "prompts_or_document_text_collected": False,
            "stable_device_identifier_collected": False,
            "unrelated_processes_retained": False,
            "webkit_attribution": (
                "verified macOS LaunchServices responsibility when available; otherwise fail closed"
            ),
            "connection_details_redacted_to_counts_and_ports": True,
        },
    }


def summary(report: dict[str, object]) -> str:
    package = report["package"]
    aggregate = report["aggregate"]
    machine = report["machine"]
    observations = report.get("observations", {})
    release_gates = report.get("release_gates", {})
    if (
        not isinstance(package, dict)
        or not isinstance(aggregate, dict)
        or not isinstance(machine, dict)
        or not isinstance(observations, dict)
        or not isinstance(release_gates, dict)
    ):
        raise TypeError("runtime report sections are invalid")
    cold_launch = observations.get("cold_launch", {})
    usable_shell = cold_launch.get("usable_shell", {}) if isinstance(cold_launch, dict) else {}
    open_gate_names = [
        label
        for key, label in _GATE_SUMMARY_LABELS.items()
        if isinstance(release_gates.get(key), dict) and release_gates[key].get("status") == "open"
    ]
    open_gate_names.extend(
        sorted(
            key.replace("_", " ")
            for key, value in release_gates.items()
            if key not in _GATE_SUMMARY_LABELS
            and isinstance(value, dict)
            and value.get("status") == "open"
        )
    )
    memory_gib = int(machine["memory_bytes"] or 0) / (1024**3)
    package_mib = int(package["size_bytes"]) / (1024**2)
    rss_mib = int(aggregate["rss_bytes"]) / (1024**2)
    forbidden = aggregate["forbidden_idle_processes"] or "none"
    usable_shell_seconds = usable_shell.get("elapsed_seconds")
    usable_shell_text = (
        f"~{float(usable_shell_seconds):.1f} s usable shell"
        if usable_shell_seconds is not None
        else "usable shell timing pending"
    )
    gate_text = ", ".join(open_gate_names) if open_gate_names else "none"
    return "\n".join(
        (
            "# Lyra desktop resource summary",
            "",
            f"- Commit: `{report['commit']}`",
            f"- Machine: {machine['architecture']}, {memory_gib:.1f} GiB RAM",
            f"- App bundle: {package_mib:.1f} MiB",
            (
                "- Preliminary local build evidence: "
                f"{aggregate['process_count']} retained processes, "
                f"{rss_mib:.1f} MiB RSS, {usable_shell_text}"
            ),
            f"- Aggregate sampled CPU: {aggregate['cpu_percent']}%",
            f"- Forbidden ordinary-idle processes: {forbidden}",
            f"- still-open physical gates: {gate_text}",
            "",
            (
                "This sample is preliminary local build evidence. It does not close the clean "
                "8 GB, "
                "sleep/wake, memory-pressure, live-provider, or packaged-soak gates."
            ),
            "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-pid", type=int, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument(
        "--usable-shell-seconds",
        type=float,
        help="optional manually observed time-to-usable-shell for this sample",
    )
    parser.add_argument(
        "--observation",
        choices=("cold_launch", "warm_launch", "idle_60s", "post_task", "post_eviction"),
        default="cold_launch",
        help="checkpoint represented by this process inventory",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(
        args.root_pid,
        args.package_root,
        usable_shell_seconds=args.usable_shell_seconds,
        observation=args.observation,
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.summary_output.write_text(summary(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
