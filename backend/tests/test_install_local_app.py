"""A local install replaces only the app and consumes its verified build output."""

from __future__ import annotations

import importlib.util
import plistlib
import shutil
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "install_local_app", Path(__file__).resolve().parents[2] / "scripts/install_local_app.py"
)
assert _SPEC is not None and _SPEC.loader is not None
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


def bundle(path: Path, marker: str = "new") -> Path:
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.lyra.desktop",
                "CFBundleExecutable": "lyra",
            }
        )
    )
    (path / "Contents/MacOS/lyra").write_text(marker)
    backend = path / installer.BACKEND
    backend.parent.mkdir(parents=True)
    backend.write_text(marker)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    source = bundle(tmp_path / "build/Lyra.app")
    destination = bundle(tmp_path / "Applications/Lyra.app", "old")
    monkeypatch.setattr(installer, "verify_bundle", lambda path: installer.validate_bundle(path))
    monkeypatch.setattr(installer, "assert_not_running", lambda: None)
    monkeypatch.setattr(installer, "copy_bundle", shutil.copytree)
    return source, destination


def test_rejects_unrelated_bundle_before_replacement(installation):
    source, destination = installation
    (source / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.example.unrelated",
                "CFBundleExecutable": "lyra",
            }
        )
    )
    with pytest.raises(ValueError, match="identity"):
        installer.install(source, destination)
    assert (destination / "Contents/MacOS/lyra").read_text() == "old"


def test_refuses_running_copy(installation, monkeypatch):
    source, destination = installation

    def running():
        raise RuntimeError("Quit Lyra")

    monkeypatch.setattr(installer, "assert_not_running", running)
    with pytest.raises(RuntimeError, match="Quit Lyra"):
        installer.install(source, destination)
    assert source.exists()
    assert (destination / "Contents/MacOS/lyra").read_text() == "old"


@pytest.mark.parametrize("failure", ["copy", "replace", "verify"])
def test_failure_preserves_source_and_previous_app(installation, monkeypatch, failure):
    source, destination = installation
    if failure == "copy":
        monkeypatch.setattr(
            installer, "copy_bundle", lambda *args: (_ for _ in ()).throw(OSError("copy failed"))
        )
    elif failure == "replace":
        original = Path.rename

        def rename(path, target):
            if path.name == "Lyra.app" and path.parent.name.endswith(".noindex"):
                raise OSError("replace failed")
            return original(path, target)

        monkeypatch.setattr(Path, "rename", rename)
    else:

        def verify(path):
            if path == destination:
                raise OSError("verify failed")
            installer.validate_bundle(path)

        monkeypatch.setattr(installer, "verify_bundle", verify)
    with pytest.raises(OSError, match="failed"):
        installer.install(source, destination)
    assert source.exists()
    assert (destination / "Contents/MacOS/lyra").read_text() == "old"
    assert set(destination.parent.iterdir()) == {
        destination,
        destination.parent / ".Lyra.app.install.lock",
    }


def test_success_consumes_source_preserves_unrelated_data(installation):
    source, destination = installation
    data = destination.parent / "user-data.sqlite"
    data.write_text("precious")
    installer.install(source, destination)
    assert not source.exists()
    assert (destination / "Contents/MacOS/lyra").read_text() == "new"
    assert data.read_text() == "precious"
    assert set(destination.parent.iterdir()) == {
        destination,
        data,
        destination.parent / ".Lyra.app.install.lock",
    }


def test_refuses_symlink_destination(installation, tmp_path):
    source, destination = installation
    alias = tmp_path / "Alias.app"
    alias.symlink_to(destination)
    with pytest.raises(ValueError, match="symlink"):
        installer.install(source, alias)
    assert source.exists()


@pytest.mark.parametrize(
    "process",
    [
        "/Applications/Lyra.app/Contents/MacOS/lyra",
        "/Users/example/build/Lyra.app/Contents/Resources/resources/lyra-backend/lyra-backend",
    ],
)
def test_detects_running_app_or_bundled_backend(monkeypatch, process):
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *a, **kw: process)
    with pytest.raises(RuntimeError, match="Quit"):
        installer.assert_not_running()


def test_verification_covers_signature_native_objects_and_frozen_smoke(tmp_path, monkeypatch):
    app = bundle(tmp_path / "Lyra.app")
    commands = []
    monkeypatch.setattr(installer, "run", lambda *args: commands.append(args))
    installer.verify_bundle(app)
    assert commands[0] == ("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    assert commands[1][1].endswith("verify_macos_bundle.py")
    assert commands[2][1].endswith("frozen_backend_smoke.py")
    assert commands[2][2] == str(app / installer.BACKEND)


def test_concurrent_installer_refuses_same_destination(installation):
    source, destination = installation
    with (
        installer.installation_lock(destination),
        pytest.raises(RuntimeError, match="Another installer"),
    ):
        installer.install(source, destination)
    assert source.exists()
    assert (destination / "Contents/MacOS/lyra").read_text() == "old"


def test_open_happens_only_after_success(installation, monkeypatch):
    source, destination = installation
    calls = []
    monkeypatch.setattr(
        installer.sys,
        "argv",
        ["install_local_app.py", str(source), "--destination", str(destination), "--open"],
    )

    def open_installed(*args):
        assert not source.exists()
        assert (destination / "Contents/MacOS/lyra").read_text() == "new"
        calls.append(args)

    monkeypatch.setattr(installer, "run", open_installed)
    assert installer.main() == 0
    assert calls == [("/usr/bin/open", str(destination))]
