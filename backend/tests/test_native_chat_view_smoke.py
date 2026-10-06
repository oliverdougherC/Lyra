"""Exercise the production Objective-C view with AppKit and actual WebKit children."""

import os
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AppKit and WebKit")
def test_native_chat_view_smoke(tmp_path: Path) -> None:
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("requires the macOS developer tools")
    root = Path(__file__).resolve().parents[2]
    bundle = tmp_path / "NativeChatSmoke.app"
    executable = bundle / "Contents" / "MacOS" / "NativeChatSmoke"
    executable.parent.mkdir(parents=True)
    with (bundle / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": f"com.lyra.native-chat-smoke.{uuid.uuid4().hex}",
                "CFBundleExecutable": executable.name,
                "CFBundlePackageType": "APPL",
                "LSBackgroundOnly": True,
            },
            stream,
        )
    built = subprocess.run(  # noqa: S603 - fixed repo harness and temporary output
        [
            compiler,
            "-fobjc-arc",
            "-Wall",
            "-Wextra",
            "-framework",
            "AppKit",
            "-framework",
            "WebKit",
            "-framework",
            "ApplicationServices",
            str(root / "scripts" / "native_chat_view_smoke.m"),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    # The harness never launches Lyra, registers its IPC, or starts its backend.
    # WKWebsiteDataStore.nonPersistentDataStore keeps synthetic HTML out of any
    # persistent WebKit store. The app cannot activate or appear in the foreground.
    result = subprocess.run(  # noqa: S603 - freshly compiled repository-owned harness
        [str(executable)],
        env={**os.environ, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT failures=0" in result.stdout
