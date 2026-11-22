# Copyright (c), CommunityLogiq Software

import argparse
import contextlib
import errno
import glob
import os
import os.path
import signal
import stat
import sys
import threading
import tempfile
import unicodedata
import urllib.parse
import uuid
import hashlib
import time
import magic
from abc import ABC, abstractmethod
from collections import deque
from types import FrameType
from typing import (
    BinaryIO,
    Dict,
    Generator,
    Hashable,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Self,
    Sequence,
    Set,
)
import requests
from requests import HTTPError
from loguru import logger

import ulcli.argparser
from ulcli.commands.common import get_api_context
from ulcli.commands.common import uuid_from_id
from ulsdk.api.datacatalog import get_object
from ulsdk.types.id import ObjectId
from ulsdk.types.fs import (
    DirectoryEntry,
    File,
    ListDirectory,
    ListSlot,
    TopLevelDirectory,
)
from ulsdk.request_context import RequestContext, ResponseStream
from ulsdk.api.drive import (
    ls,
    get_roots,
    create_entry,
    get_file_stream,
    put_file_chunk,
    unlink,
)

from .progress import Progress, printable
from .utils import parse_timestamp_arg, timestamp_in_range, is_directory_entry_id

CHUNK_SIZE = 96 * 1024 * 1024
CHUNK_UPLOAD_ATTEMPTS = 10

BLOCK_SIZE = 1024 * 1024
PARTIAL_SUFFIX = ".ulpart"
# Limit the temporary name to fewer than 255 bytes.
PARTIAL_NAME_LENGTH = 48
INTERRUPT_GRACE_SECONDS = 2.0
DOWNLOAD_TIMEOUT = (10, 60)
# Stop a download after this many failures in a row with no content between them.
DOWNLOAD_ATTEMPTS = 6
# Wait this long after a failure. Each wait with no content between them is two
# times longer.
DOWNLOAD_RETRY_SECONDS = 1.0
# The connection failed or the response ended early.
DOWNLOAD_RETRY_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
# The gateway gives these statuses while the drive service is not available.
DOWNLOAD_RETRY_STATUSES = (502, 503, 504)

# A root directory has this id as its parent.
NIL_UUID = uuid.UUID(int=0)


class Entry(ABC):
    @abstractmethod
    def size(self) -> int:
        """Return the file size in bytes."""

    @abstractmethod
    def read(self, block_size: int) -> Generator[bytes, None, None]:
        """Read the file in blocks of at most `block_size` bytes."""

    @abstractmethod
    def name(self) -> str:
        """Return the entry name."""

    @abstractmethod
    def time(self) -> float:
        """Return the modification time in seconds since 1970-01-01 UTC."""

    @abstractmethod
    def isdir(self) -> bool:
        """Return True if the entry is a directory."""

    @abstractmethod
    def block_size(self) -> int:
        """Return the preferred block size for this destination."""

    @abstractmethod
    def target(self, filename: str) -> Hashable:
        """Return a key for the destination file. Copies with the same key must
        run in order."""

    @abstractmethod
    def put(self, blocks: Iterable[bytes], size: int, filename: str):
        """Write `blocks` to the destination, with `size` bytes in total."""

    @abstractmethod
    def collect(self) -> Sequence["Entry"]:
        """Return the entries in this directory. Raise an error for a file."""

    @abstractmethod
    def mkdir(self, dir: str) -> Self:
        """Make a directory in this directory."""


def comparison_name(name: str) -> str:
    """Compare names without case differences or equivalent Unicode spellings."""
    return unicodedata.normalize("NFC", name).casefold()


def file_blocks(pieces: Iterable[bytes], size: int) -> Iterator[bytes]:
    """Return blocks of `size` bytes. The last block can be smaller."""
    if size < 1:
        raise ValueError("Block size must be at least 1")
    buffer = bytearray()
    for piece in pieces:
        if not buffer and len(piece) == size:
            yield piece
        else:
            with memoryview(piece) as view:
                offset = 0
                while offset < len(view):
                    count = min(size - len(buffer), len(view) - offset)
                    buffer.extend(view[offset : offset + count])
                    offset += count
                    if len(buffer) == size:
                        block = bytes(buffer)
                        buffer.clear()
                        yield block
                        del block
        # Release the previous block before the next read.
        del piece
    if buffer:
        yield bytes(buffer)


def check_size(blocks: Iterable[bytes], size: int) -> Iterator[bytes]:
    """Stop the copy if the content does not match the file size."""
    if size < 0:
        raise ValueError("File size must not be negative")
    received = 0
    for block in blocks:
        received += len(block)
        if received > size:
            raise OSError(f"File size changed during the copy. Expected {size} bytes.")
        yield block
        del block
    if received != size:
        raise OSError(f"File size changed during the copy. Expected {size} bytes.")


def put_chunk(context: RequestContext, id: uuid.UUID, index: int, chunk: bytes):
    """Upload one block at `index`. Retry only when the drive reports a checksum
    error (514)."""
    hash = hashlib.sha256(chunk).hexdigest()
    for attempt in range(CHUNK_UPLOAD_ATTEMPTS):
        try:
            put_file_chunk(context, ObjectId.from_uuid(id), index, hash, chunk)
            return
        except HTTPError as e:
            last_attempt = attempt == CHUNK_UPLOAD_ATTEMPTS - 1
            if e.response is None or e.response.status_code != 514 or last_attempt:
                raise
            time.sleep(1)


def put_file(
    context: RequestContext,
    parent: uuid.UUID,
    blocks: Iterable[bytes],
    size: int,
    filename: str,
):
    """Upload the file in blocks, with `size` bytes in total."""
    num_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = file_blocks(check_size(blocks, size), CHUNK_SIZE)
    chunk = next(chunks, b"")
    mime = magic.from_buffer(chunk, mime=True)
    summary = create_entry(
        context, ObjectId.from_uuid(parent), filename, "file", mime, num_chunks
    )
    id = uuid_from_id(summary.id)
    assert id

    index = 0
    while chunk:
        put_chunk(context, id, index, chunk)
        index += 1
        # Release this block before the next read.
        del chunk
        chunk = next(chunks, b"")


def incomplete_upload(context: RequestContext, id: uuid.UUID) -> Optional[str]:
    """Describe a file whose upload did not complete. Return None for a
    complete file, or if the drive does not give the entry."""
    try:
        obj = get_object(context, ObjectId.from_uuid(id))
        file = DirectoryEntry.from_bytes(bytes(obj.obj)).entry.value
        chunks = (file.chunks or []) if isinstance(file, File) else []
        stored = sum(1 for chunk in chunks if chunk.size > 0)
    except Exception:
        return None
    if stored == len(chunks):
        return None
    return (
        "The upload of this file did not complete."
        f" The drive has {stored} of {len(chunks)} blocks."
    )


def entity_tag(file: BinaryIO) -> Optional[str]:
    """Return the tag that identifies this version of the file. A later
    request can use only a strong tag."""
    if not isinstance(file, ResponseStream):
        return None
    tag = file.headers.get("ETag")
    return None if tag is None or tag.startswith("W/") else tag


def open_drive_file(
    context: RequestContext, id: uuid.UUID, start: int, tag: Optional[str]
) -> BinaryIO:
    """Request a drive file from byte `start`. A start after byte 0 needs
    the `tag` of the first response. Fail if the file changed after it."""
    file = ObjectId.from_uuid(id)
    if start == 0:
        return get_file_stream(
            context, file, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False
        )

    stream = get_file_stream(
        context,
        file,
        f"bytes={start}-",
        tag,
        timeout=DOWNLOAD_TIMEOUT,
        allow_redirects=False,
    )
    # The drive sends the complete file if the file changed.
    if (
        not isinstance(stream, ResponseStream)
        or stream.status_code != 206
        or not stream.headers.get("Content-Range", "").startswith(f"bytes {start}-")
    ):
        stream.close()
        raise OSError("File changed during the copy.")
    return stream


def read_drive_file(
    context: RequestContext, id: uuid.UUID, block_size: int
) -> Generator[bytes, None, None]:
    """Read a drive file in blocks of at most `block_size` bytes.

    If a request fails or its response ends early, request the remaining
    bytes. Stop after `DOWNLOAD_ATTEMPTS` failures in a row with no content
    between them.
    """
    if block_size < 1:
        raise ValueError("Block size must be at least 1")
    size = min(block_size, BLOCK_SIZE)
    received = 0
    tag: Optional[str] = None
    failures = 0
    while True:
        try:
            with open_drive_file(context, id, received, tag) as file:
                if received == 0:
                    tag = entity_tag(file)
                for block in iter(lambda: file.read(size), b""):
                    received += len(block)
                    failures = 0
                    yield block
                    # Release this block before the next read.
                    del block
            return
        except DOWNLOAD_RETRY_ERRORS as error:
            failure: Exception = error
        except HTTPError as error:
            status = None if error.response is None else error.response.status_code
            if status == 404:
                # The drive gives this status for a file without all its blocks.
                reason = incomplete_upload(context, id)
                if reason is not None:
                    raise OSError(reason) from error
            if status not in DOWNLOAD_RETRY_STATUSES:
                raise
            failure = error

        failures += 1
        # Without a tag, a new request cannot detect a file that changed.
        if failures == DOWNLOAD_ATTEMPTS or (received > 0 and tag is None):
            raise failure
        time.sleep(DOWNLOAD_RETRY_SECONDS * 2 ** (failures - 1))


# Track temporary files for removal after cancellation.
_partial_files: Set[str] = set()
_partial_files_lock = threading.Lock()


def remove_partial_files() -> None:
    """Remove temporary files after cancellation, including those from copies
    that cannot stop."""
    with _partial_files_lock:
        leftovers = list(_partial_files)
        _partial_files.clear()

    for path in leftovers:
        with contextlib.suppress(OSError):
            os.unlink(path)


def sync_directory(directory: str) -> None:
    """Write directory changes to disk if the system supports this operation."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_complete_file(path: str, blocks: Iterable[bytes]) -> None:
    """Replace a file only after all content reaches disk.

    Remove the temporary file if the copy fails before replacement.
    Write directly to devices and pipes.
    """
    # Follow links in the path that the user supplied.
    path = os.path.realpath(path)

    try:
        existing = os.stat(path)
    except FileNotFoundError:
        existing = None

    # Check the destination before the first read.
    if existing is not None and stat.S_ISDIR(existing.st_mode):
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), path)
    if existing is not None and not os.access(path, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)

    if existing is not None and not stat.S_ISREG(existing.st_mode):
        # Write directly to a device or pipe.
        with open(path, "wb") as f:
            for block in blocks:
                f.write(block)
        return

    directory, name = os.path.split(path)
    fd, partial = tempfile.mkstemp(
        dir=directory, prefix=f".{name[:PARTIAL_NAME_LENGTH]}.", suffix=PARTIAL_SUFFIX
    )
    try:
        with os.fdopen(fd, "wb") as f:
            with _partial_files_lock:
                _partial_files.add(partial)
            for block in blocks:
                f.write(block)

            f.flush()
            if existing is not None:
                # Keep access permissions, but remove special execution permissions.
                os.chmod(partial, stat.S_IMODE(existing.st_mode) & 0o777)
            # Write the content to disk before the file replaces the destination.
            os.fsync(f.fileno())

        os.replace(partial, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(partial)
        with _partial_files_lock:
            _partial_files.discard(partial)

    sync_directory(directory)


def mk_dir(context: RequestContext, parent: uuid.UUID, dir: str) -> ObjectId:
    summary = create_entry(context, ObjectId.from_uuid(parent), dir, "directory", "", 0)
    return summary.id


def get_parent_id(context: RequestContext, id: uuid.UUID) -> uuid.UUID:
    """Return the id of the directory that holds the entry `id`.

    The parent of a root directory is `NIL_UUID`.
    """
    obj_res = get_object(context, ObjectId.from_uuid(id))
    entry = DirectoryEntry.from_bytes(bytes(obj_res.obj))
    parent = uuid_from_id(entry.parent)
    assert parent is not None
    return parent


class Removable(ABC):
    @abstractmethod
    def rm(self):
        """Remove the entry from the drive"""


class DriveEntry(Entry, Removable):
    _oid: uuid.UUID

    def __init__(self, context: RequestContext, slot: ListSlot):
        self._context = context
        self._slot = slot
        oid = uuid_from_id(slot.id)
        assert oid is not None
        self._oid = oid
        self._directory: Optional[uuid.UUID] = None

    def parent(self) -> uuid.UUID:
        return get_parent_id(self._context, self._oid)

    def directory(self) -> uuid.UUID:
        """Return the destination directory ID. Save the result to avoid repeated
        requests."""
        if self._directory is None:
            self._directory = self._oid if self.isdir() else self.parent()
        return self._directory

    def size(self):
        return self._slot.size

    def read(self, block_size: int) -> Generator[bytes, None, None]:
        return read_drive_file(self._context, self._oid, block_size)

    def name(self):
        return self._slot.name

    def time(self):
        return float(self._slot.time) / 1000

    def isdir(self):
        return isinstance(self._slot.entry.value, ListDirectory) or isinstance(
            self._slot.entry.value, TopLevelDirectory
        )

    def block_size(self):
        # Match the upload block size to avoid extra copies in memory.
        return CHUNK_SIZE

    def target(self, filename: str) -> Hashable:
        return (self.directory(), comparison_name(filename))

    def put(self, blocks: Iterable[bytes], size: int, filename: str):
        put_file(self._context, self.directory(), blocks, size, filename)

    def collect(self) -> List["DriveEntry"]:
        assert self.isdir()
        res = ls(self._context, str(self._oid), "*")
        entries = res.slots
        return [DriveEntry(self._context, entry) for entry in entries]

    def mkdir(self, dir: str) -> "DriveEntry":
        assert self.isdir()

        try:
            mk_dir(self._context, self._oid, dir)
        except ValueError as e:
            if "already exists" in e.args[0]:
                pass
            else:
                raise e

        entries = self.collect()
        for entry in entries:
            if entry.name() == dir:
                return entry

        raise Exception("Created directory was not in directory listing")

    def rm(self):
        unlink(self._context, ObjectId.from_uuid(self._oid))


class LocalEntry(Entry):
    def __init__(self, path: str):
        self._path = os.path.realpath(path)

    def size(self):
        return os.path.getsize(self._path)

    def read(self, block_size: int) -> Generator[bytes, None, None]:
        with open(self._path, "rb") as f:
            # Do not retain the previous block during the next read.
            yield from iter(lambda: f.read(block_size), b"")

    def name(self):
        parent, name = os.path.split(self._path)
        return name

    def time(self):
        return os.path.getmtime(self._path)

    def isdir(self):
        return os.path.isdir(self._path)

    def isatty(self) -> bool:
        """Return True if the entry is a terminal."""
        try:
            # Open only a device. A pipe can end when a writer closes it.
            if not stat.S_ISCHR(os.stat(self._path).st_mode):
                return False
            # Do not wait for the device or make it the process terminal.
            flags = (
                os.O_WRONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
            )
            fd = os.open(self._path, flags)
        except OSError:
            return False
        try:
            return os.isatty(fd)
        finally:
            os.close(fd)

    def block_size(self):
        return BLOCK_SIZE

    def child_path(self, name: str) -> str:
        """Keep names from the drive inside this directory."""
        if name in ("", ".", "..") or any(char in name for char in "/\\:\0"):
            raise ValueError(f"Invalid file name: {name!r}")
        path = os.path.join(self._path, name)
        if os.path.commonpath([self._path, os.path.realpath(path)]) != self._path:
            raise ValueError(f"File name points outside the destination: {name!r}")
        return path

    def path_of(self, filename: str) -> str:
        """Return the destination path for `filename`."""
        return self.child_path(filename) if self.isdir() else self._path

    def target(self, filename: str) -> Hashable:
        return comparison_name(os.path.realpath(self.path_of(filename)))

    def put(self, blocks: Iterable[bytes], size: int, filename: str):
        write_complete_file(self.path_of(filename), check_size(blocks, size))

    def collect(self) -> List["LocalEntry"]:
        return [LocalEntry(x) for x in glob.glob(os.path.join(self._path, "*"))]

    def mkdir(self, dir: str) -> "LocalEntry":
        # Accept existing directories, as DriveEntry.mkdir does.
        path = self.child_path(dir)
        os.makedirs(path, exist_ok=True)
        return LocalEntry(path)


def get_dir_list_slot(context: RequestContext, id: str) -> DriveEntry:
    """Find an entry by its ID in the parent directory. Use the drive roots if
    the entry has no parent."""
    entry_id = uuid.UUID(id)
    parent = get_parent_id(context, entry_id)

    if parent == NIL_UUID:
        children = get_roots(context)
    else:
        children = ls(context, str(parent), "*")

    for child in children.slots:
        if uuid_from_id(child.id) == entry_id:
            return DriveEntry(context, child)
    raise ValueError(
        f"Could not find entry {id} in the listing of its parent directory {parent}"
    )


class ResolvedArg(NamedTuple):
    """The entries for one path argument. A wildcard selects each entry by name."""

    entries: Sequence[Entry]
    wildcard: bool


def parse_files(context: RequestContext, files: List[str]) -> List[ResolvedArg]:
    resolved_args = []
    nfiles = len(files)
    for i in range(nfiles):
        file = files[i]
        if "%" in file:
            file = urllib.parse.unquote(file)

        if "*" in file and not file.endswith("*"):
            raise ValueError("Currently only trailing wildcards are supported")

        splits = file.split("/")

        # An entry ID needs no parent directory in the argument.
        if is_directory_entry_id(splits[0]):
            if len(splits) > 1:
                raise Exception(
                    "to reference drive roots use the syntax <guid>:/<path>"
                )

            entry = get_dir_list_slot(context, splits[0])
            resolved_args.append(ResolvedArg([entry], False))
            continue

        # A drive path starts with a directory ID and a colon.
        if splits[0].endswith(":") and len(splits[0]) == 37:
            root = splits[0][:-1]
            path = "/".join(splits[1:])

            # A final slash or wildcard lists the drive directory contents.
            wildcard = path.endswith("*") or path.endswith("/")

            entries = ls(context, root, path)
            slots = entries.slots
            if len(slots) == 0 and i == nfiles - 1:
                raise Exception("destination path does not exist; make it")

            if len(slots) == 0:
                logger.warning(f"Source {printable(files[i])} matched nothing")

            resolved_args.append(
                ResolvedArg([DriveEntry(context, entry) for entry in slots], wildcard)
            )
        else:
            # A local path needs a wildcard to select the directory contents.
            wildcard = "*" in file

            globs = glob.glob(file)
            if len(globs) == 0 and i == nfiles - 1:
                os.mkdir(file)
                globs = [file]

            if len(globs) == 0:
                logger.warning(f"Source {printable(files[i])} matched nothing")

            resolved_args.append(
                ResolvedArg([LocalEntry(path) for path in globs], wildcard)
            )

    return resolved_args


class Transfer(NamedTuple):
    """A source file and its destination."""

    source: Entry
    dest: Entry


class Cancelled(Exception):
    """The copy stopped after cancellation."""


def is_download(transfer: Transfer) -> bool:
    """Return True if the copy reads a file from the drive."""
    return isinstance(transfer.source, DriveEntry)


def copy_file(transfer: Transfer, cancel: threading.Event, progress: Progress) -> None:
    """Copy one file. Stop on cancellation before or after each read.

    Count the bytes of a download in `progress`."""
    source, dest = transfer
    with progress.hidden():
        logger.info(f"Copying {printable(source.name())}")

    def check_cancelled() -> None:
        if cancel.is_set():
            raise Cancelled()

    def copy_blocks() -> Iterator[bytes]:
        check_cancelled()
        for block in blocks:
            check_cancelled()
            yield block
            del block
            check_cancelled()
        check_cancelled()

    blocks = source.read(dest.block_size())
    if is_download(transfer):
        blocks = progress.track(source.name(), source.size(), blocks)
    # Close the source after success, failure, or cancellation.
    with contextlib.closing(blocks):
        dest.put(copy_blocks(), source.size(), source.name())
    if is_download(transfer):
        progress.complete()


def plan_directory_copy(source: Entry, dest: Entry, transfers: List[Transfer]) -> None:
    """List the files to copy and create their destination directories."""
    for src in source.collect():
        if src.isdir():
            plan_directory_copy(src, dest.mkdir(src.name()), transfers)
        else:
            transfers.append(Transfer(src, dest))


def group_by_target(transfers: Sequence[Transfer]) -> List[List[Transfer]]:
    """Group copies by destination. Keep copies to the same file in their
    original order."""
    groups: Dict[Hashable, List[Transfer]] = dict()
    for transfer in transfers:
        target = transfer.dest.target(transfer.source.name())
        groups.setdefault(target, []).append(transfer)
    return list(groups.values())


def default_jobs() -> int:
    """Return one copy per processor, or one if the processor count is unknown."""
    return os.cpu_count() or 1


def join_all(threads: Sequence[threading.Thread], timeout: Optional[float]) -> None:
    """Wait at most `timeout` seconds for all threads. Short waits let Ctrl-C
    stop the command."""
    deadline = None if timeout is None else time.monotonic() + timeout
    for thread in threads:
        while thread.is_alive():
            if deadline is not None and time.monotonic() >= deadline:
                return
            thread.join(0.1)


def run_transfers(transfers: Sequence[Transfer], jobs: int, progress: Progress) -> None:
    """Copy at most `jobs` files at a time.

    Stop new copies after a failure. Wait for active copies, then report the
    failure."""
    groups = deque(group_by_target(transfers))
    num_workers = min(jobs, len(groups))

    if num_workers <= 1:
        # Use the calling thread so that Ctrl-C stops a single copy directly.
        never = threading.Event()
        for transfer in transfers:
            copy_file(transfer, never, progress)
        return

    cancel = threading.Event()
    lock = threading.Lock()
    failures: List[BaseException] = []
    copied = 0

    def work() -> None:
        nonlocal copied
        while True:
            with lock:
                if not groups:
                    return
                group = groups.popleft()

            for transfer in group:
                # Do not start another copy after a failure or cancellation.
                if failures or cancel.is_set():
                    return
                try:
                    copy_file(transfer, cancel, progress)
                except Cancelled:
                    return
                except BaseException as error:
                    # Report failures that would otherwise end only this thread.
                    with lock:
                        failures.append(error)
                    with progress.hidden():
                        logger.error(
                            printable(
                                f"Failed to copy {transfer.source.name()}: {error}"
                            )
                        )
                    return
                with lock:
                    copied += 1

    # Let the command exit if a network request cannot stop.
    threads = [threading.Thread(target=work, daemon=True) for _ in range(num_workers)]
    try:
        for thread in threads:
            thread.start()
        join_all(threads, None)
    except BaseException:
        # Stop active copies, then remove any remaining temporary files.
        cancel.set()
        try:
            join_all(threads, INTERRUPT_GRACE_SECONDS)
        finally:
            remove_partial_files()
        raise

    if failures:
        with progress.hidden():
            logger.error(
                f"Copied {copied} of {len(transfers)} files and stopped after a failure"
            )
        raise failures[0]


@contextlib.contextmanager
def exit_on_termination() -> Iterator[None]:
    """Remove temporary files when SIGTERM or SIGHUP stops the command. Keep
    existing signal handlers."""

    def on_signal(signum: int, frame: Optional[FrameType]) -> None:
        raise SystemExit(128 + signum)

    replaced = dict()
    # Only the main thread may set a signal handler.
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None and signal.getsignal(signum) == signal.SIG_DFL:
                replaced[signum] = signal.signal(signum, on_signal)

    try:
        yield
    finally:
        for signum, handler in replaced.items():
            signal.signal(signum, handler)


def drive_cp(args: List[str]) -> bool:
    description = "Copy files to and from the drive."

    epilog = """Examples:

    ul drive cp -profile us '05006c77-e69f-893e-40d1-842b64c961a5:/Dataset upload folder/Transportation/Turning movement counts/*' ./tmp
    ul drive cp -profile us 0500addd-0f04-fe98-4ff2-bdef69cb097d ./tmp

Use `<id>:/path` for a drive path, where `<id>` is the root directory ID.
Use a file or directory ID alone if you do not know its parent path.
Other paths refer to local files. The command can copy files between the drive
and your computer, or between two drive locations.

Put paths with `*` in quotes. The `*` selects all entries in a directory.
For these paths, the destination must be a directory.

With -r, these source paths select different contents:

    <id>:/folder      Copy the contents of `folder` into the destination.
    <id>:/folder/*    Copy each entry in `folder` with its name intact.

The command reads each file in blocks to limit memory use. Use -j to set the
number of files to copy at the same time. Use -j 1 to copy files one at a time.
Copies to the same destination file run in the given order.

The terminal shows progress for each download and for all downloads together.
The command shows no progress for uploads or for copies to a terminal.

The command writes each local file to a temporary file before it replaces the
destination. A failed copy leaves the previous file intact. Devices and pipes
receive content directly. Only the owner can read or write a new local file.
Existing files keep their access permissions. Copies remove special execution
permissions. Names from the drive cannot point outside the destination directory.

If a download stops before the end of the file, the command requests the
remaining content.

A forced exit can leave a hidden file named `.<name>.<random>.ulpart`.
You can delete these incomplete files after the command stops.

"""

    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive cp",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-r", help="Copy directories and their contents", action="store_true"
    )
    jobs = default_jobs()
    parser.add_argument(
        "-j",
        help=f"Number of files to copy at the same time. Default: one per processor ({jobs} on this system)",
        type=int,
        default=jobs,
        metavar="JOBS",
    )
    parser.add_argument(
        "-start",
        help="Copy files changed on or after this date. Use seconds since 1970-01-01 UTC or YYYY-MM-DD (local midnight).",
        type=str,
        default=None,
    )
    parser.add_argument(
        "-end",
        help="Copy files changed on or before this date. Use seconds since 1970-01-01 UTC or YYYY-MM-DD (local midnight).",
        type=str,
        default=None,
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Source paths followed by the destination. Put paths with * in quotes.",
    )

    parsed = parser.parse_args(args)
    if parsed.j < 1:
        raise Exception("-j must be at least 1")

    context = get_api_context(parsed)
    files = parsed.files
    if len(files) < 2:
        raise Exception("Need at least two files (source and destination)")

    parsed_args = parse_files(context, files)
    source_args = parsed_args[:-1]
    dest_arg = parsed_args[-1]

    if len(dest_arg.entries) != 1:
        raise Exception(
            f"The destination must name a single file or directory, but it matched {len(dest_arg.entries)} entries"
        )

    dest = dest_arg.entries[0]
    sources = [entry for arg in source_args for entry in arg.entries]

    earliest = parse_timestamp_arg(parsed.start)
    latest = parse_timestamp_arg(parsed.end)

    if earliest is not None and latest is not None and earliest > latest:
        raise Exception("Earliest timestamp must be less than latest timestamp")

    # Create directories before files can copy at the same time.
    transfers: List[Transfer] = []

    if parsed.r:
        if len(sources) == 0:
            raise Exception("Expected a source directory and a destination directory")

        if not dest.isdir():
            raise Exception("Destination must be a directory")

        for arg in source_args:
            if not arg.wildcard:
                # Copy the contents of the named directory into the destination.
                for src in arg.entries:
                    if not src.isdir():
                        raise Exception("Source must be a directory")

                    plan_directory_copy(src, dest, transfers)

                continue

            # Keep each name that the wildcard selects.
            for src in arg.entries:
                if src.isdir():
                    plan_directory_copy(src, dest.mkdir(src.name()), transfers)
                else:
                    transfers.append(Transfer(src, dest))
    else:
        if len(sources) > 1:
            if not dest.isdir():
                raise Exception(
                    "If copying multiple files, the destination must be a directory"
                )

        for src in sources:
            if src.isdir():
                logger.warning(
                    f"Skipped directory {printable(src.name())}. Use -r to copy its contents."
                )
                continue
            if not timestamp_in_range(src.time(), earliest, latest):
                continue
            transfers.append(Transfer(src, dest))

    sizes = [transfer.source.size() for transfer in transfers if is_download(transfer)]
    if sizes and isinstance(dest, LocalEntry) and dest.isatty():
        # The copy writes the files to the terminal. Do not mix progress with them.
        sizes = []
    progress = Progress(sys.stderr, len(sizes), sum(sizes))
    with exit_on_termination(), progress:
        run_transfers(transfers, parsed.j, progress)

    return True
