#!/usr/bin/env python3
"""Provision and supervise Lyra's pinned, local Firecrawl deployment.

This module intentionally uses only the Python standard library so the outer
Lyra launcher can diagnose infrastructure before the project virtualenv exists.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

FIRECRAWL_REPOSITORY = "https://github.com/firecrawl/firecrawl.git"
FIRECRAWL_TAG = "v2.11.162"
# v2.11.162 is an annotated tag. Pin both the tag object and the source it peels to.
FIRECRAWL_TAG_OBJECT = "ebfe595c3e218266c627f8651ccbcbe38f4ecfdd"
FIRECRAWL_HEAD = "7666c1f9ae8720a6bba271e0f60b6a217f8a5210"
COMPOSE_PROJECT_PREFIX = "lyra-firecrawl"
MIN_COMPOSE_VERSION = (2, 24, 4)
MIN_DOCKER_CPUS = 4
MIN_DOCKER_MEMORY = 4 * 1024**3
RECOMMENDED_DOCKER_MEMORY = 8 * 1024**3
MIN_DISK_FREE = 10 * 1024**3
API_PORT = 3002
READINESS_URL = "http://127.0.0.1:3002/v0/health/readiness"
SCRAPE_URL = "http://127.0.0.1:3002/v2/scrape"
BUILT_IMAGES = (
    f"lyra-firecrawl-api:{FIRECRAWL_TAG}",
    f"lyra-firecrawl-playwright-service:{FIRECRAWL_TAG}",
    f"lyra-firecrawl-nuq-postgres:{FIRECRAWL_TAG}",
)
REQUIRED_SERVICES = ("api", "playwright-service", "redis", "rabbitmq", "nuq-postgres")


class ProvisionError(RuntimeError):
    """A recoverable infrastructure problem with an actionable message."""


@dataclass(frozen=True)
class FirecrawlStatus:
    """A compact status value suitable for the outer application launcher."""

    running: bool
    ready: bool
    functional: bool = False
    redirect_safe: bool = False
    detail: str = ""
    services: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.running and self.ready and self.functional and self.redirect_safe


@dataclass(frozen=True)
class _HttpResult:
    status: int
    body: object
    text: str


Runner = Callable[..., subprocess.CompletedProcess[str]]
UrlOpener = Callable[..., Any]


class FirecrawlManager:
    """Idempotent owner of the Firecrawl source checkout and Compose project."""

    def __init__(
        self,
        repo_root: Path | str | None = None,
        *,
        runner: Runner = subprocess.run,
        opener: UrlOpener = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        system: str | None = None,
        docker_wait_seconds: int = 90,
        readiness_wait_seconds: int = 240,
        output: Callable[[str], None] = print,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
        checkout_id = hashlib.sha256(str(self.repo_root).encode()).hexdigest()[:10]
        self.compose_project = f"{COMPOSE_PROJECT_PREFIX}-{checkout_id}"
        self.runtime_dir = self.repo_root / ".lyra"
        self.checkout_dir = self.runtime_dir / "firecrawl"
        self.env_file = self.runtime_dir / "firecrawl.env"
        self.build_receipt = self.runtime_dir / "firecrawl.build.json"
        self.override_file = self.repo_root / "infra" / "firecrawl.override.yml"
        self.upstream_compose = self.checkout_dir / "docker-compose.yaml"
        self._runner = runner
        self._opener = opener
        self._sleep = sleep
        self._system = system or platform.system()
        self._docker_wait_seconds = docker_wait_seconds
        self._readiness_wait_seconds = readiness_wait_seconds
        self._output = output
        self._environ = dict(os.environ if environ is None else environ)

    def start(self) -> FirecrawlStatus:
        """Provision, start, and functionally verify Firecrawl."""

        try:
            self._preflight_tools_and_daemon(start_docker_desktop=True)
            self.ensure_checkout()
            self.ensure_environment()
            self._preflight_resources()
            self._validate_compose_render()
            self._preflight_port()
            if self._build_is_current():
                self._output("Pinned Firecrawl images are current; skipping rebuild.")
            else:
                self._output("Building pinned Firecrawl containers (first run can take a while)...")

                def build() -> None:
                    self._compose("build", "--pull", timeout=3600)
                    self._record_build()

                self._retry("Firecrawl image build", build, attempts=3)
            self._output("Starting Firecrawl services...")
            self._retry(
                "Firecrawl startup",
                lambda: self._compose(
                    "up",
                    "-d",
                    "--remove-orphans",
                    "--wait",
                    "--wait-timeout",
                    "240",
                    timeout=300,
                ),
                attempts=3,
            )
            self._wait_until_ready()
            self._scrape_smoke_test()
            self._redirect_safety_test()
        except ProvisionError:
            self._diagnose()
            raise

        return FirecrawlStatus(
            running=True,
            ready=True,
            functional=True,
            redirect_safe=True,
            detail="Firecrawl passed readiness, scrape, and redirect-safety checks.",
        )

    def stop(self) -> None:
        """Stop managed containers without removing containers, networks, or volumes."""

        self._require_compose_files()
        self._compose("stop", "--timeout", "30", timeout=60)
        self._output("Firecrawl stopped. Persistent volumes were preserved.")

    def status(self) -> FirecrawlStatus:
        """Return non-mutating container and HTTP readiness state."""

        if not self.upstream_compose.is_file() or not self.env_file.is_file():
            return FirecrawlStatus(False, False, detail="Firecrawl has not been provisioned.")
        service_states = self._compose_service_states()
        summaries = tuple(
            f"{name}={service_states.get(name, ('missing', ''))[0]}"
            + (
                f"/{service_states[name][1]}"
                if name in service_states and service_states[name][1]
                else ""
            )
            for name in REQUIRED_SERVICES
        )
        running = all(
            service_states.get(name, ("", ""))[0] == "running" for name in REQUIRED_SERVICES
        )
        if not running:
            return FirecrawlStatus(
                False,
                False,
                detail="Firecrawl services are not all running: " + ", ".join(summaries),
                services=summaries,
            )
        try:
            response = self._http("GET", READINESS_URL, timeout=5)
        except ProvisionError as exc:
            return FirecrawlStatus(True, False, detail=str(exc), services=summaries)
        ready = response.status == 200
        prefix = "Firecrawl is ready" if ready else f"Readiness returned HTTP {response.status}"
        detail = f"{prefix}: " + ", ".join(summaries)
        return FirecrawlStatus(True, ready, detail=detail, services=summaries)

    def doctor(self) -> FirecrawlStatus:
        """Run all installation, readiness, function, and security checks."""

        self._preflight_tools_and_daemon(start_docker_desktop=False)
        self.ensure_checkout()
        self.ensure_environment()
        self._preflight_resources()
        self._validate_compose_render()
        self._preflight_port()
        current = self.status()
        if not current.running or not current.ready:
            raise ProvisionError(
                f"Firecrawl is not ready: {current.detail} Run `python -m infra.firecrawl start`."
            )
        self._scrape_smoke_test()
        self._redirect_safety_test()
        return FirecrawlStatus(
            True,
            True,
            True,
            True,
            "All Firecrawl deployment checks passed.",
        )

    def logs(self, *, follow: bool = False, tail: int = 160) -> None:
        """Print bounded service logs, optionally following them."""

        self._require_compose_files()
        args = ["logs", "--tail", str(tail)]
        if follow:
            args.append("--follow")
        result = self._compose(*args, timeout=None if follow else 60, check=False)
        if result.stdout:
            self._output(result.stdout.rstrip())
        if result.stderr:
            self._output(result.stderr.rstrip())
        if result.returncode:
            raise ProvisionError("Docker Compose could not read Firecrawl logs.")

    def ensure_checkout(self) -> None:
        """Create or verify the exact shallow Firecrawl source checkout."""

        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.checkout_dir.exists():

            def clone() -> None:
                with tempfile.TemporaryDirectory(
                    prefix="firecrawl-clone-", dir=self.runtime_dir
                ) as temp:
                    destination = Path(temp) / "checkout"
                    self._command(
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        "--branch",
                        FIRECRAWL_TAG,
                        "--single-branch",
                        FIRECRAWL_REPOSITORY,
                        str(destination),
                        timeout=300,
                    )
                    destination.replace(self.checkout_dir)

            self._retry("Firecrawl source download", clone, attempts=3)

        if not (self.checkout_dir / ".git").exists():
            raise ProvisionError(
                f"{self.checkout_dir} exists but is not the managed Firecrawl checkout. "
                "Move it aside and retry."
            )
        remote = self._git("remote", "get-url", "origin").stdout.strip()
        if remote.rstrip("/").removesuffix(".git") != FIRECRAWL_REPOSITORY.removesuffix(".git"):
            raise ProvisionError(
                f"Firecrawl checkout has unexpected origin {remote!r}; expected the official "
                "repository."
            )
        tag_ref = self._command(
            "git",
            "rev-parse",
            f"refs/tags/{FIRECRAWL_TAG}",
            cwd=self.checkout_dir,
            check=False,
            timeout=30,
        )
        if tag_ref.returncode:
            self._retry(
                "Firecrawl tag fetch",
                lambda: self._git("fetch", "--depth", "1", "origin", "tag", FIRECRAWL_TAG),
                attempts=3,
            )
            tag_ref = self._git("rev-parse", f"refs/tags/{FIRECRAWL_TAG}")
        tag_object = tag_ref.stdout.strip()
        if tag_object != FIRECRAWL_TAG_OBJECT:
            raise ProvisionError(
                f"Firecrawl tag identity mismatch: expected {FIRECRAWL_TAG_OBJECT}, got "
                f"{tag_object or 'nothing'}. Refusing to build unpinned source."
            )
        peeled = self._git("rev-parse", f"{FIRECRAWL_TAG}^{{}}").stdout.strip()
        head = self._git("rev-parse", "HEAD").stdout.strip()
        if peeled != FIRECRAWL_HEAD or head != FIRECRAWL_HEAD:
            raise ProvisionError(
                f"Firecrawl source mismatch: expected HEAD {FIRECRAWL_HEAD}, got "
                f"{head or 'nothing'}. "
                "Move the checkout aside and retry; it will not be overwritten automatically."
            )
        dirty = self._git("status", "--porcelain", "--untracked-files=no").stdout.strip()
        if dirty:
            raise ProvisionError(
                "The managed Firecrawl checkout has tracked modifications. Move it aside and retry."
            )
        if not self.upstream_compose.is_file():
            raise ProvisionError("Pinned Firecrawl checkout has no docker-compose.yaml.")

    def ensure_environment(self) -> None:
        """Create the private Compose environment once and enforce safe permissions."""

        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.env_file.exists():
            password = secrets.token_urlsafe(32)
            content = _environment_content(password)
            descriptor = os.open(
                self.env_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
            except BaseException:
                self.env_file.unlink(missing_ok=True)
                raise
        os.chmod(self.env_file, stat.S_IRUSR | stat.S_IWUSR)
        values = _parse_environment(self.env_file.read_text(encoding="utf-8"))
        password = values.get("POSTGRES_PASSWORD", "")
        if len(password) < 32 or password.startswith("replace-"):
            raise ProvisionError(
                f"{self.env_file} has an unsafe Postgres password. Set a random value of at least "
                "32 characters; existing database credentials are never rotated automatically."
            )

    def _preflight_tools_and_daemon(self, *, start_docker_desktop: bool) -> None:
        for executable in ("git", "docker"):
            if shutil.which(executable) is None:
                raise ProvisionError(
                    f"Required executable `{executable}` was not found on PATH. Install it and "
                    "retry."
                )
        version = self._command("docker", "compose", "version", "--short", check=False)
        if version.returncode:
            raise ProvisionError(
                "Docker Compose v2 is required (`docker compose`, not docker-compose)."
            )
        parsed = _version_tuple(version.stdout)
        if parsed < MIN_COMPOSE_VERSION:
            required = ".".join(map(str, MIN_COMPOSE_VERSION))
            raise ProvisionError(
                f"Docker Compose {required}+ is required for safe `!override` port merging; "
                f"found {version.stdout.strip() or 'an unknown version'}."
            )

        if self._docker_info() is not None:
            return
        if start_docker_desktop and self._system == "Darwin" and shutil.which("open"):
            self._output("Docker is installed but its daemon is stopped; opening Docker Desktop...")
            opened = self._command("open", "-a", "Docker", check=False, timeout=15)
            if opened.returncode == 0:
                attempts = max(1, math.ceil(self._docker_wait_seconds / 3))
                for attempt in range(attempts):
                    if self._docker_info() is not None:
                        self._output("Docker Desktop is ready.")
                        return
                    if attempt + 1 < attempts:
                        self._sleep(3)
        raise ProvisionError(
            "Docker is installed but the daemon is unavailable. Start Docker Desktop "
            "(macOS/Windows) or the Docker service (Linux), wait until it is ready, and retry."
        )

    def _docker_info(self) -> dict[str, object] | None:
        result = self._command("docker", "info", "--format", "{{json .}}", check=False, timeout=15)
        if result.returncode or not result.stdout.strip():
            return None
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(info, dict) or not info.get("ServerVersion") or not info.get("NCPU"):
            return None
        return info

    def _preflight_resources(self) -> None:
        info = self._docker_info()
        if info is None:
            raise ProvisionError("Docker daemon became unavailable during preflight.")
        cpus = int(info.get("NCPU", 0))
        try:
            memory = int(info.get("MemTotal", 0))
        except (TypeError, ValueError) as exc:
            raise ProvisionError("Docker did not report its allocated memory.") from exc
        if cpus < MIN_DOCKER_CPUS:
            raise ProvisionError(
                f"Docker has {cpus} CPU(s); allocate at least {MIN_DOCKER_CPUS} to run Firecrawl."
            )
        if memory < MIN_DOCKER_MEMORY:
            allocated = memory / 1024**3
            required = MIN_DOCKER_MEMORY / 1024**3
            raise ProvisionError(
                f"Docker has {allocated:.1f} GiB memory; allocate at least {required:.0f} GiB "
                "in Docker Desktop resources."
            )
        if memory < RECOMMENDED_DOCKER_MEMORY:
            self._output(
                f"Warning: Docker has {memory / 1024**3:.1f} GiB memory. Firecrawl is more "
                "reliable with about 8 GiB or more; continuing with reduced concurrency."
            )
        free = shutil.disk_usage(self.runtime_dir).free
        if free < MIN_DISK_FREE:
            raise ProvisionError(
                f"Only {free / 1024**3:.1f} GiB disk is free; at least "
                f"{MIN_DISK_FREE / 1024**3:.0f} GiB is required for the pinned source build."
            )

    def _preflight_port(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", API_PORT))
        except OSError as exc:
            if "api" in self._compose_services(running_only=True):
                return
            raise ProvisionError(
                f"Port 127.0.0.1:{API_PORT} is already in use by another process. "
                "Stop that process or configure it manually; Lyra will never kill it automatically."
            ) from exc
        finally:
            probe.close()

    def _build_is_current(self) -> bool:
        try:
            receipt = json.loads(self.build_receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if receipt.get("source_head") != FIRECRAWL_HEAD:
            return False
        if receipt.get("override_sha256") != self._override_sha256():
            return False
        recorded_images = receipt.get("images")
        if not isinstance(recorded_images, dict):
            return False
        current_images = self._inspect_built_images()
        return current_images is not None and current_images == recorded_images

    def _record_build(self) -> None:
        images = self._inspect_built_images()
        if images is None:
            raise ProvisionError(
                "Docker reported a successful build but one or more Firecrawl images are missing."
            )
        receipt = {
            "source_head": FIRECRAWL_HEAD,
            "override_sha256": self._override_sha256(),
            "images": images,
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix="firecrawl-build-", suffix=".json", dir=self.runtime_dir
        )
        try:
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.build_receipt)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
            raise

    def _inspect_built_images(self) -> dict[str, str] | None:
        result = self._command(
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            *BUILT_IMAGES,
            check=False,
            timeout=30,
        )
        identifiers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or len(identifiers) != len(BUILT_IMAGES):
            return None
        return dict(zip(BUILT_IMAGES, identifiers, strict=True))

    def _override_sha256(self) -> str:
        try:
            content = self.override_file.read_bytes()
        except OSError as exc:
            raise ProvisionError(f"Could not read Compose override: {exc}") from exc
        return hashlib.sha256(content).hexdigest()

    def _validate_compose_render(self) -> None:
        result = self._compose("config", "--format", "json", timeout=30)
        try:
            config = json.loads(result.stdout)
            services = config["services"]
            api_ports = services["api"].get("ports", [])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProvisionError(
                "Docker Compose produced an unreadable rendered configuration."
            ) from exc
        published = [port for port in api_ports if str(port.get("published")) == str(API_PORT)]
        valid = (
            len(published) == 1
            and str(published[0].get("target")) == str(API_PORT)
            and published[0].get("host_ip") == "127.0.0.1"
        )
        if not valid or len(api_ports) != 1:
            raise ProvisionError(
                "Unsafe Firecrawl API port rendering: expected exactly "
                "127.0.0.1:3002 -> 3002 and no wildcard binding."
            )
        for name in ("redis", "rabbitmq", "nuq-postgres", "playwright-service"):
            if services.get(name, {}).get("ports"):
                raise ProvisionError(
                    f"Unsafe Compose configuration publishes private service {name}."
                )

    def _wait_until_ready(self) -> None:
        attempts = max(1, math.ceil(self._readiness_wait_seconds / 3))
        last_detail = "no response"
        for attempt in range(attempts):
            try:
                response = self._http("GET", READINESS_URL, timeout=5)
                last_detail = f"HTTP {response.status}"
                if response.status == 200:
                    return
            except ProvisionError as exc:
                last_detail = str(exc)
            if attempt + 1 < attempts:
                self._sleep(3)
        raise ProvisionError(
            f"Firecrawl containers started but readiness did not pass ({last_detail}). "
            "Inspect `python -m infra.firecrawl logs`."
        )

    def _scrape_smoke_test(self) -> None:
        response = self._http(
            "POST",
            SCRAPE_URL,
            payload={
                "url": "https://example.com",
                "formats": ["markdown"],
                "onlyMainContent": True,
                "timeout": 15_000,
                "storeInCache": False,
            },
            timeout=45,
        )
        body = response.body
        data = body.get("data") if isinstance(body, dict) else None
        content = ""
        if isinstance(data, dict):
            content = str(data.get("markdown") or data.get("html") or data.get("rawHtml") or "")
        if response.status != 200 or not isinstance(body, dict) or body.get("success") is not True:
            raise ProvisionError(
                f"Firecrawl is ready but its real scrape test failed (HTTP {response.status}). "
                f"{_safe_error(body)}"
            )
        if not content.strip():
            raise ProvisionError(
                "Firecrawl returned success for example.com but no content; readiness is not "
                "sufficient. Inspect API and Playwright logs."
            )

    def _redirect_safety_test(self) -> None:
        configured = self._environ.get("LYRA_FIRECRAWL_REDIRECT_TEST_URL", "").strip()
        evidence_token = ""
        if configured:
            target = configured
        else:
            evidence_token = secrets.token_hex(8)
            private_target = f"http://127.0.0.1:3002/v0/health/readiness?lyra_gate={evidence_token}"
            target = f"https://httpbin.org/redirect-to?url={quote(private_target, safe='')}"
        _validate_redirect_test_url(target)
        response = self._http(
            "POST",
            SCRAPE_URL,
            payload={
                "url": target,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "timeout": 15_000,
                "storeInCache": False,
            },
            timeout=45,
        )
        body = response.body
        if isinstance(body, dict) and body.get("success") is True:
            final_url = _find_final_url(body)
            suffix = f" (reported final URL: {final_url})" if final_url else ""
            raise ProvisionError(
                "SECURITY FAILURE: Firecrawl accepted a public-to-loopback redirect" + suffix + "."
            )
        refusal = _safe_error(body).lower()
        refused = isinstance(body, dict) and body.get("success") is False
        if refused and any(
            marker in refusal
            for marker in (
                "private ip",
                "private address",
                "private/internal",
                "local address",
                "loopback",
                "ssrf",
            )
        ):
            return
        if refused and evidence_token:
            logs = self._compose(
                "logs",
                "--no-color",
                "--tail",
                "400",
                "api",
                "playwright-service",
                check=False,
                timeout=30,
            )
            diagnostic = "\n".join((logs.stdout, logs.stderr))
            if evidence_token in diagnostic and "Connection violated security rules." in diagnostic:
                return
        raise ProvisionError(
            "Firecrawl's redirect-safety response was ambiguous. The gate fails closed because "
            "the public redirect service may be unavailable or Firecrawl did not clearly reject "
            f"the loopback destination (HTTP {response.status}: "
            f"{refusal[:240] or 'empty response'}). "
            "Retry `doctor` or manually verify the configured redirect endpoint."
        )

    def _http(
        self,
        method: str,
        url: str,
        *,
        payload: object | None = None,
        timeout: int,
    ) -> _HttpResult:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(  # noqa: S310 - callers and redirect drill constrain schemes.
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read(2_000_001)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(2_000_001)
        except (OSError, URLError, TimeoutError) as exc:
            raise ProvisionError(f"Could not reach Firecrawl at {url}: {exc}") from exc
        if len(raw) > 2_000_000:
            raise ProvisionError("Firecrawl returned an unexpectedly large diagnostic response.")
        text = raw.decode("utf-8", errors="replace")
        try:
            body: object = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = {"error": text[:500]}
        return _HttpResult(status, body, text)

    def _diagnose(self) -> None:
        if not self.upstream_compose.is_file() or not self.env_file.is_file():
            return
        self._output("Firecrawl did not pass verification. Managed volumes have been preserved.")
        for args in (
            ("ps", "--all"),
            (
                "logs",
                "--no-color",
                "--tail",
                "120",
                "api",
                "playwright-service",
                "rabbitmq",
                "redis",
                "nuq-postgres",
            ),
        ):
            result = self._compose(*args, check=False, timeout=60)
            text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            if text:
                self._output(text)

    def _retry(self, label: str, action: Callable[[], None], *, attempts: int) -> None:
        last_error: ProvisionError | None = None
        for attempt in range(1, attempts + 1):
            try:
                action()
                return
            except ProvisionError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                delay = 2 ** (attempt - 1)
                self._output(f"{label} attempt {attempt} failed; retrying in {delay}s...")
                self._sleep(delay)
        if last_error is None:  # Defensive: attempts is always positive at call sites.
            raise ProvisionError(f"{label} did not run")
        raise ProvisionError(
            f"{label} failed after {attempts} attempts: {last_error}"
        ) from last_error

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._command("git", *args, cwd=self.checkout_dir, timeout=30)

    def _compose(
        self,
        *args: str,
        check: bool = True,
        timeout: int | None = 120,
    ) -> subprocess.CompletedProcess[str]:
        self._require_compose_files()
        return self._command(*self._compose_command(), *args, check=check, timeout=timeout)

    def _compose_command(self) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--project-name",
            self.compose_project,
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.upstream_compose),
            "-f",
            str(self.override_file),
        )

    def _compose_services(self, *, running_only: bool) -> set[str]:
        if not self.upstream_compose.is_file() or not self.env_file.is_file():
            return set()
        args = ["ps"]
        if running_only:
            args.extend(("--status", "running"))
        args.append("--services")
        result = self._compose(*args, check=False, timeout=30)
        if result.returncode:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _compose_service_states(self) -> dict[str, tuple[str, str]]:
        """Return each Compose service's container state and health without truncating failures."""

        result = self._compose("ps", "--all", "--format", "json", check=False, timeout=30)
        if result.returncode:
            return {}
        states: dict[str, tuple[str, str]] = {}
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("Service"), str):
                continue
            states[row["Service"]] = (
                str(row.get("State") or "unknown"),
                str(row.get("Health") or ""),
            )
        return states

    def _require_compose_files(self) -> None:
        if not self.upstream_compose.is_file():
            raise ProvisionError(
                "Firecrawl source is not provisioned; run the start command first."
            )
        if not self.override_file.is_file():
            raise ProvisionError(f"Required Lyra Compose override is missing: {self.override_file}")
        if not self.env_file.is_file():
            raise ProvisionError("Firecrawl environment is not provisioned; run start first.")

    def _command(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int | None = 120,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                list(args),
                cwd=str(cwd) if cwd else None,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvisionError(f"Could not run {' '.join(args[:3])}: {exc}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "no diagnostic output").strip()
            raise ProvisionError(
                f"Command `{' '.join(args[:3])}` failed with exit {result.returncode}: "
                f"{detail[-1500:]}"
            )
        return result


def ensure_firecrawl(repo_root: Path | str | None = None) -> FirecrawlStatus:
    """Narrow application-launcher API: provision, start, and verify Firecrawl."""

    return FirecrawlManager(repo_root).start()


def _environment_content(password: str) -> str:
    required = {
        # The pinned nuq-postgres init scripts and pg_cron extension target the
        # default database/user names. Only the password is safe to customize.
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "postgres",
        "LYRA_FIRECRAWL_PORT": str(API_PORT),
        "INTERNAL_PORT": str(API_PORT),
        "NUM_WORKERS_PER_QUEUE": "2",
        "CRAWL_CONCURRENT_REQUESTS": "2",
        "MAX_CONCURRENT_JOBS": "2",
        "BROWSER_POOL_SIZE": "2",
        "LOGGING_LEVEL": "info",
        "ALLOW_LOCAL_WEBHOOKS": "false",
        "BLOCK_MEDIA": "true",
    }
    optional = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MODEL_NAME",
        "MODEL_EMBEDDING_NAME",
        "OLLAMA_BASE_URL",
        "AUTUMN_SECRET_KEY",
        "SLACK_WEBHOOK_URL",
        "BULL_AUTH_KEY",
        "TEST_API_KEY",
        "SUPABASE_ANON_TOKEN",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_TOKEN",
        "SELF_HOSTED_WEBHOOK_URL",
        "PROXY_SERVER",
        "PROXY_USERNAME",
        "PROXY_PASSWORD",
        "SEARXNG_ENDPOINT",
        "SEARXNG_ENGINES",
        "SEARXNG_CATEGORIES",
        "NUQ_BACKEND",
    )
    lines = ["# Generated by Lyra. Keep private; do not commit."]
    lines.extend(f"{key}={value}" for key, value in required.items())
    lines.extend(f"{key}=" for key in optional)
    return "\n".join(lines) + "\n"


def _parse_environment(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:v)?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def _safe_error(body: object) -> str:
    if not isinstance(body, dict):
        return "unstructured response"
    parts: list[str] = []
    for key in ("error", "message", "detail"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            parts.extend(str(item) for item in value.values() if isinstance(item, str))
    return " | ".join(parts)[:500] or "no error detail"


def _find_final_url(body: dict[str, object]) -> str:
    data = body.get("data")
    if not isinstance(data, dict):
        return ""
    metadata = data.get("metadata")
    candidates: list[object] = [data.get("url")]
    if isinstance(metadata, dict):
        candidates.extend(
            (metadata.get("sourceURL"), metadata.get("sourceUrl"), metadata.get("url"))
        )
    return next((value for value in candidates if isinstance(value, str)), "")


def _validate_redirect_test_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProvisionError("Redirect test URL must be a public HTTPS URL.")
    try:
        if ipaddress.ip_address(parsed.hostname).is_private:
            raise ProvisionError(
                "Redirect test must begin at a public host, not a private address."
            )
    except ValueError:
        pass
    query = unquote(" ".join(item for values in parse_qs(parsed.query).values() for item in values))
    if not any(marker in query.lower() for marker in ("127.0.0.1", "localhost", "[::1]")):
        raise ProvisionError("Redirect test URL must redirect to an explicit loopback destination.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="provision, start, and verify Firecrawl")
    subparsers.add_parser("stop", help="stop Firecrawl without deleting state")
    subparsers.add_parser("status", help="show container and readiness status")
    subparsers.add_parser("doctor", help="run complete functional and security checks")
    logs_parser = subparsers.add_parser("logs", help="show Firecrawl service logs")
    logs_parser.add_argument("--follow", action="store_true")
    logs_parser.add_argument("--tail", type=int, default=160)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manager = FirecrawlManager()
    try:
        if args.command == "start":
            result = manager.start()
            print(result.detail)
        elif args.command == "stop":
            manager.stop()
        elif args.command == "status":
            result = manager.status()
            print(result.detail)
            return 0 if result.ready else 1
        elif args.command == "doctor":
            result = manager.doctor()
            print(result.detail)
        else:
            manager.logs(follow=args.follow, tail=max(1, args.tail))
    except ProvisionError as exc:
        print(f"Firecrawl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
