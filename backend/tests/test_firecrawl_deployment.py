"""Firecrawl deployment tests never require Git, Docker, DNS, or network access."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import infra.firecrawl as deployment


def _completed(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class _Response:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._content = json.dumps(body).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._content[:limit]


def _make_compose_files(manager: deployment.FirecrawlManager) -> None:
    manager.checkout_dir.mkdir(parents=True)
    manager.upstream_compose.write_text("services: {}\n", encoding="utf-8")
    manager.env_file.write_text("POSTGRES_PASSWORD=" + "x" * 40 + "\n", encoding="utf-8")
    manager.override_file.parent.mkdir(parents=True)
    manager.override_file.write_text("services: {}\n", encoding="utf-8")


def test_environment_is_idempotent_private_and_uses_official_database_names(
    tmp_path: Path,
) -> None:
    manager = deployment.FirecrawlManager(tmp_path)

    manager.ensure_environment()
    first = manager.env_file.read_text(encoding="utf-8")
    manager.ensure_environment()

    assert manager.env_file.read_text(encoding="utf-8") == first
    values = deployment._parse_environment(first)
    assert values["POSTGRES_USER"] == "postgres"
    assert values["POSTGRES_DB"] == "postgres"
    assert len(values["POSTGRES_PASSWORD"]) >= 32
    assert values["ALLOW_LOCAL_WEBHOOKS"] == "false"
    assert stat.S_IMODE(manager.env_file.stat().st_mode) == 0o600


def test_existing_environment_permissions_are_repaired_without_rotating_password(
    tmp_path: Path,
) -> None:
    manager = deployment.FirecrawlManager(tmp_path)
    manager.runtime_dir.mkdir()
    password = "a" * 40
    manager.env_file.write_text(f"POSTGRES_PASSWORD={password}\n", encoding="utf-8")
    os.chmod(manager.env_file, 0o644)

    manager.ensure_environment()

    assert password in manager.env_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(manager.env_file.stat().st_mode) == 0o600


def test_checkout_verification_is_idempotent_for_annotated_tag(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        joined = " ".join(args)
        if "remote get-url origin" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_REPOSITORY + "\n")
        if f"refs/tags/{deployment.FIRECRAWL_TAG}" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_TAG_OBJECT + "\n")
        if f"{deployment.FIRECRAWL_TAG}^{{}}" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_HEAD + "\n")
        if "rev-parse HEAD" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_HEAD + "\n")
        if "status --porcelain" in joined:
            return _completed(args)
        raise AssertionError(f"unexpected command: {args}")

    manager = deployment.FirecrawlManager(tmp_path, runner=runner)
    (manager.checkout_dir / ".git").mkdir(parents=True)
    manager.upstream_compose.write_text("services: {}\n", encoding="utf-8")

    manager.ensure_checkout()
    manager.ensure_checkout()

    assert not any("clone" in call for call in calls)
    assert sum("rev-parse HEAD" in " ".join(call) for call in calls) == 2


def test_checkout_mismatch_fails_instead_of_overwriting_source(tmp_path: Path) -> None:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if "remote get-url origin" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_REPOSITORY)
        if f"refs/tags/{deployment.FIRECRAWL_TAG}" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_TAG_OBJECT)
        if f"{deployment.FIRECRAWL_TAG}^{{}}" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_HEAD)
        if "rev-parse HEAD" in joined:
            return _completed(args, stdout="0" * 40)
        raise AssertionError(f"unexpected command: {args}")

    manager = deployment.FirecrawlManager(tmp_path, runner=runner)
    (manager.checkout_dir / ".git").mkdir(parents=True)
    manager.upstream_compose.write_text("services: {}\n", encoding="utf-8")

    with pytest.raises(deployment.ProvisionError, match="source mismatch"):
        manager.ensure_checkout()


def test_existing_shallow_checkout_fetches_missing_pinned_tag(tmp_path: Path) -> None:
    tag_checks = 0
    fetches = 0

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal tag_checks, fetches
        joined = " ".join(args)
        if "remote get-url origin" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_REPOSITORY)
        if f"refs/tags/{deployment.FIRECRAWL_TAG}" in joined:
            tag_checks += 1
            if tag_checks == 1:
                return _completed(args, returncode=128, stderr="unknown revision")
            return _completed(args, stdout=deployment.FIRECRAWL_TAG_OBJECT)
        if f"fetch --depth 1 origin tag {deployment.FIRECRAWL_TAG}" in joined:
            fetches += 1
            return _completed(args)
        if f"{deployment.FIRECRAWL_TAG}^{{}}" in joined or "rev-parse HEAD" in joined:
            return _completed(args, stdout=deployment.FIRECRAWL_HEAD)
        if "status --porcelain" in joined:
            return _completed(args)
        raise AssertionError(f"unexpected command: {args}")

    manager = deployment.FirecrawlManager(tmp_path, runner=runner, sleep=lambda delay: None)
    (manager.checkout_dir / ".git").mkdir(parents=True)
    manager.upstream_compose.write_text("services: {}\n", encoding="utf-8")

    manager.ensure_checkout()

    assert fetches == 1
    assert tag_checks == 2


def test_port_conflict_never_kills_unknown_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BusySocket:
        def setsockopt(self, *args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            raise OSError("address already in use")

        def close(self) -> None:
            pass

    manager = deployment.FirecrawlManager(tmp_path)
    monkeypatch.setattr(deployment.socket, "socket", lambda *args: BusySocket())
    monkeypatch.setattr(manager, "_compose_services", lambda **kwargs: set())

    with pytest.raises(deployment.ProvisionError, match="never kill"):
        manager._preflight_port()


def test_readiness_does_not_hide_functional_scrape_failure(tmp_path: Path) -> None:
    responses = iter(
        (
            _Response(200, {"status": "ready"}),
            _Response(503, {"success": False, "error": "playwright failed"}),
        )
    )
    manager = deployment.FirecrawlManager(
        tmp_path,
        opener=lambda request, **kwargs: next(responses),
        readiness_wait_seconds=1,
    )

    manager._wait_until_ready()
    with pytest.raises(deployment.ProvisionError, match="real scrape test failed"):
        manager._scrape_smoke_test()


def test_retry_uses_bounded_exponential_backoff(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise deployment.ProvisionError("temporary")

    manager = deployment.FirecrawlManager(tmp_path, sleep=sleeps.append, output=lambda text: None)
    manager._retry("operation", flaky, attempts=3)

    assert attempts == 3
    assert sleeps == [1, 2]


def test_current_build_receipt_reuses_exact_image_ids(tmp_path: Path) -> None:
    image_ids = {name: f"sha256:{index}" for index, name in enumerate(deployment.BUILT_IMAGES)}

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["docker", "image", "inspect", "--format"]
        return _completed(args, stdout="\n".join(image_ids.values()) + "\n")

    manager = deployment.FirecrawlManager(tmp_path, runner=runner)
    manager.runtime_dir.mkdir()
    manager.override_file.parent.mkdir()
    manager.override_file.write_text("services: {}\n", encoding="utf-8")
    manager.build_receipt.write_text(
        json.dumps(
            {
                "source_head": deployment.FIRECRAWL_HEAD,
                "override_sha256": manager._override_sha256(),
                "images": image_ids,
            }
        ),
        encoding="utf-8",
    )

    assert manager._build_is_current() is True
    manager.override_file.write_text("services: {api: {}}\n", encoding="utf-8")
    assert manager._build_is_current() is False


def test_start_skips_build_when_receipt_and_images_are_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose_calls: list[tuple[str, ...]] = []
    manager = deployment.FirecrawlManager(tmp_path, output=lambda text: None)
    for name in (
        "_preflight_tools_and_daemon",
        "ensure_checkout",
        "ensure_environment",
        "_preflight_resources",
        "_validate_compose_render",
        "_preflight_port",
        "_wait_until_ready",
        "_scrape_smoke_test",
        "_redirect_safety_test",
    ):
        monkeypatch.setattr(manager, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_build_is_current", lambda: True)
    monkeypatch.setattr(
        manager,
        "_compose",
        lambda *args, **kwargs: compose_calls.append(args) or _completed(list(args)),
    )

    assert manager.start().healthy is True
    assert not any(call and call[0] == "build" for call in compose_calls)
    assert any(call and call[0] == "up" for call in compose_calls)


def test_resource_preflight_warns_below_eight_gibibytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    manager = deployment.FirecrawlManager(tmp_path, output=messages.append)
    manager.runtime_dir.mkdir()
    monkeypatch.setattr(
        manager,
        "_docker_info",
        lambda: {"ServerVersion": "27", "NCPU": 8, "MemTotal": 7_800_000_000},
    )
    monkeypatch.setattr(
        deployment.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=20 * 1024**3),
    )

    manager._preflight_resources()

    assert any("Warning" in message and "8 GiB" in message for message in messages)


def test_compose_render_requires_one_loopback_api_port(tmp_path: Path) -> None:
    api_port = {
        "host_ip": "127.0.0.1",
        "target": 3002,
        "published": "3002",
        "protocol": "tcp",
    }

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        config = {
            "services": {
                "api": {"ports": [api_port]},
                "redis": {},
                "rabbitmq": {},
                "nuq-postgres": {},
                "playwright-service": {},
            }
        }
        return _completed(args, stdout=json.dumps(config))

    manager = deployment.FirecrawlManager(tmp_path, runner=runner)
    _make_compose_files(manager)
    manager._validate_compose_render()


def test_compose_render_rejects_surviving_upstream_wildcard_port(tmp_path: Path) -> None:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        config = {
            "services": {
                "api": {
                    "ports": [
                        {
                            "host_ip": "0.0.0.0",  # noqa: S104 - unsafe fixture
                            "target": 3002,
                            "published": "3002",
                        },
                        {"host_ip": "127.0.0.1", "target": 3002, "published": "3002"},
                    ]
                }
            }
        }
        return _completed(args, stdout=json.dumps(config))

    manager = deployment.FirecrawlManager(tmp_path, runner=runner)
    _make_compose_files(manager)

    with pytest.raises(deployment.ProvisionError, match="Unsafe Firecrawl API port"):
        manager._validate_compose_render()


def test_status_enumerates_every_required_service(tmp_path: Path) -> None:
    rows = "\n".join(
        json.dumps({"Service": name, "State": "running", "Health": "healthy"})
        for name in deployment.REQUIRED_SERVICES
    )

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "ps" in args
        return _completed(args, stdout=rows)

    manager = deployment.FirecrawlManager(
        tmp_path,
        runner=runner,
        opener=lambda request, **kwargs: _Response(200, {"status": "ok"}),
    )
    _make_compose_files(manager)

    result = manager.status()

    assert result.ready is True
    assert len(result.services) == len(deployment.REQUIRED_SERVICES)
    assert all(f"{name}=running/healthy" in result.detail for name in deployment.REQUIRED_SERVICES)


def test_macos_preflight_opens_docker_once_then_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker_info_calls = 0
    opens = 0
    sleeps: list[float] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal docker_info_calls, opens
        if args[:4] == ["docker", "compose", "version", "--short"]:
            return _completed(args, stdout="2.30.0\n")
        if args[:2] == ["docker", "info"]:
            docker_info_calls += 1
            if docker_info_calls < 3:
                # Docker Desktop sometimes emits empty JSON alongside a socket error.
                return _completed(args, returncode=1, stdout="{}\n", stderr="daemon stopped")
            return _completed(
                args,
                stdout=json.dumps({"ServerVersion": "27.0.0", "NCPU": 8, "MemTotal": 12 * 1024**3}),
            )
        if args[:3] == ["open", "-a", "Docker"]:
            opens += 1
            return _completed(args)
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(deployment.shutil, "which", lambda executable: f"/bin/{executable}")
    manager = deployment.FirecrawlManager(
        tmp_path,
        runner=runner,
        sleep=sleeps.append,
        system="Darwin",
        docker_wait_seconds=9,
    )

    manager._preflight_tools_and_daemon(start_docker_desktop=True)

    assert opens == 1
    assert docker_info_calls == 3
    assert sleeps == [3]


def test_redirect_gate_accepts_only_clear_private_address_refusal(tmp_path: Path) -> None:
    manager = deployment.FirecrawlManager(
        tmp_path,
        opener=lambda request, **kwargs: _Response(
            400, {"success": False, "error": "URL is blocked: private address"}
        ),
    )

    manager._redirect_safety_test()


def test_redirect_gate_accepts_clear_internal_refusal_even_on_worker_error(tmp_path: Path) -> None:
    manager = deployment.FirecrawlManager(
        tmp_path,
        opener=lambda request, **kwargs: _Response(
            500,
            {
                "success": False,
                "error": "Blocked insecure target: navigation to private/internal resource",
            },
        ),
    )

    manager._redirect_safety_test()


def test_redirect_gate_accepts_exact_connection_security_log_for_unique_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = deployment.FirecrawlManager(
        tmp_path,
        opener=lambda request, **kwargs: _Response(
            500, {"success": False, "error": "all engines failed"}
        ),
    )
    monkeypatch.setattr(deployment.secrets, "token_hex", lambda _: "probe123")
    monkeypatch.setattr(
        manager,
        "_compose",
        lambda *args, **kwargs: _completed(
            list(args),
            stdout=(
                "url=http://127.0.0.1/private?lyra_gate=probe123 "
                "Connection violated security rules.\n"
            ),
        ),
    )

    manager._redirect_safety_test()


def test_compose_project_is_stable_and_checkout_scoped(tmp_path: Path) -> None:
    first = deployment.FirecrawlManager(tmp_path / "first")
    same = deployment.FirecrawlManager(tmp_path / "first")
    other = deployment.FirecrawlManager(tmp_path / "other")

    assert first.compose_project == same.compose_project
    assert first.compose_project.startswith(deployment.COMPOSE_PROJECT_PREFIX + "-")
    assert first.compose_project != other.compose_project


@pytest.mark.parametrize(
    "status,body,match",
    [
        (200, {"success": True, "data": {"markdown": "leaked"}}, "SECURITY FAILURE"),
        (503, {"success": False, "error": "upstream unavailable"}, "ambiguous"),
    ],
)
def test_redirect_gate_fails_closed_on_success_or_ambiguity(
    tmp_path: Path,
    status: int,
    body: object,
    match: str,
) -> None:
    manager = deployment.FirecrawlManager(
        tmp_path,
        opener=lambda request, **kwargs: _Response(status, body),
        environ={
            "LYRA_FIRECRAWL_REDIRECT_TEST_URL": (
                "https://redirect.example.test/?url=http://127.0.0.1:3002/private"
            )
        },
    )

    with pytest.raises(deployment.ProvisionError, match=match):
        manager._redirect_safety_test()
