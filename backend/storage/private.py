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

import contextlib
import errno
import fnmatch
import logging
import os
import stat
import threading
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
# Bounded retry count for the EEXIST → ENOENT race in _open_or_create_nofollow. SQLite's
# transient WAL sidecars can disappear between the exclusive create seeing the entry and
# the fallback open, and a retry safely re-creates the file. Three retries give four total
# attempts — well above any legitimate concurrent-connection churn — without allowing a
# pathological path to spin indefinitely.
_SIDECAR_RACE_RETRIES = 3
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
    _assert_owned_entry(info, path, is_dir=False)
    return True


def ensure_private_file(path: Path) -> None:
    """Create or harden a regular Lyra-owned file without truncating or following links.

    New files are created exclusively at `0o600`, so they are private from their first
    byte. Existing files are opened with `O_NOFOLLOW`, checked through the returned
    descriptor, and hardened through that same descriptor. This is the preparation an
    external writer such as SQLite needs *before* it opens a predictable pathname itself.

    A bounded retry tolerates the race where another connection (typically SQLite managing
    its WAL sidecars) removes the file between the exclusive create seeing EEXIST and the
    fallback open. Only the specific EEXIST-then-ENOENT interleaving is retried; every
    other failure propagates immediately and symlink substitution remains fail-closed.
    """
    if not _POSIX:
        _refuse_existing_symlink(path)
    descriptor = _open_or_create_nofollow(path)
    try:
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=False)
        _fchmod_owned(descriptor, FILE_MODE, path)
    finally:
        os.close(descriptor)


def _open_or_create_nofollow(path: Path) -> int:
    """Open or exclusively create `path` without following symlinks, with bounded retry.

    Tolerates the race where another process removes the file between the exclusive
    create seeing EEXIST and the fallback open. Only FileNotFoundError after
    FileExistsError triggers a retry; every other failure propagates immediately.
    """
    flags = os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK
    for attempt in range(_SIDECAR_RACE_RETRIES + 1):
        try:
            return os.open(path, flags | os.O_CREAT | os.O_EXCL, FILE_MODE)
        except FileExistsError:
            try:
                return os.open(path, flags)
            except FileNotFoundError:
                if attempt == _SIDECAR_RACE_RETRIES:
                    raise
            except OSError as exc:
                _raise_unsafe_open(path, exc, operation="secure")
                raise
        except OSError as exc:
            _raise_unsafe_open(path, exc, operation="secure")
            raise
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))


def assert_safe_external_writer_parent(path: Path) -> None:
    """Require a POSIX directory where another user cannot swap prepared pathnames.

    Some libraries, notably SQLite, cannot accept an already-secured descriptor and must
    reopen predictable names themselves. No-follow preparation is only durable across that
    handoff when another OS user cannot unlink and replace entries in the containing
    directory. Owner-only or owner-writable `0755`-style directories are safe; a group- or
    world-writable parent is rejected without chmodding a directory the user owns.
    """
    if not _POSIX:
        return
    try:
        descriptor = _open_nofollow(path, is_dir=True)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivacyContractError(
                f"{path} is a symlink; refusing to hand sensitive pathnames to an external writer"
            ) from exc
        raise
    try:
        info = os.fstat(descriptor)
        _assert_owned_entry(info, path, is_dir=True)
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise PrivacyContractError(
                f"the database directory ({path}) is writable by other users; choose a "
                "current-user-owned directory without group or world write permission so "
                "SQLite sidecar names cannot be replaced before SQLite opens them"
            )
    finally:
        os.close(descriptor)


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


def harden_file_if_present(path: Path) -> None:
    """Set a file to `0o600` if it exists, tolerating legitimate absence.

    For transient files such as SQLite WAL sidecars that may be removed by another
    connection at any moment. Opens with `O_NOFOLLOW` and hardens through the descriptor,
    so a symlink at the path still fails closed. If the file is absent at open time,
    returns silently - a sidecar SQLite has already removed needs no hardening. This
    eliminates the TOCTOU in a separate existence check followed by a harden call.
    """
    if not _POSIX:
        _chmod_best_effort(path, FILE_MODE)
        return
    try:
        descriptor = _open_nofollow(path, is_dir=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivacyContractError(
                f"{path} is a symlink; refusing to chmod its target"
            ) from exc
        raise
    try:
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=False)
        _fchmod_owned(descriptor, FILE_MODE, path)
    finally:
        os.close(descriptor)


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
        _raise_unsafe_open(path, exc, operation="write")
        raise
    owns_descriptor = False
    try:
        # Tighten an existing file that a prior run left broad, on the descriptor we already
        # hold. O_CREAT's mode applies only to a file this call creates.
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=False)
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


# The suffix that marks a staged, not-yet-published file. Anything carrying it is garbage
# the moment its writer is gone: `publish_private_bytes` removes its own on every exit,
# and startup reconciliation sweeps any a killed process left behind.
PARTIAL_SUFFIX = ".partial"


def partial_path(final: Path) -> Path:
    """A writer-private staging name beside `final`.

    The pid alone is not private enough: FastAPI serves requests from a threadpool, so two
    concurrent writers of the same target share a pid, and one request's cleanup would
    delete the staged file out from under the other's publish. The thread id is what
    distinguishes two writers in this process, and the pid still keeps two processes (a dev
    server and a test run, say) out of each other's way.
    """
    return final.with_name(f"{final.name}.{os.getpid()}.{threading.get_ident()}{PARTIAL_SUFFIX}")


def publish_private_bytes(path: Path, data: bytes) -> None:
    """Write `data` beside `path` and atomically publish it under the final name.

    The final-file publication contract: a file that exists under its final name is whole.
    The bytes go to a writer-private staging name through the same no-follow `0o600` writer
    as any private file, and only a rename - atomic within the directory - puts the final
    name in place, so a crash at any point leaves either the old file, a clearly-marked
    `*.partial` leftover, or the complete new file. It can never leave a truncated file
    under a name readers trust on the strength of its existence.

    A symlink planted at `path` is not followed: `os.replace` swaps the directory entry
    itself, so the link is replaced rather than its target overwritten, and the staged
    write refuses a symlink at the staging name outright.
    """
    staged = partial_path(path)
    try:
        write_private_bytes(staged, data)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def publish_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` beside `path` and atomically publish it under the final name."""
    publish_private_bytes(path, text.encode(encoding))


# How much of a stream is pulled into memory at once while it is copied to disk. One
# mebibyte is large enough that the per-chunk overhead is noise against a lecture deck and
# small enough that a hostile or accidental oversized upload can never materialize more than
# this plus one chunk before the size ceiling aborts it.
STREAM_CHUNK_BYTES = 1024 * 1024


class StreamTooLargeError(Exception):
    """A streamed write crossed the byte ceiling it was given and was aborted.

    Distinct from an `OSError`: nothing failed on disk. The stream simply exceeded the
    limit the caller set, the staged bytes have been discarded, and no final file was
    published. The caller turns this into whatever over-limit answer its surface owes the
    user; `limit` is the ceiling that was crossed so the message can name it.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"stream exceeded the {limit}-byte ceiling")
        self.limit = limit


def _stream_private_bytes(staged: Path, source: object, *, max_bytes: int, chunk_size: int) -> int:
    """Copy `source` into the staged file in bounded chunks, enforcing `max_bytes`.

    `source` is any object with a blocking `read(size) -> bytes`, which is exactly the
    shape of an upload's underlying spooled file. Bytes are pulled one `chunk_size` block
    at a time and never accumulated into one object, so the peak memory a copy holds is a
    single chunk regardless of how large the stream is. The running total is checked as
    each chunk lands: the first chunk that carries the total past `max_bytes` aborts the
    copy with `StreamTooLargeError` before the remainder is read, so an oversized stream
    costs at most one chunk beyond the ceiling rather than the whole body.

    The staged file is created exclusively at `0o600` through a no-follow descriptor, the
    same private-from-the-first-byte writer every other sensitive write here uses, so a
    symlink planted at the staging name fails closed instead of being followed.

    Returns the number of bytes written, which the caller records as the stored size.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
    if not _POSIX:
        _refuse_existing_symlink(staged)
    try:
        descriptor = os.open(staged, flags, FILE_MODE)
    except OSError as exc:
        _raise_unsafe_open(staged, exc, operation="write")
        raise
    owns_descriptor = False
    total = 0
    try:
        _assert_owned_entry(os.fstat(descriptor), staged, is_dir=False)
        _fchmod_owned(descriptor, FILE_MODE, staged)
        with os.fdopen(descriptor, "wb") as handle:
            owns_descriptor = True
            while True:
                chunk = source.read(chunk_size)  # type: ignore[attr-defined]
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise StreamTooLargeError(max_bytes)
                handle.write(chunk)
    finally:
        if not owns_descriptor:
            os.close(descriptor)
    return total


def publish_private_stream(
    path: Path,
    source: object,
    *,
    max_bytes: int,
    chunk_size: int = STREAM_CHUNK_BYTES,
) -> int:
    """Stream `source` into a staged file beside `path` and publish it, enforcing `max_bytes`.

    The streaming twin of `publish_private_bytes`, and it keeps the identical publication
    contract: the bytes are copied to a writer-private `*.partial` staging name and only an
    atomic rename puts the final name in place, so a crash at any point leaves either no
    file, a clearly-marked staging leftover the startup sweep removes, or the whole file -
    never a truncated file under a name readers trust on sight. What differs is only that
    the bytes arrive from a stream rather than one `bytes` object, so an upload is never
    materialized whole in memory to be measured.

    The staging file is removed on every exit that does not publish - an over-limit abort, a
    disconnect or read error surfacing out of `source`, or a disk error mid-write - so a
    rejected or interrupted upload leaves nothing behind. The final rename happens only
    after the complete stream has been accepted under the ceiling.

    Args:
        path: Final name to publish the stored file under.
        source: An object with `read(size) -> bytes`; an upload's spooled file.
        max_bytes: The inclusive size ceiling. A stream of exactly this many bytes is
            accepted; the first byte past it aborts the copy.
        chunk_size: How many bytes to pull per read.

    Returns:
        The number of bytes published, for the caller to record as the stored size.

    Raises:
        StreamTooLargeError: the stream exceeded `max_bytes`; nothing was published.
        OSError, PrivacyContractError: the copy or publish failed; nothing was published.
    """
    staged = partial_path(path)
    try:
        total = _stream_private_bytes(staged, source, max_bytes=max_bytes, chunk_size=chunk_size)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return total


def read_owned_bytes(path: Path, *, root: Path, max_bytes: int) -> bytes:
    """Read a bounded original through the existing no-follow owned-tree boundary."""
    path = _abspath(path)
    parent_fd = None
    descriptor = None
    try:
        if _HAS_OPENAT:
            parent_fd = _open_tree_parent(path, root=root)
            if parent_fd is None:
                raise FileNotFoundError(path)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
                dir_fd=parent_fd,
            )
        else:
            if not _tree_components_real(path, root=root):
                raise FileNotFoundError(path)
            _refuse_existing_symlink(path)
            descriptor = _open_nofollow(path, is_dir=False)
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=False)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("The original exceeds the upload limit")
        return data
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def read_private_text(path: Path, *, encoding: str = "utf-8") -> str:
    """Read a private regular file without following a final-component symlink."""
    if not _POSIX:
        _refuse_existing_symlink(path)
    try:
        descriptor = _open_nofollow(path, is_dir=False)
    except OSError as exc:
        _raise_unsafe_open(path, exc, operation="read")
        raise
    owns_descriptor = False
    try:
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=False)
        _fchmod_owned(descriptor, FILE_MODE, path)
        with os.fdopen(descriptor, "r", encoding=encoding) as handle:
            owns_descriptor = True
            return handle.read()
    finally:
        if not owns_descriptor:
            os.close(descriptor)


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


# --- Destructive operations through the owned tree ------------------------------------
#
# `is_within` answers the *lexical* question - does this recorded path claim to live in
# the owned tree - but a lexical answer says nothing about symlinks in intermediate
# components: `uploads/5` can be a link to anywhere, and a plain `unlink`/`replace` on
# `uploads/5/file` would traverse it and act outside the tree while every string check
# passes. The operations below close that hole the same way `secure_mkdir` does for
# creation: descend from the owned root one component at a time with `O_NOFOLLOW`, then
# perform the final unlink/rename/stat against the directory descriptor that descent
# validated, so the entry acted on is the entry that was checked. On platforms without
# openat semantics the same best-effort per-component `lstat` policy as
# `_secure_mkdir_fallback` stands in.
#
# Shared behavior: a symlink (or non-directory) where an owned component should be raises
# `PrivacyContractError`; an absent component means nothing exists at the path, which for
# removal is the goal state and for inspection is "not present".


def _open_tree_parent(path: Path, *, root: Path) -> int | None:
    """Open `path`'s parent directory by O_NOFOLLOW descent from `root`.

    Returns an open directory descriptor the caller must close, or None only when a
    component on the way down (including the root) is provably absent (ENOENT). None is
    a statement of absence that callers settle durable work on, so no other failure may
    produce it: anything else raises, with every descriptor this call opened already
    closed. Exactly one descriptor is ever live here, and exactly one exit - the final
    `return dir_fd` - hands it to the caller. Only called on openat platforms.

    Raises:
        PrivacyContractError: a symlink or non-directory sits where an owned component
            should be.
        ValueError: `path` is not strictly inside `root`.
        OSError: a component could not be opened for any reason other than absence;
            nothing about the path's existence is implied.
    """
    path = _abspath(path)
    root = _abspath(root)
    if root not in path.parents:
        raise ValueError(f"{path} is not within the owned root {root}")
    components = [root.name, *path.parent.relative_to(root).parts]
    try:
        dir_fd = os.open(root.parent, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise
    # From here `dir_fd` is the one live descriptor this function owns. The `finally`
    # closes it on every exit - the "absent" returns included - except the single return
    # that transfers ownership to the caller.
    transferred = False
    try:
        for name in components:
            try:
                child_fd = os.open(
                    name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd
                )
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                if exc.errno == errno.ELOOP:
                    raise PrivacyContractError(
                        f"a symlink is on the path to {path}; refusing to touch state "
                        f"outside the data tree"
                    ) from exc
                if exc.errno == errno.ENOTDIR:
                    raise PrivacyContractError(
                        f"a non-directory blocks the path to {path} inside the data tree"
                    ) from exc
                raise
            # Swap before closing, so if the close itself fails the finally still closes
            # the live child rather than double-closing the descriptor that just errored.
            previous_fd, dir_fd = dir_fd, child_fd
            os.close(previous_fd)
        transferred = True
        return dir_fd
    finally:
        if not transferred:
            os.close(dir_fd)


def _tree_components_real(path: Path, *, root: Path) -> bool:
    """Best-effort stand-in for `_open_tree_parent` on platforms without openat.

    Walks the components from `root` down to `path`'s parent with `lstat`, refusing a
    symlink. Carries the same check/use race as `_secure_mkdir_fallback`, and for the
    same reason: without O_NOFOLLOW this shape is the best the platform offers, and the
    per-user data location is the real isolation there.

    Returns False only when a component is provably absent (nothing can exist at
    `path`); True when every component is a real directory. False is a statement of
    absence that callers settle durable work on, so no other failure may produce it: a
    component whose state cannot be determined raises instead, exactly as the openat
    descent does.

    Raises:
        PrivacyContractError: a component is a symlink, or a real non-directory sits
            where an owned component should be.
        ValueError: `path` is not strictly inside `root`.
        OSError: a component could not be inspected for any reason other than absence;
            nothing about the path's existence is implied.
    """
    path = _abspath(path)
    root = _abspath(root)
    if root not in path.parents:
        raise ValueError(f"{path} is not within the owned root {root}")
    current = root.parent
    for name in [root.name, *path.parent.relative_to(root).parts]:
        current = current / name
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return False
        except NotADirectoryError as exc:
            raise PrivacyContractError(
                f"a non-directory blocks the path to {path} inside the data tree"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise PrivacyContractError(
                f"a symlink is on the path to {path}; refusing to touch state outside the data tree"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise PrivacyContractError(
                f"a non-directory blocks the path to {path} inside the data tree"
            )
    return True


def stat_in_tree(path: Path, *, root: Path) -> os.stat_result | None:
    """`lstat` of the entry at `path`, reached without following any symlink.

    Returns None only when the entry - or an owned component on the way to it - is
    provably absent. The final entry itself is stat'd without following, so a symlink is
    reported as a symlink, never as its target. None is a statement of absence that
    callers settle durable work on, so an entry or component whose state cannot be
    determined raises instead of answering "absent".

    Raises:
        PrivacyContractError: a symlink or non-directory blocks an owned component.
        OSError: the entry or a component could not be inspected for any reason other
            than absence (its presence is unknown).
    """
    if not _HAS_OPENAT:
        if not _tree_components_real(path, root=root):
            return None
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None
    dir_fd = _open_tree_parent(path, root=root)
    if dir_fd is None:
        return None
    try:
        return os.stat(_abspath(path).name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        os.close(dir_fd)


def unlink_in_tree(path: Path, *, root: Path) -> None:
    """Remove the entry at `path`, reached without following any symlink; absent is fine.

    `unlink` operates on the directory entry itself, so a symlink at the final component
    is removed as a link, never followed - and the no-follow descent guarantees the same
    for every component above it.

    Raises:
        PrivacyContractError: a symlink or non-directory blocks an owned component.
        OSError: the entry exists but could not be removed, or the tree down to it
            could not be inspected (whether anything exists there is unknown).
    """
    if not _HAS_OPENAT:
        if not _tree_components_real(path, root=root):
            return
        path.unlink(missing_ok=True)
        return
    dir_fd = _open_tree_parent(path, root=root)
    if dir_fd is None:
        return
    try:
        os.unlink(_abspath(path).name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(dir_fd)


def replace_in_tree(source: Path, destination: Path, *, root: Path) -> None:
    """`os.replace` between two owned paths, reached without following any symlink.

    The rename acts on the directory entries the two no-follow descents validated, so a
    symlinked intermediate component can neither supply the file being moved nor receive
    it somewhere outside the tree.

    Raises:
        PrivacyContractError: a symlink or non-directory blocks an owned component.
        FileNotFoundError: the source (or either parent directory) does not exist.
        OSError: the rename itself failed.
    """
    if not _HAS_OPENAT:
        if not _tree_components_real(source, root=root) or not _tree_components_real(
            destination, root=root
        ):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(source))
        os.replace(source, destination)
        return
    source_fd = _open_tree_parent(source, root=root)
    if source_fd is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(source))
    try:
        destination_fd = _open_tree_parent(destination, root=root)
        if destination_fd is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(destination))
        try:
            os.replace(
                _abspath(source).name,
                _abspath(destination).name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def clear_owned_dir(directory: Path, *, root: Path, patterns: tuple[str, ...]) -> bool:
    """Empty and remove one owned directory of derived files, following no symlink ever.

    The no-recursion cleanup primitive for directories whose contents Lyra generated -
    a page/figure cache, a class's upload directory. Directly-contained entries whose
    names match `patterns` are unlinked when they are regular files or symlinks (a link
    is removed as a link, never followed). Everything else - a non-matching name, an
    unexpected subdirectory, an entry that cannot be inspected or removed - is left in
    place, deliberately visible, and never entered. The directory itself is then removed
    if it is empty.

    Returns True only when the directory is provably gone afterward: it was already
    absent (as is any path whose intermediate components are absent), its name held a
    stray non-directory entry that was removed - a symlink planted where the directory
    belongs is unlinked as a link - or every entry was removed and the final `rmdir`
    landed. Anything remaining returns False, so a caller settling durable cleanup can
    keep its record instead of declaring incomplete work done.

    Raises:
        PrivacyContractError: a symlink or non-directory blocks an owned component on
            the way to `directory`.
        ValueError: `directory` is not strictly inside `root`.
        OSError: the tree down to `directory` could not be inspected for any reason
            other than absence; whether the directory exists is unknown, and the
            caller's durable record must survive rather than settle on a guess.
    """
    directory = _abspath(directory)
    if not _HAS_OPENAT:
        return _clear_owned_dir_fallback(directory, root=root, patterns=patterns)
    parent_fd = _open_tree_parent(directory, root=root)
    if parent_fd is None:
        return True
    try:
        try:
            info = os.stat(directory.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            # A stray file or planted link wearing the directory's name: remove the
            # entry itself - for a link, the link, never its target.
            try:
                os.unlink(directory.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            return True
        try:
            dir_fd = os.open(
                directory.name,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return True
        except OSError:
            # Includes ELOOP from an entry raced into a symlink after the stat above:
            # nothing is removed, and the survivor keeps the cleanup incomplete.
            return False
        try:
            _clear_dir_entries(dir_fd, patterns)
        finally:
            os.close(dir_fd)
        try:
            os.rmdir(directory.name, dir_fd=parent_fd)
        except FileNotFoundError:
            return True
        except OSError:
            # Something survived: an entry that did not match, could not be removed, or
            # arrived while this ran. The caller decides whether that keeps a durable
            # cleanup record alive.
            return False
        return True
    finally:
        os.close(parent_fd)


def _clear_dir_entries(dir_fd: int, patterns: tuple[str, ...]) -> None:
    """Unlink the pattern-matching regular files and links directly inside `dir_fd`."""
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in patterns):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                # A subdirectory (or stranger) wearing a derived file's name is never
                # entered and never removed; it stays visible and fails the rmdir.
                continue
            try:
                os.unlink(entry.name, dir_fd=dir_fd)
            except OSError:
                continue


def _clear_owned_dir_fallback(directory: Path, *, root: Path, patterns: tuple[str, ...]) -> bool:
    """`clear_owned_dir` for platforms without openat: same shape, lstat-guarded."""
    if not _tree_components_real(directory, root=root):
        return True
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        try:
            directory.unlink(missing_ok=True)
        except OSError:
            return False
        return True
    for entry in directory.iterdir():
        if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in patterns):
            continue
        try:
            entry_info = os.lstat(entry)
        except OSError:
            continue
        if stat.S_ISREG(entry_info.st_mode) or stat.S_ISLNK(entry_info.st_mode):
            with contextlib.suppress(OSError):
                entry.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


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
    is_dir = stat.S_ISDIR(info.st_mode)
    current_mode = stat.S_IMODE(info.st_mode)
    # Directories need all owner rwx bits as well as no group/other access. A mode such as
    # 0o000 is narrower in one sense but unusable: once the sentinel is written Lyra would
    # otherwise never revisit it. Files may intentionally be owner-read-only, so their
    # existing owner bits are preserved when group/other access is already absent.
    if (is_dir and current_mode == mode) or (not is_dir and current_mode & ~mode == 0):
        return
    try:
        descriptor = _open_nofollow(path, is_dir=is_dir)
    except OSError as exc:
        # Raced into a symlink or vanished between the lstat and the open: leave it. The
        # no-follow open is what guarantees we never chmod a link's target here.
        if exc.errno in (errno.ELOOP, errno.ENOENT):
            return
        if is_dir and exc.errno in (errno.EACCES, errno.EPERM):
            _chmod_unopenable_dir_nofollow(path, mode)
            return
        raise
    try:
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=is_dir)
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
        _assert_owned_entry(os.fstat(descriptor), path, is_dir=is_dir)
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
    if not _POSIX or not hasattr(os, "fchmod"):
        _chmod_best_effort(path, mode)
        return
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


def _assert_owned_entry(info: os.stat_result, path: Path, *, is_dir: bool) -> None:
    """Require the descriptor/path entry type and, on POSIX, current-user ownership."""
    expected = stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode)
    if not expected:
        kind = "directory" if is_dir else "regular file"
        raise PrivacyContractError(
            f"{path} exists but is not a {kind}; refusing to trust it as Lyra-owned state"
        )
    if _POSIX and info.st_uid != os.geteuid():
        raise PrivacyContractError(
            f"{path} is not owned by the current user; refusing to trust or modify it as "
            "Lyra-owned state"
        )


def _raise_unsafe_open(path: Path, exc: OSError, *, operation: str) -> None:
    """Translate no-follow/special-entry open failures into the contract's error."""
    if exc.errno == errno.ELOOP:
        raise PrivacyContractError(
            f"{path} is a symlink; refusing to {operation} Lyra-owned state through it"
        ) from exc
    if exc.errno in (errno.EISDIR, errno.ENXIO, errno.ENODEV):
        raise PrivacyContractError(
            f"{path} is not a regular file; refusing to {operation} it as Lyra-owned state"
        ) from exc
    if exc.errno in (errno.EACCES, errno.EPERM):
        raise PrivacyContractError(
            f"could not {operation} {path} through a private descriptor; check that it is "
            "owned and writable by the current user"
        ) from exc


def _chmod_unopenable_dir_nofollow(path: Path, mode: int) -> None:
    """Restore owner access to a mode-000 directory without chmodding through a link.

    Some systems refuse an ordinary directory descriptor when the owner has no read or
    execute bits. `follow_symlinks=False` keeps the pathname fallback from reaching an
    outside target if the entry is raced into a symlink; the migration's next walk will
    either see the real directory at `0o700` or skip the unexpected link.
    """
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno in _MODE_UNSUPPORTED:
            logger.warning(
                "Filesystem for %s does not support POSIX modes; relying on the data "
                "directory location for privacy",
                path,
            )
            return
        raise PrivacyContractError(
            f"could not restore owner access on {path} without following symlinks"
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
