#!/usr/bin/env python3
"""Exercise a selected frozen binary in a disposable profile with no developer PATH."""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import secrets
import select
import signal
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root):
    return {
        str(p.relative_to(root)): digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and p.name != "lyra.db-shm"
        and not (p.name == "lyra.db-wal" and p.stat().st_size == 0)
    }


def isolated_environment(root):
    """Do not inherit provider credentials, source paths, or Python loader overrides."""
    root = root.resolve(strict=True)
    data = root / "profile"
    env = {
        name: os.environ[name]
        for name in ("HOME", "TMPDIR", "LANG", "LC_ALL")
        if name in os.environ
    }
    env.update(
        {
            "PATH": "/usr/bin:/bin",
            "LYRA_PACKAGED": "1",
            "LYRA_DATA_DIR": str(data),
            "LYRA_DB_PATH": str(data / "lyra.db"),
            "LYRA_CACHE_DIR": str(root / "cache"),
            "LYRA_LOGS_DIR": str(root / "logs"),
            "LYRA_MODELS_DIR": str(root / "models"),
            "HF_HUB_OFFLINE": "1",
            "HF_HOME": str(root / "huggingface-cache"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
    )
    return env


def assert_child_isolation(root, env):
    """Fail before each startup, backup, restore, and recovery child if selectors drift."""
    root = root.resolve(strict=True)
    for name in (
        "LYRA_DATA_DIR",
        "LYRA_DB_PATH",
        "LYRA_CACHE_DIR",
        "LYRA_LOGS_DIR",
        "LYRA_MODELS_DIR",
        "HF_HOME",
    ):
        path = Path(env.get(name, ""))
        if not path.is_absolute() or not path.resolve().is_relative_to(root):
            raise ValueError("Child mutable path escapes disposable root: " + name)
    if Path(env["LYRA_DB_PATH"]) != Path(env["LYRA_DATA_DIR"]) / "lyra.db":
        raise ValueError("Child database must use the disposable profile layout")
    if env.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.null.Keyring":
        raise ValueError("Child may not use the host Keychain")
    if env.get("LYRA_PACKAGED") != "1" or env.get("PATH") != "/usr/bin:/bin":
        raise ValueError("Child must use the frozen packaged environment")
    if any(
        name in env
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "LYRA_SOURCE_DATA_DIR",
            "LYRA_SOURCE_DB_PATH",
            "LYRA_RESOURCE_ROOT",
        )
    ):
        raise ValueError("Child contains an ambient runtime override")


def read_readiness(stream, timeout=45):
    deadline = time.monotonic() + timeout
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise RuntimeError("Frozen startup exceeded readiness deadline")
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            raise RuntimeError("Frozen startup exited before readiness")
        payload.extend(chunk)
        if len(payload) > 8192:
            raise RuntimeError("Frozen readiness exceeded 8 KiB")
        if b"\n" in payload:
            return json.loads(payload.split(b"\n", 1)[0])


def stop_owned_group(child):
    # Only this harness's newly created session is signaled, while its leader remains
    # unreaped; no PID-file lookup or foreign process discovery is used here.
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def start_and_stop(binary, root, env, expected_original_hash=None):
    assert_child_isolation(root, env)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    token = secrets.token_hex(32)
    port = listener.getsockname()[1]
    with (root / "process-stderr.log").open("ab") as error:
        child = subprocess.Popen(  # noqa: S603 - explicit owner-selected frozen artifact
            [str(binary)],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=error,
            text=True,
            pass_fds=(listener.fileno(),),
            start_new_session=True,
        )
        try:
            child.stdin.write(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "socket_fd": listener.fileno(),
                        "parent_pid": os.getpid(),
                        "listener_addr": f"127.0.0.1:{port}",
                        "session_header_name": "X-Lyra-Session",
                        "session_secret": token,
                    }
                )
                + "\n"
            )
            child.stdin.close()
            ready = read_readiness(child.stdout)
            if not (ready["status"] == "ready" and ready["session_secret"] == token):
                raise AssertionError("Frozen acceptance assertion failed")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/health/live", headers={"X-Lyra-Session": token})
            response = conn.getresponse()
            if not (response.status == 200):
                raise AssertionError("Frozen acceptance assertion failed")
            response.read()
            conn.close()
            if expected_original_hash is not None:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(
                    "GET", "/api/documents/9001/original", headers={"X-Lyra-Session": token}
                )
                response = conn.getresponse()
                if not (response.status == 200):
                    raise AssertionError(("original-document-status", response.status))
                if not (hashlib.sha256(response.read()).hexdigest() == expected_original_hash):
                    raise AssertionError("Frozen acceptance assertion failed")
                conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST",
                "/api/health/shutdown",
                headers={"X-Lyra-Session": token, "X-Lyra-Client": "desktop"},
            )
            response = conn.getresponse()
            if not (response.status == 202):
                raise AssertionError(response.status)
            response.read()
            conn.close()
            child.wait(timeout=15)
            if not (child.returncode == 0):
                raise AssertionError("Frozen acceptance assertion failed")
        finally:
            stop_owned_group(child)
            listener.close()


def helper(binary, operation, path, root, env, expected):
    assert_child_isolation(root, env)
    completed = subprocess.run(  # noqa: S603 - explicit owner-selected frozen artifact
        [str(binary), "--desktop-backup-" + operation, str(path)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if not (completed.returncode == expected):
        raise AssertionError(
            (
                operation,
                completed.returncode,
                completed.stdout[:300],
                completed.stderr[:500],
            )
        )
    return json.loads(completed.stdout)


def profile_context(explicit_root, keep_profile):
    if explicit_root is not None:
        root = explicit_root.expanduser().absolute()
        if root.resolve() != root:
            raise ValueError("Explicit profile root may not contain symlink ancestors")
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise ValueError("Explicit profile root must be a new or empty real directory")
        if root.is_relative_to(Path.cwd().resolve()):
            raise ValueError("Acceptance profile must be outside the source checkout")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return contextlib.nullcontext(str(root))
    if keep_profile:
        return contextlib.nullcontext(tempfile.mkdtemp(prefix="lyra-packaged-ui-"))
    return tempfile.TemporaryDirectory(prefix="lyra-packaged-backup-")


def run(binary, *, explicit_root=None, keep_profile=False, prepare_only=False):
    binary = binary.resolve(strict=True)
    started = time.monotonic()
    keep_profile = keep_profile or explicit_root is not None or prepare_only
    with profile_context(explicit_root, keep_profile) as temporary:
        root = Path(temporary)
        data = root / "profile"
        env = isolated_environment(root)
        start_and_stop(binary, root, env)
        (data / "uploads").mkdir(exist_ok=True)
        (data / "text").mkdir(exist_ok=True)
        (data / "uploads" / "9001").mkdir()
        source = data / "uploads" / "9001" / "9001-synthetic-source.txt"
        source.write_text("Synthetic source: mitochondria produce ATP.\n")
        (data / "text" / "synthetic-source.txt").write_text("Synthetic extracted source.\n")
        db = data / "lyra.db"
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("insert into classes(id,name) values(9001,'Backup acceptance')")
            conn.execute(
                "insert into documents(id,class_id,filename,stored_path,mime,"
                "byte_size,state) values(9001,9001,'synthetic-source.txt',?,'"
                "text/plain',?,'ready')",
                (str(source), source.stat().st_size),
            )
            conn.execute(
                "insert into artifacts(id,class_id,kind,title,state) values(9"
                "001,9001,'draft','Synthetic essay','ready')"
            )
            conn.execute(
                "insert into artifacts(id,class_id,kind,title,state) values(9"
                "002,9001,'flashcard_deck','Synthetic deck','ready')"
            )
            conn.execute(
                "insert into artifact_parts(id,artifact_id,kind,ordinal,conte"
                "nt,status) values(9001,9001,'draft_body',0,'Original saved e"
                "ssay','complete')"
            )
            conn.execute(
                "insert into artifact_parts(id,artifact_id,kind,ordinal,conte"
                "nt,content_type,status) values(9002,9002,'card',0,?,'json','"
                "complete')",
                (json.dumps({"front": "Where is ATP produced?", "back": "Mitochondria"}),),
            )
            conn.execute(
                "insert into artifact_sources(artifact_id,document_id,role,or"
                "dinal) values(9002,9001,'study_source',0)"
            )
            conn.execute(
                "update settings set endpoint_url='http://127.0.0.1:9998/v1',"
                " model='synthetic-old' where id=1"
            )
        if prepare_only:
            with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                conn.execute(
                    "update settings set endpoint_url=null, model=null, tutor_cre"
                    "dential_id=null, legacy_credential_endpoint=null, remote_ack"
                    "=0, extraction_enabled=0 where id=1"
                )
                conn.execute(
                    "update artifact_parts set content='# Synthetic essay for nat"
                    "ive print\n\nThis saved essay and its source exist only in a d"
                    "isposable acceptance profile.\n\nMitochondria produce ATP; the"
                    " attached synthetic source supports this sentence.' where id"
                    "=9001"
                )
            return {
                "status": "prepared",
                "binary": str(binary),
                "binary_sha256": digest(binary),
                "profile_root": str(root),
                "data_dir": str(data),
                "draft_route": "/classes/9001/drafts/9001",
                "class_route": "/classes/9001",
                "draft_title": "Synthetic essay",
                "keyring": "null",
                "endpoint_configured": False,
                "credentials_created": False,
                "launch_environment": {
                    key: value
                    for key, value in env.items()
                    if key.startswith("LYRA_")
                    or key in ("PYTHON_KEYRING_BACKEND", "HF_HUB_OFFLINE", "HF_HOME")
                },
                "cleanup": (
                    "After quitting the isolated app, remove only this generated profile_root."
                ),
            }
        original_source = digest(source)
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            original_parts = conn.execute(
                "select id,kind,content from artifact_parts where id>=9001 order by id"
            ).fetchall()
            schema_version = conn.execute("pragma user_version").fetchone()[0]
        archive = root / "backup.tar.gz"
        holder = sqlite3.connect(db)
        holder.execute("select count(*) from artifact_parts").fetchone()
        try:
            with contextlib.closing(sqlite3.connect(db)) as writer, writer:
                writer.execute(
                    "update artifact_parts set content='Original essay committed "
                    "in WAL' where id=9001"
                )
            if not ((data / "lyra.db-wal").stat().st_size > 0):
                raise AssertionError("Frozen acceptance assertion failed")
            original_parts = holder.execute(
                "select id,kind,content from artifact_parts where id>=9001 order by id"
            ).fetchall()
            if not (helper(binary, "create", archive, root, env, 0)["status"] == "created"):
                raise AssertionError("Frozen acceptance assertion failed")
        finally:
            holder.close()
        with tarfile.open(archive, "r:gz") as bundle:
            if {"data/lyra.db-wal", "data/lyra.db-shm"}.intersection(bundle.getnames()):
                raise AssertionError("Frozen acceptance assertion failed")
        archive_hash = digest(archive)
        if not (archive.stat().st_mode & 0o777 == 0o600):
            raise AssertionError("Frozen acceptance assertion failed")
        helper(binary, "create", archive, root, env, 1)
        if not (digest(archive) == archive_hash):
            raise AssertionError("Frozen acceptance assertion failed")
        source.write_text("Newer current source that must be retained.\n")
        current_source = digest(source)
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("update artifact_parts set content='Newer current essay' where id=9001")
            conn.execute(
                "update settings set endpoint_url='http://127.0.0.1:9999/v1',"
                " model='synthetic-current' where id=1"
            )
        restored = helper(binary, "restore", archive, root, env, 0)
        if not (restored["status"] == "restored"):
            raise AssertionError("Frozen acceptance assertion failed")
        previous = root / restored["label"]
        if not (previous.is_dir()):
            raise AssertionError("Frozen acceptance assertion failed")
        if not (digest(source) == original_source):
            raise AssertionError("Frozen acceptance assertion failed")
        if not (
            digest(previous / "uploads" / "9001" / "9001-synthetic-source.txt") == current_source
        ):
            raise AssertionError("Frozen acceptance assertion failed")
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            if not (
                conn.execute(
                    "select id,kind,content from artifact_parts where id>=9001 order by id"
                ).fetchall()
                == original_parts
            ):
                raise AssertionError("Frozen acceptance assertion failed")
            if not (
                conn.execute("select endpoint_url,model from settings where id=1").fetchone()
                == ("http://127.0.0.1:9999/v1", "synthetic-current")
            ):
                raise AssertionError("Frozen acceptance assertion failed")
        with contextlib.closing(sqlite3.connect(previous / "lyra.db")) as conn, conn:
            if not (
                conn.execute("select content from artifact_parts where id=9001").fetchone()[0]
                == "Newer current essay"
            ):
                raise AssertionError("Frozen acceptance assertion failed")
        # Reopen the populated restored profile using the same frozen application protocol.
        start_and_stop(binary, root, env)
        future_stage = root / "future"
        future_stage.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(future_stage, filter="data")
        with contextlib.closing(sqlite3.connect(future_stage / "data" / "lyra.db")) as conn, conn:
            conn.execute("pragma user_version=99999")
        future_archive = root / "future.tar.gz"
        with tarfile.open(future_archive, "w:gz") as bundle:
            bundle.add(future_stage / "manifest.json", arcname="manifest.json")
            bundle.add(future_stage / "data", arcname="data")
        before = tree_digest(data)
        helper(binary, "restore", future_archive, root, env, 1)
        after = tree_digest(data)
        if not (after == before):
            raise AssertionError(
                {
                    name: {"before": before.get(name), "after": after.get(name)}
                    for name in before.keys() | after.keys()
                    if before.get(name) != after.get(name)
                }
            )
        corrupt_archive = root / "corrupt.tar.gz"
        corrupt_archive.write_bytes(b"synthetic invalid backup archive")
        before = tree_digest(data)
        helper(binary, "restore", corrupt_archive, root, env, 1)
        if tree_digest(data) != before:
            raise AssertionError("Corrupt archive changed the live profile")
        relocated = root / "relocated-profile"
        moved_env = dict(env, LYRA_DATA_DIR=str(relocated), LYRA_DB_PATH=str(relocated / "lyra.db"))
        start_and_stop(binary, root, moved_env)
        if not (helper(binary, "restore", archive, root, moved_env, 0)["status"] == "restored"):
            raise AssertionError("Frozen acceptance assertion failed")
        with contextlib.closing(sqlite3.connect(relocated / "lyra.db")) as conn, conn:
            stored = Path(
                conn.execute("select stored_path from documents where id=9001").fetchone()[0]
            )
        if not (stored.is_relative_to(relocated / "uploads")):
            raise AssertionError(
                "Cross-profile restore kept a source path outside the restored profile"
            )
        if not (digest(stored) == original_source):
            raise AssertionError("Frozen acceptance assertion failed")
        start_and_stop(binary, root, moved_env, expected_original_hash=original_source)
        return {
            "status": "passed",
            "binary": str(binary),
            "binary_sha256": digest(binary),
            "path": env["PATH"],
            "pythonpath_removed": "PYTHONPATH" not in env,
            "outside_checkout": True,
            "keyring": "null",
            "schema_version": schema_version,
            "profile_root": str(root) if keep_profile else None,
            "future_refusal_sidecars": {
                name: (data / name).stat().st_size
                for name in ("lyra.db-wal", "lyra.db-shm")
                if (data / name).exists()
            },
            "no_durable_data_mutation": True,
            "archive_staged_sidecars_absent": True,
            "committed_source_wal_preserved": True,
            "archive_sha256": archive_hash,
            "source_sha256": original_source,
            "checks": [
                "fresh-frozen-startup",
                "populated-archive-create",
                "existing-output-refused",
                "draft-deck-source-roundtrip",
                "current-settings-preserved",
                "prior-profile-retained",
                "restored-profile-frozen-relaunch",
                "future-archive-refused-without-live-mutation",
                "corrupt-archive-refused-without-live-mutation",
                "cross-profile-original-path-rebased",
                "cross-profile-original-authenticated-download",
            ],
            "seconds": round(time.monotonic() - started, 2),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-profile", action="store_true")
    parser.add_argument("--profile-root", type=Path)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Keep a populated UI fixture with no endpoint or credentials",
    )
    args = parser.parse_args()
    result = run(
        args.binary,
        explicit_root=args.profile_root,
        keep_profile=args.keep_profile,
        prepare_only=args.prepare_only,
    )
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)
