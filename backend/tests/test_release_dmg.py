"""The public DMG must contain precisely the verified, runnable source bundle."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release import verify_dmg  # noqa: E402


@pytest.fixture
def image_setup(tmp_path, monkeypatch):
    app = tmp_path / "source" / "Lyra.app"
    executable = app / "Contents/Resources/resources/lyra-backend/lyra-backend"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable")
    executable.chmod(0o755)
    (app / "link").symlink_to("Contents")
    image = tmp_path / "Lyra.dmg"
    image.write_bytes(b"synthetic image")
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    monkeypatch.setattr(verify_dmg.tempfile, "mkdtemp", lambda **kwargs: str(mountpoint))
    calls = []

    def run(*args):
        calls.append(args)
        if args[1] == "attach":
            shutil.copytree(app, mountpoint / app.name, symlinks=True)
        if args[1] == "detach":
            shutil.rmtree(mountpoint / app.name)
        return ""

    monkeypatch.setattr(verify_dmg, "run", run)
    inspected = []
    smoked = []
    monkeypatch.setattr(
        verify_dmg,
        "inspect_bundle",
        lambda path: inspected.append(path) or {"hardened_runtime": True},
    )
    monkeypatch.setattr(
        verify_dmg,
        "run_smoke",
        lambda path: smoked.append(path) or {"status": "passed"},
    )
    return image, app, mountpoint, calls, run, inspected, smoked


def test_verifies_image_readback_signatures_and_mounted_backend(image_setup):
    image, app, mountpoint, calls, _, inspected, smoked = image_setup
    receipt = verify_dmg.verify_dmg(image, app)
    assert receipt["status"] == "passed"
    assert receipt["image_sha256"] == verify_dmg.file_digest(image)
    assert receipt["app_tree_entries"] == len(verify_dmg.app_tree(app))
    assert receipt["signing"]["hardened_runtime"] is True
    assert inspected == [mountpoint / app.name]
    assert smoked[0].is_relative_to(mountpoint)
    assert calls[0][1] == "verify"
    assert "-readonly" in calls[1] and "-nobrowse" in calls[1]
    assert calls[-1][1] == "detach"
    assert not mountpoint.exists()


@pytest.mark.parametrize("corruption", ["bytes", "symlink", "mode", "missing", "extra"])
def test_corrupt_packaged_app_is_rejected_and_detached(image_setup, monkeypatch, corruption):
    image, app, mountpoint, calls, original_run, inspected, _ = image_setup

    def run(*args):
        original_run(*args)
        if args[1] == "attach":
            copy = mountpoint / app.name
            executable = copy / "Contents/Resources/resources/lyra-backend/lyra-backend"
            if corruption == "bytes":
                executable.write_bytes(b"corrupt")
            elif corruption == "symlink":
                (copy / "link").unlink()
                (copy / "link").symlink_to("Elsewhere")
            elif corruption == "mode":
                executable.chmod(0o644)
            elif corruption == "missing":
                executable.unlink()
            else:
                (copy / "extra").write_text("unexpected")
        return ""

    monkeypatch.setattr(verify_dmg, "run", run)
    with pytest.raises(ValueError, match="differs from signed source"):
        verify_dmg.verify_dmg(image, app)
    assert not inspected
    assert calls[-1][1] == "detach"
    assert not mountpoint.exists()


def test_invalid_image_fails_before_attach(image_setup, monkeypatch):
    image, app, mountpoint, calls, _, _, _ = image_setup

    def fail(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(verify_dmg, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        verify_dmg.verify_dmg(image, app)
    assert [call[1] for call in calls] == ["verify"]
    assert mountpoint.exists()  # test fixture; production has not created it yet


@pytest.mark.parametrize("partial_mount", [False, True])
def test_attach_failure_cleans_up_partial_mount(image_setup, monkeypatch, partial_mount):
    image, app, mountpoint, calls, original_run, _, _ = image_setup
    monkeypatch.setattr(verify_dmg.os.path, "ismount", lambda path: partial_mount)

    def run(*args):
        if args[1] == "attach":
            if partial_mount:
                original_run(*args)
            raise subprocess.CalledProcessError(1, args)
        return original_run(*args)

    monkeypatch.setattr(verify_dmg, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        verify_dmg.verify_dmg(image, app)
    assert any(call[1] == "detach" for call in calls) is partial_mount
    assert not mountpoint.exists()


@pytest.mark.parametrize("failure", ["signing", "smoke", "detach"])
def test_verification_failures_detach_but_failed_detach_preserves_mount(
    image_setup,
    monkeypatch,
    failure,
):
    image, app, mountpoint, calls, original_run, _, _ = image_setup

    def fail(*args):
        raise ValueError("synthetic failure")

    if failure == "signing":
        monkeypatch.setattr(verify_dmg, "inspect_bundle", fail)
    elif failure == "smoke":
        monkeypatch.setattr(verify_dmg, "run_smoke", fail)
    else:

        def run(*args):
            if args[1] == "detach":
                calls.append(args)
                raise ValueError("synthetic failure")
            return original_run(*args)

        monkeypatch.setattr(verify_dmg, "run", run)
    with pytest.raises(ValueError, match="synthetic failure"):
        verify_dmg.verify_dmg(image, app)
    assert calls[-1][1] == "detach"
    assert mountpoint.exists() is (failure == "detach")
    if failure == "detach":
        assert (mountpoint / app.name / "Contents").exists()


def test_detach_may_remove_mountpoint_itself(image_setup, monkeypatch):
    image, app, mountpoint, _, original_run, _, _ = image_setup

    def run(*args):
        original_run(*args)
        if args[1] == "detach":
            mountpoint.rmdir()
        return ""

    monkeypatch.setattr(verify_dmg, "run", run)
    assert verify_dmg.verify_dmg(image, app)["status"] == "passed"
    assert not mountpoint.exists()
