"""Filesystem permissions for Lyra-owned state.

Lyra's local-first promise covers everything under the data directory: the uploads a
course came from, the extracted text and rendered page and figure caches derived from
them, chat and draft state, the SQLite database and its journal, and - when the OS
keychain is unavailable - the fallback API-key file. All of it is private to the user who
runs Lyra.

The umask the process happens to inherit is not a safe basis for that privacy. A permissive
umask, or a data directory placed inside a group-readable parent, would otherwise leave
Lyra's files readable more broadly than the promise allows. These helpers set the modes
explicitly, so the result never depends on the umask.

The contract:

- **Lyra-owned directories are `0o700`.** This is the load-bearing control: a directory
  another user cannot enter hides every file beneath it, whatever those files' own modes
  are, and it leaves the owner's execute bit intact so a bundled binary under `models/`
  still runs.
- **Sensitive Lyra-owned files are `0o600`**, as defence in depth and - for the backup
  archive, which lives outside the data tree - as the only control. Files are not tightened
  blanket-wide, because `models/` holds an executable that must keep its owner execute bit.

Modes are POSIX. On Windows `chmod` only toggles the read-only bit; there Lyra relies on
the per-user location of the data directory, and these calls do no harm.

Attached external workspaces are deliberately out of scope. They are the user's own project
trees: Lyra reads and edits their files but never rewrites their permissions.
"""

import contextlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path

# The two modes the whole data tree is held to. Named so a caller states intent rather than
# an octal literal, and so there is one place to read the contract off in code.
DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_mkdir(path: Path) -> Path:
    """Create `path` and any missing parents, hardening only what this call creates.

    Parents that already existed are left untouched: a data directory may sit inside a
    user-chosen folder whose permissions are the user's to set, not Lyra's. Only the
    directories this call brings into being are Lyra's own, and only those are set to
    `0o700` - independent of the umask, because the explicit chmod follows the mkdir.
    """
    created: list[Path] = []
    probe = path
    while not probe.exists():
        created.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(created):
        harden_dir(directory)
    return path


def harden_dir(path: Path) -> None:
    """Set a directory to `0o700`, independent of the umask."""
    _chmod(path, DIR_MODE)


def harden_file(path: Path) -> None:
    """Set a file to `0o600`, independent of the umask."""
    _chmod(path, FILE_MODE)


def write_private_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path`, private from the first byte, replacing any existing file.

    The mode is on the `open` call, so a newly created file is never briefly world-readable.
    An existing file keeps whatever mode it had until the trailing `harden_file`, which is
    why the surrounding directory being `0o700` is the control that actually matters.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    harden_file(path)


def write_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path`, private from the first byte, replacing any existing file."""
    write_private_bytes(path, text.encode(encoding))


def harden_data_tree(root: Path, *, keep_file_modes: Iterable[Path] = ()) -> None:
    """Bring an existing tree to the contract in place, tightening only where needed.

    Every directory under `root` is set to `0o700` and every file to `0o600`, except that
    files inside a `keep_file_modes` subtree are left alone - that is where the bundled
    executable lives, and a `0o600` file cannot be run. An entry already at or below the
    contract is not rewritten, so the walk touches only files that are genuinely too broad.

    Only entries under `root` are visited and symlinks are never followed, so a data tree
    that happens to contain a link out to an attached workspace cannot be reached through
    it. This is Lyra's one-time upgrade path for installations created before the contract
    existed; new files and directories are already created private at their source.
    """
    keep = tuple(Path(directory) for directory in keep_file_modes)
    _tighten(root, DIR_MODE)
    for parent, dirs, files in os.walk(root):
        parent_path = Path(parent)
        for name in dirs:
            _tighten(parent_path / name, DIR_MODE)
        if any(_is_within(parent_path, directory) for directory in keep):
            continue
        for name in files:
            _tighten(parent_path / name, FILE_MODE)


def _is_within(path: Path, ancestor: Path) -> bool:
    """Whether `path` is `ancestor` or sits beneath it."""
    return path == ancestor or ancestor in path.parents


def _tighten(path: Path, mode: int) -> None:
    """Drop any permission bit `mode` forbids, leaving a compliant entry untouched."""
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode):
        return
    if stat.S_IMODE(info.st_mode) & ~mode:
        _chmod(path, mode)


def _chmod(path: Path, mode: int) -> None:
    # A filesystem that does not carry POSIX modes - a mounted share, some Windows setups -
    # cannot be hardened this way. The data directory's own location is the isolation there;
    # Lyra does not crash over a chmod the platform ignores.
    with contextlib.suppress(OSError):
        os.chmod(path, mode)
