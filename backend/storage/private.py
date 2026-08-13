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

Symlinks are the boundary this module is careful about. Lyra owns a *root* - the data
directory - and everything it creates, writes, or hardens lives at or beneath that root.
The rule is:

- The root itself must be a real directory. A symlinked root is refused, not followed, so
  Lyra never recursively hardens or writes into whatever a symlinked data directory points
  at. `config` enforces this for `LYRA_DATA_DIR` and `database` for a symlinked
  `LYRA_DB_PATH`; `harden_data_tree` enforces it for the tree walk.
- No component *beneath* the root may be a symlink. Creation descends the tree component by
  component with `O_NOFOLLOW`, so a pre-existing symlink where Lyra expects to own a
  directory fails closed rather than redirecting creation outside the tree. Ancestors
  *above* the root are the user's own layout and may legitimately be symlinks; those are
  not inspected.
- A sensitive write opens its target with `O_NOFOLLOW` and hardens the returned descriptor
  with `fchmod`, never re-resolving the pathname, so it can neither follow nor truncate a
  symlink and never races a check against a use.
- Hardening chmods the file the descriptor names, never a symlink's target. Operational
  helpers (`harden_file`, `harden_dir`) fail closed on a symlink; the one-time migration
  walk leaves symlinks - and their targets - untouched, so an attached workspace linked
  into an old tree is never permission-rewritten.

Modes are POSIX. On Windows `chmod` only toggles the read-only bit and there is no
`O_NOFOLLOW`; there Lyra relies on the per-user location of the data directory, uses a
best-effort `lstat` check instead of the atomic no-follow open, and these calls do no harm.
On a POSIX filesystem that genuinely cannot carry modes (some network mounts report
`ENOTSUP`) a failed chmod is tolerated, because the data directory's location is the
isolation there; any *other* chmod failure is surfaced rather than leaving owned state
silently broad.

Attached external workspaces are deliberately out of scope. They are the user's own project
trees: Lyra reads and edits their files but never rewrites their permissions.
"""

import errno
import logging
import os
import stat
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# The two modes the whole data tree is held to. Named so a caller states intent rather than
# an octal literal, and so there is one place to read the contract off in code.
DIR_MODE = 0o700
FILE_MODE = 0o600

_POSIX = os.name == "posix"
# O_NOFOLLOW refuses to open a final path component that is a symlink (open raises ELOOP).
# It is present on POSIX and absent on Windows, where it and O_DIRECTORY are 0 and the
# lstat-guarded fallback below stands in.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# openat-style descent (dir_fd) is what makes the component-by-component creation race-free.
_HAS_OPENAT = _POSIX and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
# Errnos that mean "this filesystem does not implement POSIX modes", as opposed to a real
# failure to secure an owned path. Only these are tolerated; anything else fails closed.
_MODE_UNSUPPORTED = frozenset({errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)})


class PrivacyContractError(Exception):
    """A Lyra-owned path cannot be secured as the privacy contract requires.

    Raised when hardening or creating owned state would have to follow a symlink out of the
    data tree, or when a real chmod fails on a POSIX filesystem that carries modes. Failing
    closed here is the point: the alternative is a startup that reports success while having
    silently left coursework or the fallback key broader than the local-first promise, or
    chmodded a file outside the tree.
    """


def assert_not_symlink(path: Path, description: str) -> None:
    """Refuse a path that is a symlink, for state Lyra is expected to own outright.

    `lstat`-based, so it inspects the path itself and not its target. Used for the two
    entry points a user configures directly - the data root and an explicit database path -
    where following the link would let Lyra create through, or chmod, something outside the
    tree while reporting the configured path as secured.
    """
    if path.is_symlink():
        raise PrivacyContractError(
            f"{description} ({path}) is a symlink; point it at a real path so Lyra does not "
            f"read or modify the link's target while claiming the configured path is private"
        )


def regular_file_present(path: Path) -> bool:
    """Whether `path` is an existing regular file, without following a symlink.

    For security-relevant sentinel state: an absent path is simply not present, but a
    symlink (or any non-regular entry) where a plain marker file belongs is tampering, and
    is refused rather than trusted - otherwise a link could report state as recorded that
    was never written, or aim a later write at an outside file.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISREG(info.st_mode):
        return True
    raise PrivacyContractError(
        f"{path} exists but is not a regular file (it may be a symlink); refusing to trust "
        f"or overwrite it as Lyra-owned state"
    )


def secure_mkdir(path: Path, *, root: Path) -> Path:
    """Create `path` and any missing directories down from `root`, never through a symlink.

    `root` is the Lyra-owned boundary. Its own ancestors are the user's layout and are not
    inspected - they may be symlinks - but `root` and every component from `root` down to
    `path` must be a real directory. A component that already exists as a symlink fails
    closed, so an old tree that links a cache or upload directory out to another location
    can never redirect creation (or the hardening that follows it) outside the data tree.

    Directories this call brings into being are set to `0o700` explicitly, independent of
    the umask. Directories that already existed - including a pre-existing `root` - are left
    as they are; `config` re-hardens the top-level tree on every startup separately.
    """
    path = _abspath(path)
    root = _abspath(root)
    if path != root and root not in path.parents:
        raise ValueError(f"{path} is not within the owned root {root}")

    components = [root.name, *path.relative_to(root).parts]
    if not _HAS_OPENAT:
        return _secure_mkdir_fallback(root.parent, components, path)

    # Start from the root's parent - the user's territory, whose own symlinks are allowed -
    # and descend one component at a time, so O_NOFOLLOW guards every step at or below root.
    root.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(root.parent, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for name in components:
            child_fd, created = _mkdir_step(dir_fd, name, path)
            try:
                if created:
                    _fchmod_owned(child_fd, DIR_MODE, path)
            except BaseException:
                os.close(child_fd)
                raise
            os.close(dir_fd)
            dir_fd = child_fd
    finally:
        os.close(dir_fd)
    return path


def _mkdir_step(dir_fd: int, name: str, owned_path: Path) -> tuple[int, bool]:
    """Create `name` under `dir_fd` if missing, then open it refusing a symlink.

    Returns the open directory descriptor and whether this call created it. The open uses
    O_NOFOLLOW, so if `name` is (or is raced into) a symlink the step fails closed instead
    of descending into its target.
    """
    try:
        os.mkdir(name, DIR_MODE, dir_fd=dir_fd)
        created = True
    except FileExistsError:
        created = False
    try:
        child_fd = os.open(
            name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivacyContractError(
                f"a symlink is on the path to {owned_path}; refusing to create Lyra state "
                f"outside the data tree"
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise PrivacyContractError(
                f"a non-directory blocks creating {owned_path} inside the data tree"
            ) from exc
        raise
    return child_fd, created


def _secure_mkdir_fallback(base: Path, components: list[str], path: Path) -> Path:
    """secure_mkdir for platforms without openat (Windows).

    Best-effort: without O_NOFOLLOW the symlink refusal is an `lstat` check rather than an
    atomic open, so it carries a check/use race a local attacker could in principle win.
    Windows already lacks POSIX mode semantics, so the per-user data location is the real
    isolation there and this preserves the create-and-refuse-a-symlink shape.
    """
    base.mkdir(parents=True, exist_ok=True)
    current = base
    for name in components:
        current = current / name
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current)
            _harden(current, DIR_MODE, is_dir=True)
        else:
            if stat.S_ISLNK(info.st_mode):
                raise PrivacyContractError(
                    f"a symlink is on the path to {path}; refusing to create Lyra state "
                    f"outside the data tree"
                )
    return path


def harden_dir(path: Path) -> None:
    """Set a directory to `0o700`, without following a symlink at `path`.

    For a directory Lyra owns and expects to be real; a symlink here fails closed.
    """
    _harden(path, DIR_MODE, is_dir=True)


def harden_file(path: Path) -> None:
    """Set a file to `0o600`, without following a symlink at `path`.

    For a file Lyra owns and expects to be real - a rendered cache entry, the database and
    its sidecars - a symlink here fails closed rather than chmodding the link's target.
    """
    _harden(path, FILE_MODE, is_dir=False)


def write_private_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path`, private from the first byte, replacing any existing file.

    The file is opened with O_NOFOLLOW, so a symlink at `path` fails closed instead of
    being followed and its target truncated. The mode is on the `open` call, so a newly
    created file is never briefly world-readable, and an existing file that a prior run left
    broad is tightened by an `fchmod` on the same descriptor - never a re-resolved pathname,
    so there is no window and no second path lookup to race.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_CLOEXEC
    if not _POSIX:
        _refuse_existing_symlink(path)
    try:
        descriptor = os.open(path, flags, FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivacyContractError(
                f"{path} is a symlink; refusing to write Lyra-owned state through it"
            ) from exc
        raise
    owns_descriptor = False
    try:
        # Tighten an existing file that a prior run left broad, on the descriptor we already
        # hold. O_CREAT's mode applies only to a file this call creates.
        _fchmod_owned(descriptor, FILE_MODE, path)
        with os.fdopen(descriptor, "wb") as handle:
            owns_descriptor = True
            handle.write(data)
    finally:
        if not owns_descriptor:
            os.close(descriptor)


def write_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path`, private from the first byte, replacing any existing file."""
    write_private_bytes(path, text.encode(encoding))


def harden_data_tree(root: Path, *, keep_file_modes: Iterable[Path] = ()) -> None:
    """Bring an existing tree to the contract in place, tightening only where needed.

    Every directory under `root` is set to `0o700` and every file to `0o600`, except that
    files inside a `keep_file_modes` subtree are left alone - that is where the bundled
    executable lives, and a `0o600` file cannot be run. An entry already at or below the
    contract is not rewritten, so the walk touches only files that are genuinely too broad.

    Symlinks are never followed. `root` itself must be a real directory: a symlinked root is
    refused rather than walked, so its target is never traversed or chmodded. Beneath
    `root`, `os.walk(followlinks=False)` does not descend a symlinked directory and each
    entry is chmodded through a no-follow descriptor, so a link out to an attached workspace
    is left entirely untouched - neither the link nor its target is rewritten. This is
    Lyra's one-time upgrade path for installations created before the contract existed; new
    files and directories are already created private at their source.
    """
    assert_not_symlink(Path(root), "the data tree root")
    keep = tuple(_abspath(directory) for directory in keep_file_modes)
    _tighten(root, DIR_MODE)
    for parent, dirs, files in os.walk(root, followlinks=False):
        parent_path = _abspath(Path(parent))
        for name in dirs:
            _tighten(parent_path / name, DIR_MODE)
        if any(_is_within(parent_path, directory) for directory in keep):
            continue
        for name in files:
            _tighten(parent_path / name, FILE_MODE)


def is_within(path: Path, ancestor: Path) -> bool:
    """Whether `path` is `ancestor` or sits beneath it, compared as absolute paths.

    Used to decide whether a configured location (an explicit database path) falls inside
    the Lyra-owned tree, and so whether the owned-root creation guarantees apply to it.
    """
    return _is_within(_abspath(path), _abspath(ancestor))


def _is_within(path: Path, ancestor: Path) -> bool:
    """Whether `path` is `ancestor` or sits beneath it."""
    return path == ancestor or ancestor in path.parents


def _abspath(path: Path) -> Path:
    """Absolute, lexically normalized path - without resolving symlinks the way `resolve` would."""
    return Path(os.path.abspath(path))


def _tighten(path: Path, mode: int) -> None:
    """Drop any permission bit `mode` forbids from a real file or directory.

    A symlink - or anything that is not a plain file or directory - is left untouched, so
    the migration walk never rewrites a link or reaches its target. An entry already within
    the contract is not rewritten. A real chmod failure on a mode-carrying filesystem is
    surfaced, because the migration exists precisely to make old state private.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        return
    if stat.S_IMODE(info.st_mode) & ~mode == 0:
        return
    try:
        descriptor = _open_nofollow(path, is_dir=stat.S_ISDIR(info.st_mode))
    except OSError as exc:
        # Raced into a symlink or vanished between the lstat and the open: leave it. The
        # no-follow open is what guarantees we never chmod a link's target here.
        if exc.errno in (errno.ELOOP, errno.ENOENT):
            return
        raise
    try:
        _fchmod_owned(descriptor, mode, path)
    finally:
        os.close(descriptor)


def _harden(path: Path, mode: int, *, is_dir: bool) -> None:
    """Set `path` to `mode` without following a symlink, failing closed if it is one.

    For operational state Lyra owns and expects to be a real file or directory. The chmod
    goes through a no-follow descriptor, so it can neither follow a link nor race a check
    against the chmod.
    """
    if not _POSIX:
        _chmod_best_effort(path, mode)
        return
    try:
        descriptor = _open_nofollow(path, is_dir=is_dir)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivacyContractError(
                f"{path} is a symlink; refusing to chmod its target"
            ) from exc
        raise
    try:
        _fchmod_owned(descriptor, mode, path)
    finally:
        os.close(descriptor)


def _open_nofollow(path: Path, *, is_dir: bool) -> int:
    """Open `path` read-only without following a final-component symlink (ELOOP if it is one).

    O_NONBLOCK keeps the open from stalling on an unexpected fifo/device; fchmod on the
    returned descriptor still adjusts the entry the descriptor names.
    """
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK
    if is_dir:
        flags |= _O_DIRECTORY
    return os.open(path, flags)


def _fchmod_owned(descriptor: int, mode: int, path: Path) -> None:
    """fchmod a descriptor Lyra owns, tolerating only a mode-less filesystem.

    A filesystem that does not implement POSIX modes (some network mounts report ENOTSUP)
    cannot be hardened this way, and there the data directory's location is the isolation;
    that case is logged and tolerated. Every other failure - not the owner, read-only
    filesystem - is a real failure to secure owned state and is surfaced, so a startup never
    reports success while leaving the data broad-readable.
    """
    try:
        os.fchmod(descriptor, mode)
    except OSError as exc:
        if exc.errno in _MODE_UNSUPPORTED:
            logger.warning(
                "Filesystem for %s does not support POSIX modes; relying on the data "
                "directory location for privacy",
                path,
            )
            return
        raise PrivacyContractError(
            f"could not set mode {mode:#o} on {path}: {exc.strerror}"
        ) from exc


def _refuse_existing_symlink(path: Path) -> None:
    """Windows fallback guard for a sensitive write: refuse a symlink target up front."""
    if path.is_symlink():
        raise PrivacyContractError(
            f"{path} is a symlink; refusing to write Lyra-owned state through it"
        )


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Windows: chmod only toggles the read-only bit and there is no atomic no-follow open.

    Guard with an lstat check so a symlink is not chmodded through, then chmod by path. The
    per-user data location is the real isolation on Windows; this call does little more than
    avoid setting a read-only bit on a link's target.
    """
    try:
        if path.is_symlink():
            return
        os.chmod(path, mode)
    except OSError:
        return
