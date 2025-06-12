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
from typing import List, Self
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
    summary = create_entry(context, ObjectId.from_uuid(parent), filename, "file", mime, num_chunks)
    id = uuid_from_id(summary.id)
    assert id

    for i in range(num_chunks):
        chunk = content[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        put_chunk(context, id, chunk)


def mk_dir(context: RequestContext, parent: uuid.UUID, dir: str) -> ObjectId:
    summary = create_entry(context, ObjectId.from_uuid(parent), dir, "directory", "", 0)
    return summary.id


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
        obj_res = get_object(self._context, ObjectId.from_uuid(self._oid))
        obj_bytes = bytes(obj_res.obj)
        entry = DirectoryEntry.from_bytes(obj_bytes)
        id = uuid_from_id(entry.parent)
        assert id is not None
        return id

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
        path = os.path.join(self._path, dir)
        os.mkdir(path)
        return LocalEntry(path)


def is_directory_entry_id(id: str) -> bool:
    try:
        assert uuid.UUID(id)
    except Exception:
        return False
    return id.startswith("0500")


def get_dir_list_slot(context: RequestContext, id: str) -> DriveEntry:
    obj_res = get_object(context, ObjectId.from_uuid(id))
    obj_bytes = bytes(obj_res.obj)
    entry = DirectoryEntry.from_bytes(obj_bytes)
    parent = uuid_from_id(entry.parent)

    if parent == "00000000-0000-0000-0000-000000000000":
        children = get_roots(context)
    else:
        children = ls(context, str(parent), "*")

    for child in children.slots:
        if uuid_from_id(child.id) == id:
            return DriveEntry(context, child)
    raise ValueError(f"Could not find directory with id {id} in parent {parent}")


def parse_files(context: RequestContext, files: List[str]) -> List[Entry]:
    resolved_files = []
    nfiles = len(files)
    for i in range(nfiles):
        file = files[i]
        if "%" in file:
            file = urllib.parse.unquote(file)

        if "*" in file and not file.endswith("*"):
            raise ValueError("Currently only trailing wildcards are supported")

        splits = file.split("/")

        # Drive location may be a directory id, e.g.
        # 05006c77-e69f-893e-40d1-842b64c961a5
        if is_directory_entry_id(splits[0]):
            if len(splits) > 1:
                raise Exception(
                    "to reference drive roots use the syntax <guid>:/<path>"
                )

            resolved_files.append(get_dir_list_slot(context, splits[0]))
            continue

        # Dirve location may also be a directory id followed by a relative path, e.g.:
        # 05006c77-e69f-893e-40d1-842b64c961a5:/Dataset upload folder/Transportation/Turning movement counts/*
        if splits[0].endswith(":") and len(splits[0]) == 37:
            # remote (drive) path
            root = splits[0][:-1]
            path = "/".join(splits[1:])

            entries = ls(context, root, path)
            slots = entries.slots
            if len(slots) == 0 and i == nfiles - 1:
                raise Exception("destination path does not exist; make it")

            resolved_files += [DriveEntry(context, entry) for entry in slots]
        else:
            # local path
            globs = glob.glob(file)
            if len(globs) == 0 and i == nfiles - 1:
                os.mkdir(file)
                globs = [file]

            resolved_files += [LocalEntry(file) for file in globs]

    return resolved_files


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

    epilog = """Example:

    ul drive cp -profile us '05006c77-e69f-893e-40d1-842b64c961a5:/Dataset upload folder/Transportation/Turning movement counts/*' ./tmp

Drive locations are specified by the prefix `<uuid>:`, where the uuid is the id
of a root directory entry. All other paths are assumed to be local. This bit of
functionality will copy files (either one at a time or in bulk) from the drive
to a local location, from a local location to the drive, or from one location in
the drive to another.

To play nicely with shell wildcard expansion, all paths containing wildcards
need to be quoted. If a wildcard is used, the destination (last location) needs
to be a directory.
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

    parsed_files = parse_files(context, files)
    sources = parsed_files[:-1]
    dest = parsed_files[-1]

    earliest = parse_timestamp_arg(parsed.start)
    latest = parse_timestamp_arg(parsed.end)

    if earliest is not None and latest is not None and earliest > latest:
        raise Exception("Earliest timestamp must be less than latest timestamp")

    if parsed.r:
        if len(sources) != 1:
            raise Exception("Expected a source directory and a destination directory")

        source = sources[0]

        if not sources[0].isdir():
            raise Exception("Source must be a directory")

        if not dest.isdir():
            raise Exception("Destination must be a directory")

        return do_cp_r(context, source, dest)

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
