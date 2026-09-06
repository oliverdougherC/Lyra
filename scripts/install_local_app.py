"""Install the verified local build as the single canonical Lyra app."""

from __future__ import annotations

import argparse
import fcntl
import plistlib
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

BACKEND = Path("Contents/Resources/resources/lyra-backend/lyra-backend")
SCRIPTS = Path(__file__).resolve().parent


def validate_bundle(app: Path) -> None:
    if app.is_symlink() or not app.is_dir() or app.suffix != ".app":
        raise ValueError(f"Expected an app directory, not a symlink: {app}")
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != "com.lyra.desktop":
        raise ValueError(f"Unexpected bundle identity: {app}")
    executable = info.get("CFBundleExecutable", "")
    if not executable or Path(executable).name != executable:
        raise ValueError(f"Invalid bundle executable: {app}")
    for path in (app / "Contents/MacOS" / executable, app / BACKEND):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing bundled executable: {path}")


def run(*args: str) -> None:
    subprocess.run(args, check=True)  # noqa: S603 — argument arrays, no shell


def verify_bundle(app: Path) -> None:
    validate_bundle(app)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    # Includes resource-located native objects that codesign --deep may not visit.
    run(sys.executable, str(SCRIPTS / "verify_macos_bundle.py"), str(app))
    run(sys.executable, str(SCRIPTS / "frozen_backend_smoke.py"), str(app / BACKEND))


def assert_not_running() -> None:
    processes = subprocess.check_output(  # noqa: S603
        ["/bin/ps", "-axww", "-o", "comm="], text=True
    )
    if any("Lyra.app/Contents/" in command for command in processes.splitlines()):
        raise RuntimeError("Quit every running Lyra app before installing the new build.")


def copy_bundle(source: Path, destination: Path) -> None:
    run("/usr/bin/ditto", str(source), str(destination))


@contextmanager
def installation_lock(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Retain this inode between installs: unlinking a lock allows concurrent owners.
    lock_path = destination.parent / f".{destination.name}.install.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Another installer is updating {destination}") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def install(source: Path, destination: Path = Path("/Applications/Lyra.app")) -> None:
    with installation_lock(destination.absolute()):
        install_locked(source, destination)


def install_locked(source: Path, destination: Path = Path("/Applications/Lyra.app")) -> None:
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("App source and destination must not be symlinks")
    source = source.resolve(strict=True)
    destination = destination.absolute()
    resolved_destination = destination.resolve()
    if (
        source == resolved_destination
        or source in resolved_destination.parents
        or (resolved_destination in source.parents)
    ):
        raise ValueError("Source and destination must be separate app bundles")
    validate_bundle(source)
    if destination.exists():
        validate_bundle(destination)
    assert_not_running()
    verify_bundle(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".lyra-install-", suffix=".noindex", dir=destination.parent)
    )
    staged = staging / "Lyra.app"
    rollback = staging / "previous.app"
    installed = False
    try:
        copy_bundle(source, staged)
        verify_bundle(staged)
        assert_not_running()
        if destination.exists():
            destination.rename(rollback)
        try:
            staged.rename(destination)
            installed = True
            verify_bundle(destination)
        except BaseException:
            if installed:
                shutil.rmtree(destination)
            if rollback.exists():
                rollback.rename(destination)
            raise
        # Keep the prior app until the installed signed backend has passed smoke.
        if rollback.exists():
            shutil.rmtree(rollback)
        shutil.rmtree(source)
    finally:
        # If rollback itself failed, retain the previous app for manual recovery.
        if not rollback.exists():
            shutil.rmtree(staging)
    print(f"Installed and verified {destination}; consumed build output {source}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--destination", type=Path, default=Path("/Applications/Lyra.app"))
    parser.add_argument("--open", action="store_true", help="Open the verified installed app")
    args = parser.parse_args()
    try:
        install(args.app, args.destination)
        if args.open:
            run("/usr/bin/open", str(args.destination.absolute()))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Local installation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
