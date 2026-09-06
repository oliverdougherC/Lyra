"""Local signing must preserve certificate identity and sign nested code first."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sign_local_app", Path(__file__).resolve().parents[2] / "scripts/sign_local_app.py"
)
assert _SPEC is not None and _SPEC.loader is not None
signer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(signer)

IDENTITIES = f'  1) {"A" * 40} "Apple Development: Example (TEAM)"\n'


def test_auto_selects_sole_development_identity() -> None:
    assert signer.select_identity(IDENTITIES, None) == "A" * 40


def test_explicit_identity_is_exact_and_must_be_valid() -> None:
    assert signer.select_identity(IDENTITIES, "a" * 40) == "A" * 40
    assert signer.select_identity(IDENTITIES, "Apple Development: Example (TEAM)") == "A" * 40
    with pytest.raises(ValueError):
        signer.select_identity(IDENTITIES, "-")
    with pytest.raises(ValueError):
        signer.select_identity(IDENTITIES, "Example")


def test_ambiguous_or_missing_identity_fails_without_adhoc_fallback() -> None:
    other = f'  2) {"B" * 40} "Apple Development: Other (TEAM)"\n'
    for output in ("", IDENTITIES + other):
        with pytest.raises(ValueError):
            signer.select_identity(output, None)


def test_targets_sign_inside_out_without_following_symlinks(tmp_path: Path) -> None:
    app = tmp_path / "Lyra.app"
    nested = app / "Contents/Helpers/Helper.app"
    binary = nested / "Contents/MacOS/helper"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"code")
    (app / "alias").symlink_to(binary)
    (app / "data.txt").write_text("plain data")
    assert signer.signing_targets(app) == [binary, nested, app]


def test_rejects_hash_bound_designated_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signer, "run", lambda *args: 'designated => cdhash H"abcd"')
    with pytest.raises(ValueError):
        signer.verify_stable_requirement(Path("backend"), "com.lyra.desktop.backend")
    requirement = (
        'designated => identifier "com.lyra.desktop.backend" '
        "and anchor apple generic and certificate leaf[subject.OU] = TEAM"
    )
    monkeypatch.setattr(signer, "run", lambda *args: requirement)
    assert (
        signer.verify_stable_requirement(Path("backend"), "com.lyra.desktop.backend") == requirement
    )
