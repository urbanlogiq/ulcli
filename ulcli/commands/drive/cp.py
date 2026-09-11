# Copyright (c), CommunityLogiq Software

import argparse
import glob
import os.path
import urllib.parse
import uuid
import hashlib
import math
import time
import magic
from abc import ABC, abstractmethod
from typing import List, NamedTuple, Self, Sequence
from requests import HTTPError
from flatbuffers import util
from loguru import logger

import ulcli.argparser
from ulcli.commands.common import get_api_context
from ulcli.commands.common import uuid_from_id
from ulsdk.api.datacatalog import get_object
from ulsdk.types.id import ObjectId
from ulsdk.types.fs import ListSlot, DirectoryEntry, ListDirectory, TopLevelDirectory
from ulsdk.request_context import RequestContext
from ulsdk.api.drive import (
    get_file,
    ls,
    get_roots,
    create_entry,
    post_file,
    put_file_chunk,
    unlink,
)

from .utils import parse_timestamp_arg, timestamp_in_range, is_directory_entry_id


CHUNK_SIZE = 96 * 1024 * 1024

# A root directory has this id as its parent.
NIL_UUID = uuid.UUID(int=0)


class Entry(ABC):
    @abstractmethod
    def get(self) -> bytes:
        """Get the file content"""

    @abstractmethod
    def name(self) -> str:
        """Return the entry's name"""

    @abstractmethod
    def time(self) -> float:
        """Return the time of the entry in seconds since epoch"""

    @abstractmethod
    def isdir(self) -> bool:
        """Return whether or not an entry is a directory"""

    @abstractmethod
    def put(self, content: bytes, filename: str):
        """Append a new file to the directory"""

    @abstractmethod
    def collect(self) -> List[Self]:
        """Collect all contained entries if entry is a directory, else raises an exception"""

    @abstractmethod
    def mkdir(self, dir: str) -> Self:
        """Make a directory in the current directory"""


def put_chunk(context: RequestContext, id: uuid.UUID, chunk: bytes):
    for i in range(10):
        h = hashlib.sha256()
        h.update(chunk)
        hash = h.hexdigest()
        try:
            put_file_chunk(context, ObjectId.from_uuid(id), i, hash, chunk)
        except HTTPError as e:
            if e.response.status_code == 514:
                time.sleep(1)
                continue

            if e.response.status_code == 204:
                return

            raise e


def put_file(context: RequestContext, parent: uuid.UUID, content: bytes, filename: str):
    content_len = len(content)
    num_chunks = math.ceil(content_len / CHUNK_SIZE)
    mime = magic.from_buffer(content, mime=True)
    summary = create_entry(
        context, ObjectId.from_uuid(parent), filename, "file", mime, num_chunks
    )
    id = uuid_from_id(summary.id)
    assert id

    for i in range(num_chunks):
        chunk = content[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        put_chunk(context, id, chunk)


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

    def parent(self) -> uuid.UUID:
        return get_parent_id(self._context, self._oid)

    def get(self):
        return get_file(self._context, ObjectId.from_uuid(self._oid))

    def name(self):
        return self._slot.name

    def time(self):
        return float(self._slot.time) / 1000

    def isdir(self):
        return isinstance(self._slot.entry.value, ListDirectory) or isinstance(
            self._slot.entry.value, TopLevelDirectory
        )

    def put(self, content: bytes, filename: str):
        if self.isdir():
            put_file(self._context, self._oid, content, filename)
        else:
            parent_id = self.parent()
            put_file(self._context, parent_id, content, filename)

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

    def get(self) -> bytes:
        with open(self._path, "rb") as f:
            return f.read()

    def name(self):
        parent, name = os.path.split(self._path)
        return name

    def time(self):
        return os.path.getmtime(self._path)

    def isdir(self):
        return os.path.isdir(self._path)

    def put(self, content: bytes, filename: str):
        dest_name = os.path.join(self._path, filename) if self.isdir() else self._path
        with open(dest_name, "wb") as f:
            f.write(content)

    def collect(self) -> List["LocalEntry"]:
        return [LocalEntry(x) for x in glob.glob(os.path.join(self._path, "*"))]

    def mkdir(self, dir: str) -> "LocalEntry":
        # DriveEntry.mkdir accepts a directory that already exists, so the local
        # entry must accept one too. A recursive copy into an existing tree
        # depends on it.
        path = os.path.join(self._path, dir)
        os.makedirs(path, exist_ok=True)
        return LocalEntry(path)


def get_dir_list_slot(context: RequestContext, id: str) -> DriveEntry:
    """Resolve a bare entry id, of a file or of a directory, to its listing slot.

    The drive has no endpoint that returns the listing slot of one entry, and
    the entry object does not hold the name of the entry. The name, time and
    size are in the listing of the parent directory. So this reads the parent
    id from the entry object, lists the parent, and returns the slot that has
    the id. A root directory has `NIL_UUID` as its parent and is found in the
    list of drive roots instead.
    """
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
    """The entries that one command line path argument resolved to.

    `wildcard` records whether the argument was a wildcard pattern. A pattern
    resolves to the matched entries themselves, while a plain path resolves to
    the single entry that the path names. Recursive copies need to tell the two
    apart, because `<dir>` names the directory and `<dir>/*` names its children.
    """

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

        # Drive location may be the bare id of one entry, a file or a
        # directory, e.g. 05006c77-e69f-893e-40d1-842b64c961a5
        # The parent of the entry is read from the drive, so the caller does
        # not have to name it.
        if is_directory_entry_id(splits[0]):
            if len(splits) > 1:
                raise Exception(
                    "to reference drive roots use the syntax <guid>:/<path>"
                )

            entry = get_dir_list_slot(context, splits[0])
            resolved_args.append(ResolvedArg([entry], False))
            continue

        # Dirve location may also be a directory id followed by a relative path, e.g.:
        # 05006c77-e69f-893e-40d1-842b64c961a5:/Dataset upload folder/Transportation/Turning movement counts/*
        if splits[0].endswith(":") and len(splits[0]) == 37:
            # remote (drive) path
            root = splits[0][:-1]
            path = "/".join(splits[1:])

            # The drive treats a trailing slash the same way as a trailing `*`,
            # so both forms list the contents of the directory.
            wildcard = path.endswith("*") or path.endswith("/")

            entries = ls(context, root, path)
            slots = entries.slots
            if len(slots) == 0 and i == nfiles - 1:
                raise Exception("destination path does not exist; make it")

            if len(slots) == 0:
                logger.warning(f"Source {files[i]} matched nothing")

            resolved_args.append(
                ResolvedArg([DriveEntry(context, entry) for entry in slots], wildcard)
            )
        else:
            # local path. Unlike the drive, a local trailing slash still names
            # the directory itself, so only a `*` expands.
            wildcard = "*" in file

            globs = glob.glob(file)
            if len(globs) == 0 and i == nfiles - 1:
                os.mkdir(file)
                globs = [file]

            if len(globs) == 0:
                logger.warning(f"Source {files[i]} matched nothing")

            resolved_args.append(
                ResolvedArg([LocalEntry(path) for path in globs], wildcard)
            )

    return resolved_args


def do_cp_r(context: RequestContext, source: Entry, dest: Entry) -> bool:
    src_entries = source.collect()
    for src in src_entries:
        if src.isdir():
            dest_child = dest.mkdir(src.name())
            do_cp_r(context, src, dest_child)
        else:
            logger.info(f"Processing {src.name()}")

            content = src.get()
            dest.put(content, src.name())

    return True


def drive_cp(args: List[str]) -> bool:
    description = "Copy files to and from the drive."

    epilog = """Examples:

    ul drive cp -profile us '05006c77-e69f-893e-40d1-842b64c961a5:/Dataset upload folder/Transportation/Turning movement counts/*' ./tmp
    ul drive cp -profile us 0500addd-0f04-fe98-4ff2-bdef69cb097d ./tmp

Drive locations are specified by the prefix `<uuid>:`, where the uuid is the id
of a root directory entry, or by the bare id of one file or directory. A bare id
needs no parent directory: the command reads the parent from the drive. Use it
to download one file when you know only its id. All other paths are assumed to
be local. This bit of functionality will copy files (either one at a time or in
bulk) from the drive to a local location, from a local location to the drive,
or from one location in the drive to another.

To play nicely with shell wildcard expansion, all paths containing wildcards
need to be quoted. If a wildcard is used, the destination (last location) needs
to be a directory.

With -r, the two source forms copy different things:

    <guid>:/folder      copies the contents of `folder` into the destination
    <guid>:/folder/*    copies each entry in `folder` into the destination,
                        and each matched directory keeps its own name
"""

    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive cp",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-r", help="recursively copy directories", action="store_true")
    parser.add_argument(
        "-start",
        help="Earliest last modified date of files to move. Format: unix second or YYYY-MM-DD string (midnight local time)",
        type=str,
        default=None,
    )
    parser.add_argument(
        "-end",
        help="Latest last modified date of files to move. Format: unix second or YYYY-MM-DD string (midnight local time)",
        type=str,
        default=None,
    )
    parser.add_argument(
        "files", nargs="+", help="cp pattern. please quote all wildcards"
    )

    parsed = parser.parse_args(args)

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

    if parsed.r:
        if len(sources) == 0:
            raise Exception("Expected a source directory and a destination directory")

        if not dest.isdir():
            raise Exception("Destination must be a directory")

        for arg in source_args:
            if not arg.wildcard:
                # A plain path names one directory, and its contents merge into
                # the destination.
                for src in arg.entries:
                    if not src.isdir():
                        raise Exception("Source must be a directory")

                    do_cp_r(context, src, dest)

                continue

            # A wildcard names the matched entries, so each one lands in the
            # destination under its own name.
            for src in arg.entries:
                if src.isdir():
                    do_cp_r(context, src, dest.mkdir(src.name()))
                else:
                    logger.info(f"Processing {src.name()}")
                    dest.put(src.get(), src.name())

        return True

    if len(sources) > 1:
        if not dest.isdir():
            raise Exception(
                "If copying multiple files, the destination must be a directory"
            )

    for src in sources:
        if src.isdir():
            logger.warning(
                f"Skipping copy of source {src.name()} because it is a directory. If you intended to recursively copy directory contents, use -r."
            )
            continue
        if not timestamp_in_range(src.time(), earliest, latest):
            continue
        content = src.get()
        dest.put(content, src.name())

    return True
