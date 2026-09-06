"""Publisher regressions: immutable channels and version-derived metadata."""

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from release_artifacts import (  # noqa: E402
    assemble,
    check_advance,
    checksum_payload,
    required_assets,
    sha256,
    site,
    validate,
)
from release_metadata import build_number, metadata, synchronize, version_parts  # noqa: E402


def test_beta_order_and_stable_transition():
    versions = ["0.1.0", "0.2.0-beta.0", "0.2.0-beta.1", "0.2.0-beta.10", "0.2.0", "0.2.1-beta.0"]
    assert sorted(versions, key=version_parts) == versions
    assert sorted(versions, key=lambda v: tuple(map(int, build_number(v).split(".")))) == versions


@pytest.mark.parametrize(
    "version", ["0.2.0-beta.98", "0.2.0-beta.01", "1.2.3-alpha.1", "99.0.0", "0.99.0"]
)
def test_invalid_or_exhausted_build_numbers_rejected(version):
    with pytest.raises(ValueError):
        metadata(version)


def test_synchronize_only_owned_package_metadata(tmp_path):
    for name in (
        "src-tauri/tauri.conf.json",
        "frontend/package.json",
        "src-tauri/Cargo.toml",
        "src-tauri/Cargo.lock",
        "pyproject.toml",
        "uv.lock",
    ):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((SCRIPTS.parent / name).read_bytes())
    synchronize(tmp_path, "0.2.0-beta.7")
    assert json.loads((tmp_path / "frontend/package.json").read_text())["version"] == "0.2.0-beta.7"
    assert 'version = "0.2.0b7"' in (tmp_path / "uv.lock").read_text()
    assert 'version = "0.2.0-beta.7"' in (tmp_path / "src-tauri/Cargo.lock").read_text()
    conf = json.loads((tmp_path / "src-tauri/tauri.conf.json").read_text())
    assert conf["bundle"]["macOS"]["bundleVersion"] == "3.0.8"
    assert conf["bundle"]["macOS"]["minimumSystemVersion"] == "14.0"
    assert 'VERSION = "0.2.0-beta.7"' in (tmp_path / "backend/version.py").read_text()


def fixture_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "oliverdougherC/Lyra")
    for name in required_assets("0.2.0-beta.1"):
        (tmp_path / name).write_text("fixture payload")
    (tmp_path / "distribution-signing.json").write_text(
        json.dumps({"mode": "ad-hoc", "developer_id_signed": False, "notarized": False})
    )
    (tmp_path / "frozen-smoke.json").write_text('{"status":"passed"}')
    (tmp_path / "native-inventory.json").write_text('{"status":"passed"}')
    (tmp_path / "updater-signature-verification.txt").write_text(
        "Actual updater archive accepted by the installed parser (1024 unpacked bytes).\n"
        "Updater archive signature verified against the retained Lyra public key.\n"
    )
    inner = {
        "version": "0.2.0-beta.1",
        "build": "3.0.2",
        "source": "a" * 40,
        "bundleIdentifier": "com.lyra.desktop",
        "architecture": "aarch64",
        "schemaMin": 0,
        "schemaMax": 44,
    }
    payload = json.dumps(inner).encode()
    with tarfile.open(tmp_path / "Lyra.app.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("Lyra.app/Contents/Resources/lyra-release.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    info = assemble(tmp_path, "0.2.0-beta.1", "a" * 40, 44)
    checksum_payload(tmp_path)
    return info


def test_beta_only_site_uses_versioned_urls(tmp_path, monkeypatch):
    fixture_assets(tmp_path, monkeypatch)
    site(tmp_path, tmp_path / "public")
    feed = json.loads((tmp_path / "public/beta/latest.json").read_text())
    assert feed["platforms"]["darwin-aarch64"]["url"].endswith("/v0.2.0-beta.1/Lyra.app.tar.gz")
    assert feed["lyra"]["size"] == (tmp_path / "Lyra.app.tar.gz").stat().st_size
    assert not (tmp_path / "public/stable").exists()
    assert "latest/download" not in (tmp_path / "public/beta/index.html").read_text()
    page = (tmp_path / "public/beta/index.html").read_text()
    assert "not notarized" in page
    assert "Privacy &amp; Security" in page
    assert "Open Anyway" in page


def test_ad_hoc_distribution_needs_no_apple_evidence(tmp_path, monkeypatch):
    info = fixture_assets(tmp_path, monkeypatch)
    assert validate(tmp_path) == info
    assert not any(
        "notarization" in name or "gatekeeper" in name for name in required_assets(info["version"])
    )


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"mode": "developer-id", "developer_id_signed": False, "notarized": False},
        {"mode": "ad-hoc", "developer_id_signed": True, "notarized": False},
        {"mode": "ad-hoc", "developer_id_signed": False, "notarized": True},
        {"mode": "ad-hoc", "developer_id_signed": 0, "notarized": False},
        {"mode": "ad-hoc", "developer_id_signed": False},
        [],
    ],
)
def test_invalid_distribution_receipt_rejected(tmp_path, monkeypatch, receipt):
    fixture_assets(tmp_path, monkeypatch)
    (tmp_path / "distribution-signing.json").write_text(json.dumps(receipt))
    checksum_payload(tmp_path)
    with pytest.raises(ValueError, match="Distribution signing"):
        validate(tmp_path)


def test_corrupt_asset_rejected(tmp_path, monkeypatch):
    fixture_assets(tmp_path, monkeypatch)
    (tmp_path / "Lyra.app.tar.gz").write_text("corrupt")
    with pytest.raises(ValueError, match="checksum"):
        validate(tmp_path)


def test_downgrade_collision_and_cross_channel_rejected():
    previous = {"version": "0.2.0-beta.2", "channel": "beta", "source": "a"}
    check_advance(previous, dict(previous))
    for candidate in (
        {**previous, "version": "0.2.0-beta.1"},
        {**previous, "source": "different"},
        {**previous, "channel": "stable"},
    ):
        with pytest.raises(ValueError):
            check_advance(previous, candidate)


def test_manifest_tamper_detected_by_payload_checksum(tmp_path, monkeypatch):
    fixture_assets(tmp_path, monkeypatch)
    feed = tmp_path / "latest.json"
    feed.write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        validate(tmp_path)


def test_signed_inner_contract_uses_exact_source_and_current_migrations(tmp_path):
    from release_metadata import write_bundle_contract

    app = tmp_path / "Lyra.app"
    (app / "Contents/Resources").mkdir(parents=True)
    write_bundle_contract(app, "0.2.0-beta.1", "a" * 40)
    contract = json.loads((app / "Contents/Resources/lyra-release.json").read_text())
    assert contract["version"] == "0.2.0-beta.1"
    assert contract["build"] == "3.0.2"
    assert contract["source"] == "a" * 40
    maximum = max(
        int(p.name.split("_", 1)[0])
        for p in (SCRIPTS.parent / "backend/storage/migrations").glob("*.sql")
    )
    assert contract["schemaMax"] == maximum
    with pytest.raises(ValueError, match="exact source"):
        write_bundle_contract(app, "0.2.0-beta.1", "main")


def test_partial_draft_retry_uploads_missing_identical_bytes_only(tmp_path, monkeypatch):
    import release_artifacts

    info = fixture_assets(tmp_path, monkeypatch)
    existing = tmp_path / "Lyra.app.tar.gz"
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return json.dumps(
                {
                    "draft": True,
                    "target_commitish": info["source"],
                    "assets": [{"name": existing.name, "digest": f"sha256:{sha256(existing)}"}],
                }
            )
        return ""

    monkeypatch.setattr(release_artifacts, "gh", fake_gh)
    release_artifacts.stage(tmp_path)
    uploads = [args[-1] for args in calls if args[0] == "release"]
    assert str(existing) not in uploads
    assert str(tmp_path / "latest.json") in uploads


def test_existing_draft_collision_refuses_replacement(tmp_path, monkeypatch):
    import release_artifacts

    info = fixture_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(
        release_artifacts,
        "gh",
        lambda *args: json.dumps(
            {
                "draft": True,
                "target_commitish": info["source"],
                "assets": [{"name": "Lyra.app.tar.gz", "digest": "sha256:different"}],
            }
        ),
    )
    with pytest.raises(ValueError, match="never overwrite"):
        release_artifacts.stage(tmp_path)


def test_complete_draft_retry_does_not_upload(tmp_path, monkeypatch):
    import release_artifacts

    info = fixture_assets(tmp_path, monkeypatch)
    calls = []

    def fake_gh(*args):
        calls.append(args)
        return json.dumps(
            {
                "draft": True,
                "target_commitish": info["source"],
                "assets": [
                    {"name": p.name, "digest": f"sha256:{sha256(p)}"}
                    for p in tmp_path.iterdir()
                    if p.is_file()
                ],
            }
        )

    monkeypatch.setattr(release_artifacts, "gh", fake_gh)
    release_artifacts.stage(tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize("name", sorted(required_assets("0.2.0-beta.1")))
def test_missing_asset_refuses_before_any_release_mutation(tmp_path, monkeypatch, name):
    import release_artifacts

    fixture_assets(tmp_path, monkeypatch)
    (tmp_path / name).unlink()
    calls = []
    monkeypatch.setattr(release_artifacts, "gh", lambda *args: calls.append(args))
    with pytest.raises((ValueError, OSError)):
        release_artifacts.stage(tmp_path)
    assert calls == []


def test_feed_cannot_redirect_even_with_recomputed_checksums(tmp_path, monkeypatch):
    fixture_assets(tmp_path, monkeypatch)
    path = tmp_path / "latest.json"
    feed = json.loads(path.read_text())
    feed["platforms"]["darwin-aarch64"]["url"] = "https://example.com/different.tar.gz"
    path.write_text(json.dumps(feed))
    checksum_payload(tmp_path)
    with pytest.raises(ValueError, match="Feed"):
        validate(tmp_path)


def test_unsigned_metadata_cannot_claim_new_schema_for_old_signed_archive(tmp_path, monkeypatch):
    fixture_assets(tmp_path, monkeypatch)
    for name in ("provenance.json", "latest.json"):
        path = tmp_path / name
        value = json.loads(path.read_text())
        (value["lyra"] if name == "latest.json" else value)["schemaMax"] = 45
        path.write_text(json.dumps(value))
    checksum_payload(tmp_path)
    with pytest.raises(ValueError, match="Signed inner contract"):
        validate(tmp_path)


def test_signature_only_receipt_cannot_skip_installed_archive_parser(tmp_path, monkeypatch):
    fixture_assets(tmp_path, monkeypatch)
    (tmp_path / "updater-signature-verification.txt").write_text(
        "Updater archive signature verified against the retained Lyra public key.\n"
    )
    checksum_payload(tmp_path)
    with pytest.raises(ValueError, match="actual archive"):
        validate(tmp_path)
