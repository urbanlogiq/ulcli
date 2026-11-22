# Copyright (c), CommunityLogiq Software

"""Test drive copies with local files, prepared data, and a local test server."""

import _thread
import base64
import gzip
import hashlib
import http.server
import io
import itertools
import os
import re
import signal
import stat
import sys
import threading
import time
import tracemalloc
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Callable,
    Dict,
    Generator,
    Hashable,
    Iterable,
    Iterator,
    List,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    cast,
)

import pytest
import requests
from loguru import logger
from requests import HTTPError, Response

from ulcli.commands.common import uuid_from_id
from ulcli.commands.drive import cp, progress
from ulcli.commands.drive.cp import ResolvedArg, Transfer
from ulcli.commands.drive.progress import Progress
from ulsdk.api_key_context import ApiKeyContext
from ulsdk.keys import Environment, Key, Region
from ulsdk.request_context import RequestContext
from ulsdk.types.crypto import Digest
from ulsdk.types.fs import (
    Chunk,
    DirectoryEntry,
    DirectoryList,
    Entry,
    File,
    ListEntry,
    ListFile,
    ListSlot,
    TopLevelDirectory,
)
from ulsdk.types.id import GenericId, ObjectId
from ulsdk.types.object import DataCatalogObject


class FakeEntry(cp.Entry):
    """A file or directory stored in memory."""

    def __init__(self, name: str, isdir: bool, content: bytes = b"", time: float = 0.0):
        self._name = name
        self._isdir = isdir
        self._content = content
        self._time = time
        self.children: Dict[str, "FakeEntry"] = {}

    def add(self, child: "FakeEntry") -> "FakeEntry":
        self.children[child.name()] = child
        return child

    def content(self) -> bytes:
        return self._content

    def size(self) -> int:
        return len(self._content)

    def read(self, block_size: int) -> Generator[bytes, None, None]:
        for start in range(0, len(self._content), block_size):
            yield self._content[start : start + block_size]

    def name(self) -> str:
        return self._name

    def time(self) -> float:
        return self._time

    def isdir(self) -> bool:
        return self._isdir

    def block_size(self) -> int:
        return 4

    def target(self, filename: str) -> Hashable:
        return (id(self), filename)

    def put(self, blocks: Iterable[bytes], size: int, filename: str):
        assert self._isdir
        content = b"".join(blocks)
        assert len(content) == size
        self.add(FakeEntry(filename, False, content))

    def collect(self) -> List["FakeEntry"]:
        assert self._isdir
        return list(self.children.values())

    def mkdir(self, dir: str) -> "FakeEntry":
        existing = self.children.get(dir)
        if existing is not None:
            assert existing.isdir()
            return existing
        return self.add(FakeEntry(dir, True))


def flatten(entry: FakeEntry, prefix: str = "") -> Dict[str, bytes]:
    """Return each file path and its content below `entry`."""

    files = {}
    for child in entry.collect():
        path = f"{prefix}{child.name()}"
        if child.isdir():
            files.update(flatten(child, f"{path}/"))
        else:
            files[path] = child.content()
    return files


def source_tree() -> FakeEntry:
    """A directory with one file and two child directories."""

    root = FakeEntry("folder", True)
    root.add(FakeEntry("top.csv", False, b"top"))
    fire = root.add(FakeEntry("Fire incidents", True))
    fire.add(FakeEntry("2026.csv", False, b"fire"))
    geo = root.add(FakeEntry("Geospatial assets", True))
    nested = geo.add(FakeEntry("nested", True))
    nested.add(FakeEntry("deep.geojson", False, b"deep"))
    return root


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch):
    """Do not wait between the requests of one download."""

    monkeypatch.setattr(cp, "DOWNLOAD_RETRY_SECONDS", 0.0)


def no_progress() -> Progress:
    """Return a progress display that shows nothing."""

    return Progress(io.StringIO(), 0, 0)


def run_cp(
    monkeypatch: pytest.MonkeyPatch,
    args: List[str],
    resolved: List[ResolvedArg],
) -> Optional[bool]:
    monkeypatch.setattr(cp, "get_api_context", lambda parsed: None)
    monkeypatch.setattr(cp, "parse_files", lambda context, files: resolved)
    return cp.drive_cp(args)


def test_wildcard_source_copies_each_match_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
):

    source = source_tree()
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-r", "folder/*", "dest"], resolved)
    assert flatten(dest) == {
        "top.csv": b"top",
        "Fire incidents/2026.csv": b"fire",
        "Geospatial assets/nested/deep.geojson": b"deep",
    }


def test_plain_source_merges_contents_into_destination(
    monkeypatch: pytest.MonkeyPatch,
):

    source = source_tree()
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg([source], False),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-r", "folder", "dest"], resolved)
    assert flatten(dest) == {
        "top.csv": b"top",
        "Fire incidents/2026.csv": b"fire",
        "Geospatial assets/nested/deep.geojson": b"deep",
    }


def test_wildcard_source_that_matches_one_directory_keeps_that_name(
    monkeypatch: pytest.MonkeyPatch,
):

    source = FakeEntry("folder", True)
    only = source.add(FakeEntry("only", True))
    only.add(FakeEntry("a.csv", False, b"a"))
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-r", "folder/*", "dest"], resolved)
    assert flatten(dest) == {"only/a.csv": b"a"}


def test_recursive_copy_into_an_existing_tree_merges(
    monkeypatch: pytest.MonkeyPatch,
):
    source = source_tree()
    dest = FakeEntry("dest", True)
    existing = dest.add(FakeEntry("Fire incidents", True))
    existing.add(FakeEntry("2025.csv", False, b"old"))

    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-r", "folder/*", "dest"], resolved)
    assert flatten(dest) == {
        "top.csv": b"top",
        "Fire incidents/2025.csv": b"old",
        "Fire incidents/2026.csv": b"fire",
        "Geospatial assets/nested/deep.geojson": b"deep",
    }


def test_several_wildcard_arguments_all_copy(monkeypatch: pytest.MonkeyPatch):
    first = FakeEntry("first", True)
    first.add(FakeEntry("a.csv", False, b"a"))
    second = FakeEntry("second", True)
    second.add(FakeEntry("b.csv", False, b"b"))
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg(first.collect(), True),
        ResolvedArg(second.collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-r", "first/*", "second/*", "dest"], resolved)
    assert flatten(dest) == {"a.csv": b"a", "b.csv": b"b"}


def test_recursive_copy_rejects_a_file_destination(monkeypatch: pytest.MonkeyPatch):
    source = source_tree()
    dest = FakeEntry("dest.csv", False)

    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([dest], False),
    ]

    with pytest.raises(Exception, match="Destination must be a directory"):
        run_cp(monkeypatch, ["-r", "folder/*", "dest.csv"], resolved)


def test_recursive_copy_rejects_a_plain_file_source(monkeypatch: pytest.MonkeyPatch):
    source = FakeEntry("a.csv", False, b"a")
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg([source], False),
        ResolvedArg([dest], False),
    ]

    with pytest.raises(Exception, match="Source must be a directory"):
        run_cp(monkeypatch, ["-r", "a.csv", "dest"], resolved)


def test_recursive_copy_rejects_a_source_that_matches_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg([], True),
        ResolvedArg([dest], False),
    ]

    with pytest.raises(Exception, match="Expected a source directory"):
        run_cp(monkeypatch, ["-r", "folder/*", "dest"], resolved)


def test_destination_that_matches_several_entries_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
):
    source = source_tree()
    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([FakeEntry("one", True), FakeEntry("two", True)], True),
    ]

    with pytest.raises(Exception, match="must name a single file or directory"):
        run_cp(monkeypatch, ["-r", "folder/*", "dest/*"], resolved)


ROOT = "05005b63-7183-a068-417b-b73392b83856:"


class FakeListing:
    def __init__(self, slots: List[str]):
        self.slots = slots


def classify_drive_arg(monkeypatch: pytest.MonkeyPatch, arg: str) -> ResolvedArg:
    monkeypatch.setattr(cp, "ls", lambda context, root, path: FakeListing(["a", "b"]))
    monkeypatch.setattr(cp, "DriveEntry", lambda context, slot: FakeEntry(slot, True))
    return cp.parse_files(cast(RequestContext, None), [arg, "unused"])[0]


def classify_local_arg(monkeypatch: pytest.MonkeyPatch, arg: str) -> ResolvedArg:
    monkeypatch.setattr(cp.glob, "glob", lambda pattern: ["a", "b"])
    monkeypatch.setattr(cp, "LocalEntry", lambda path: FakeEntry(path, True))
    return cp.parse_files(cast(RequestContext, None), [arg, "unused"])[0]


@pytest.mark.parametrize(
    "arg,wildcard",
    [
        (f"{ROOT}/folder/*", True),
        # The drive treats a final slash as a wildcard.
        (f"{ROOT}/folder/", True),
        (f"{ROOT}/folder", False),
    ],
)
def test_drive_argument_wildcard_classification(
    monkeypatch: pytest.MonkeyPatch, arg: str, wildcard: bool
):
    assert classify_drive_arg(monkeypatch, arg).wildcard == wildcard


@pytest.mark.parametrize(
    "arg,wildcard",
    [
        ("/tmp/folder/*", True),
        # A local trailing slash still names the directory itself.
        ("/tmp/folder/", False),
        ("/tmp/folder", False),
    ],
)
def test_local_argument_wildcard_classification(
    monkeypatch: pytest.MonkeyPatch, arg: str, wildcard: bool
):
    assert classify_local_arg(monkeypatch, arg).wildcard == wildcard


def test_local_mkdir_accepts_an_existing_directory(tmp_path: object):
    entry = cp.LocalEntry(str(tmp_path))
    first = entry.mkdir("sub")
    second = entry.mkdir("sub")
    assert first._path == second._path


def test_non_recursive_copy_still_copies_files(monkeypatch: pytest.MonkeyPatch):
    first = FakeEntry("a.csv", False, b"a")
    second = FakeEntry("b.csv", False, b"b")
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg([first, second], True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["folder/*", "dest"], resolved)
    assert flatten(dest) == {"a.csv": b"a", "b.csv": b"b"}


def test_non_recursive_copy_honours_the_time_range(monkeypatch: pytest.MonkeyPatch):
    sources = [
        FakeEntry("early.csv", False, b"early", time=99.0),
        FakeEntry("first.csv", False, b"first", time=100.0),
        FakeEntry("last.csv", False, b"last", time=200.0),
        FakeEntry("late.csv", False, b"late", time=201.0),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    args = ["-start", "100", "-end", "200", "folder/*", "dest"]
    assert run_cp(monkeypatch, args, resolved)
    assert flatten(dest) == {"first.csv": b"first", "last.csv": b"last"}


def test_non_recursive_copy_skips_directories(monkeypatch: pytest.MonkeyPatch):
    source = source_tree()
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["folder/*", "dest"], resolved)
    assert flatten(dest) == {"top.csv": b"top"}


# Copy entries by ID.

PARENT_ID = uuid.UUID("05005b63-7183-a068-417b-b73392b83856")
FILE_ID = uuid.UUID("0500addd-0f04-fe98-4ff2-bdef69cb097d")
OTHER_ID = uuid.UUID("0500e62f-9ac7-6323-85d6-0ca330b10480")


def entry_object(parent: uuid.UUID) -> DataCatalogObject:
    """The data catalog object of a drive entry whose parent is `parent`."""

    obj = DataCatalogObject.make_default()
    entry = DirectoryEntry(Entry.make_default(), ObjectId.from_uuid(parent))
    obj.obj = entry.to_bytes()
    return obj


def file_object(block_sizes: Sequence[int]) -> DataCatalogObject:
    """The data catalog object of a file with one block of each size."""

    file = File.make_default()
    file.chunks = [
        Chunk(GenericId.make_default(), Digest.make_default(), size)
        for size in block_sizes
    ]
    obj = DataCatalogObject.make_default()
    obj.obj = DirectoryEntry(Entry(file), ObjectId.from_uuid(PARENT_ID)).to_bytes()
    return obj


def file_slot(id: uuid.UUID, name: str, time_ms: int = 0, size: int = 3) -> ListSlot:
    """A listing slot for a file, as `ls` returns it."""

    entry = ListEntry(ListFile("text/csv", 3, None))
    return ListSlot(None, entry, ObjectId.from_uuid(id), None, name, size, time_ms, 0)


def root_slot(id: uuid.UUID, name: str) -> ListSlot:
    """A listing slot for a drive root, as `get_roots` returns it."""

    entry = ListEntry(TopLevelDirectory.make_default())
    return ListSlot(None, entry, ObjectId.from_uuid(id), None, name, 0, 0, 0)


def unexpected_call(*args: object) -> NoReturn:
    raise AssertionError(f"unexpected SDK call with {args}")


def test_bare_file_id_resolves_through_the_parent_listing(
    monkeypatch: pytest.MonkeyPatch,
):

    listed = []

    def fake_ls(context: RequestContext, root: str, tail: str) -> DirectoryList:
        listed.append((root, tail))
        return DirectoryList(
            [file_slot(OTHER_ID, "other.csv"), file_slot(FILE_ID, "a.csv", 1500)]
        )

    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(cp, "ls", fake_ls)
    monkeypatch.setattr(cp, "get_roots", unexpected_call)

    entry = cp.get_dir_list_slot(cast(RequestContext, None), str(FILE_ID))

    assert listed == [(str(PARENT_ID), "*")]
    assert entry.name() == "a.csv"
    assert not entry.isdir()
    assert entry.time() == 1.5
    assert entry._oid == FILE_ID


def test_bare_root_id_resolves_through_the_drive_roots(
    monkeypatch: pytest.MonkeyPatch,
):

    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(cp.NIL_UUID))
    monkeypatch.setattr(cp, "ls", unexpected_call)
    monkeypatch.setattr(
        cp,
        "get_roots",
        lambda context: DirectoryList([root_slot(PARENT_ID, "Group drive")]),
    )

    entry = cp.get_dir_list_slot(cast(RequestContext, None), str(PARENT_ID))

    assert entry.name() == "Group drive"
    assert entry.isdir()


def test_bare_id_missing_from_the_parent_listing_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(
        cp,
        "ls",
        lambda context, root, tail: DirectoryList([file_slot(OTHER_ID, "other.csv")]),
    )

    with pytest.raises(ValueError, match="Could not find entry"):
        cp.get_dir_list_slot(cast(RequestContext, None), str(FILE_ID))


def test_parse_files_resolves_a_bare_id_as_one_plain_entry(
    monkeypatch: pytest.MonkeyPatch,
):
    resolved = FakeEntry("a.csv", False, b"a")
    monkeypatch.setattr(cp, "get_dir_list_slot", lambda context, id: resolved)
    monkeypatch.setattr(cp.glob, "glob", lambda pattern: [pattern])
    monkeypatch.setattr(cp, "LocalEntry", lambda path: FakeEntry(path, True))

    args = cp.parse_files(cast(RequestContext, None), [str(FILE_ID), "dest"])

    assert args[0] == ResolvedArg([resolved], False)


def test_parse_files_rejects_a_path_under_a_bare_id():
    with pytest.raises(Exception, match="<guid>:/<path>"):
        cp.parse_files(cast(RequestContext, None), [f"{FILE_ID}/a.csv", "dest"])


def test_copy_by_file_id_downloads_the_file_into_a_local_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def fake_read_drive_file(
        context: RequestContext, id: uuid.UUID, block_size: int
    ) -> Iterator[bytes]:
        assert id == FILE_ID
        yield b"ab"
        yield b"c"

    monkeypatch.setattr(cp, "get_api_context", lambda parsed: None)
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(
        cp,
        "ls",
        lambda context, root, tail: DirectoryList([file_slot(FILE_ID, "a.csv")]),
    )
    monkeypatch.setattr(cp, "read_drive_file", fake_read_drive_file)

    assert cp.drive_cp([str(FILE_ID), str(tmp_path)])

    assert (tmp_path / "a.csv").read_bytes() == b"abc"
    assert os.listdir(tmp_path) == ["a.csv"]


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def http_error(status_code: int) -> HTTPError:
    return HTTPError(response=cast(Response, FakeResponse(status_code)))


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[int, bytes]]:
    """Record each chunk the drive accepts, as (index, bytes), for a 4-byte chunk size."""
    accepted: List[Tuple[int, bytes]] = []

    def fake_create_entry(context, parent, name, ty, mime, chunks):
        return SimpleNamespace(id=ObjectId.from_uuid(FILE_ID))

    def fake_put_file_chunk(context, id, index, hash, chunk):
        assert uuid_from_id(id) == FILE_ID
        assert hash == hashlib.sha256(chunk).hexdigest()
        accepted.append((index, chunk))

    monkeypatch.setattr(cp, "CHUNK_SIZE", 4)
    monkeypatch.setattr(cp, "create_entry", fake_create_entry)
    monkeypatch.setattr(cp, "put_file_chunk", fake_put_file_chunk)
    monkeypatch.setattr(cp.time, "sleep", lambda seconds: None)
    return accepted


def test_put_file_sends_a_one_chunk_file_once_at_index_zero(
    uploads: List[Tuple[int, bytes]],
):
    cp.put_file(cast(RequestContext, None), PARENT_ID, [b"abc"], 3, "a.csv")

    assert uploads == [(0, b"abc")]


def test_put_file_sends_each_chunk_once_at_its_own_index(
    uploads: List[Tuple[int, bytes]],
):

    blocks = [b"abcde", b"f", b"ghij"]
    cp.put_file(cast(RequestContext, None), PARENT_ID, blocks, 10, "a.csv")

    assert uploads == [(0, b"abcd"), (1, b"efgh"), (2, b"ij")]


def test_put_file_reads_a_local_file_one_chunk_at_a_time(
    uploads: List[Tuple[int, bytes]], tmp_path: Path
):

    path = tmp_path / "a.csv"
    path.write_bytes(b"abcdefghij")
    source = cp.LocalEntry(str(path))
    handed_out: List[int] = []

    def blocks() -> Iterator[bytes]:
        for block in source.read(cp.CHUNK_SIZE):
            # Every chunk read before this one is already with the drive.
            assert len(uploads) == len(handed_out)
            handed_out.append(len(block))
            yield block

    cp.put_file(cast(RequestContext, None), PARENT_ID, blocks(), source.size(), "a.csv")

    assert handed_out == [4, 4, 2]
    assert uploads == [(0, b"abcd"), (1, b"efgh"), (2, b"ij")]


def test_put_file_names_the_number_of_chunks_and_the_type_up_front(
    monkeypatch: pytest.MonkeyPatch, uploads: List[Tuple[int, bytes]]
):
    created: List[Tuple[str, str, int]] = []

    def fake_create_entry(context, parent, name, ty, mime, chunks):
        assert uploads == []
        created.append((name, mime, chunks))
        return SimpleNamespace(id=ObjectId.from_uuid(FILE_ID))

    # Use a block large enough to identify the file type.
    monkeypatch.setattr(cp, "CHUNK_SIZE", 8)
    monkeypatch.setattr(cp, "create_entry", fake_create_entry)

    blocks = [b"%PDF", b"-1.4\n%", b"more content"]
    cp.put_file(cast(RequestContext, None), PARENT_ID, blocks, 22, "a.pdf")

    assert created == [("a.pdf", "application/pdf", 3)]
    assert [chunk for index, chunk in uploads] == [b"%PDF-1.4", b"\n%more c", b"ontent"]


@pytest.mark.parametrize(
    "content", [b"", b"abcdef", b"abcdefghi", b"abcdefghijk", b"abcdefghijklmnop"]
)
def test_put_file_rejects_content_that_does_not_match_the_size(
    uploads: List[Tuple[int, bytes]], content: bytes
):
    blocks = (content[i : i + 4] for i in range(0, len(content), 4))
    with pytest.raises(OSError, match="Expected 10 bytes"):
        cp.put_file(cast(RequestContext, None), PARENT_ID, blocks, 10, "a.csv")
    assert len(uploads) <= 2


def test_put_chunk_tries_again_at_the_same_index_after_a_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch, uploads: List[Tuple[int, bytes]]
):
    answers = [http_error(514), http_error(514), None]
    indexes: List[int] = []

    def retrying_put_file_chunk(context, id, index, hash, chunk):
        indexes.append(index)
        answer = answers.pop(0)
        if answer is not None:
            raise answer

    monkeypatch.setattr(cp, "put_file_chunk", retrying_put_file_chunk)

    cp.put_chunk(cast(RequestContext, None), FILE_ID, 3, b"abc")

    assert indexes == [3, 3, 3]


def test_put_chunk_raises_any_other_error_at_once(
    monkeypatch: pytest.MonkeyPatch, uploads: List[Tuple[int, bytes]]
):
    calls: List[int] = []

    def failing_put_file_chunk(context, id, index, hash, chunk):
        calls.append(index)
        raise http_error(400)

    monkeypatch.setattr(cp, "put_file_chunk", failing_put_file_chunk)

    with pytest.raises(HTTPError):
        cp.put_chunk(cast(RequestContext, None), FILE_ID, 0, b"abc")

    assert calls == [0]


def test_put_chunk_raises_when_the_hash_mismatch_does_not_clear(
    monkeypatch: pytest.MonkeyPatch, uploads: List[Tuple[int, bytes]]
):
    calls: List[int] = []

    def failing_put_file_chunk(context, id, index, hash, chunk):
        calls.append(index)
        raise http_error(514)

    monkeypatch.setattr(cp, "put_file_chunk", failing_put_file_chunk)

    with pytest.raises(HTTPError):
        cp.put_chunk(cast(RequestContext, None), FILE_ID, 0, b"abc")

    assert len(calls) == cp.CHUNK_UPLOAD_ATTEMPTS


# Check block sizes and memory use.


def partial_files(directory: Path) -> List[str]:
    """Return the temporary file names in `directory`."""

    return [name for name in os.listdir(directory) if name.endswith(cp.PARTIAL_SUFFIX)]


@pytest.mark.parametrize(
    "pieces,blocks",
    [
        ([], []),
        ([b""], []),
        ([b"ab"], [b"ab"]),
        ([b"abcd"], [b"abcd"]),
        ([b"abcdefghij"], [b"abcd", b"efgh", b"ij"]),
        ([b"a", b"b", b"c", b"d", b"e"], [b"abcd", b"e"]),
        ([b"abc", b"defgh", b"", b"ijkl"], [b"abcd", b"efgh", b"ijkl"]),
        ([b"abcd", b"efgh"], [b"abcd", b"efgh"]),
    ],
)
def test_file_blocks_regroups_pieces_into_blocks_of_the_asked_size(
    pieces: List[bytes], blocks: List[bytes]
):
    assert list(cp.file_blocks(pieces, 4)) == blocks


def test_file_blocks_passes_a_whole_block_through_without_a_copy():
    block = b"abcd"

    assert next(cp.file_blocks([block], 4)) is block


def test_local_entry_reads_its_content_in_blocks(tmp_path: Path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"abcdefghij")
    entry = cp.LocalEntry(str(path))

    assert entry.size() == 10
    assert list(entry.read(4)) == [b"abcd", b"efgh", b"ij"]


def test_download_holds_one_block_of_a_large_file_in_memory(tmp_path: Path):

    block = bytes(cp.BLOCK_SIZE)
    num_blocks = 64

    tracemalloc.start()
    try:
        dest = cp.LocalEntry(str(tmp_path))
        dest.put((block for _ in range(num_blocks)), num_blocks * len(block), "big")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (tmp_path / "big").stat().st_size == num_blocks * cp.BLOCK_SIZE
    assert peak < 4 * cp.BLOCK_SIZE


def test_upload_holds_one_chunk_of_a_large_file_in_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):

    chunk_size = 4 * 1024 * 1024
    num_chunks = 8
    path = tmp_path / "big"
    with open(path, "wb") as f:
        for _ in range(num_chunks):
            f.write(os.urandom(chunk_size))

    digest = hashlib.sha256()
    indexes: List[int] = []

    def fake_put_file_chunk(context, id, index, hash, chunk):
        indexes.append(index)
        digest.update(chunk)

    monkeypatch.setattr(cp, "CHUNK_SIZE", chunk_size)
    monkeypatch.setattr(
        cp,
        "create_entry",
        lambda *args: SimpleNamespace(id=ObjectId.from_uuid(FILE_ID)),
    )
    monkeypatch.setattr(cp, "put_file_chunk", fake_put_file_chunk)

    source = cp.LocalEntry(str(path))
    dest = cp.DriveEntry(cast(RequestContext, None), root_slot(PARENT_ID, "dir"))
    tracemalloc.start()
    try:
        cp.copy_file(Transfer(source, dest), threading.Event(), no_progress())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert indexes == list(range(num_chunks))
    assert digest.hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()
    assert peak < 1.5 * chunk_size


def test_drive_to_drive_copy_regroups_the_download_into_chunks(
    monkeypatch: pytest.MonkeyPatch, uploads: List[Tuple[int, bytes]]
):
    def fake_read_drive_file(
        context: RequestContext, id: uuid.UUID, block_size: int
    ) -> Iterator[bytes]:
        assert id == FILE_ID
        yield from (b"abc", b"de", b"fghij")

    monkeypatch.setattr(cp, "read_drive_file", fake_read_drive_file)
    context = cast(RequestContext, None)
    slot = file_slot(FILE_ID, "a.csv")
    slot.size = 10
    source = cp.DriveEntry(context, slot)
    dest = cp.DriveEntry(context, root_slot(PARENT_ID, "dir"))

    cp.copy_file(Transfer(source, dest), threading.Event(), no_progress())

    assert uploads == [(0, b"abcd"), (1, b"efgh"), (2, b"ij")]


# Replace local files only after the copy completes.


def failing_blocks(*blocks: bytes) -> Iterator[bytes]:
    """Return `blocks`, then report a connection failure."""

    yield from blocks
    raise requests.ConnectionError("connection dropped")


def unexpected_blocks() -> Iterator[bytes]:
    """Fail if the copy reads from this source."""

    raise AssertionError("the source was read")
    yield b""


def test_write_complete_file_writes_the_content_and_leaves_no_partial_file(
    tmp_path: Path,
):
    path = tmp_path / "a.csv"

    cp.write_complete_file(str(path), [b"ab", b"", b"cd"])

    assert path.read_bytes() == b"abcd"
    assert os.listdir(tmp_path) == ["a.csv"]


def test_write_complete_file_writes_an_empty_file(tmp_path: Path):
    path = tmp_path / "empty"

    cp.write_complete_file(str(path), [])

    assert path.read_bytes() == b""


def test_write_complete_file_keeps_the_final_name_free_until_the_last_block(
    tmp_path: Path,
):

    path = tmp_path / "a.csv"
    seen: List[Tuple[bool, List[str], int]] = []

    def blocks() -> Iterator[bytes]:
        for block in (b"ab", b"cd"):
            yield block
            names = partial_files(tmp_path)
            size = os.path.getsize(tmp_path / names[0]) if names else -1
            seen.append((path.exists(), names, size))

    cp.write_complete_file(str(path), blocks())

    assert [exists for exists, names, size in seen] == [False, False]
    names = seen[0][1]
    assert len(names) == 1
    assert names[0].startswith(".a.csv.")
    assert names == seen[1][1]
    assert path.read_bytes() == b"abcd"


def test_write_complete_file_leaves_nothing_behind_when_the_source_fails(
    tmp_path: Path,
):
    path = tmp_path / "a.csv"

    with pytest.raises(requests.ConnectionError):
        cp.write_complete_file(str(path), failing_blocks(b"ab"))

    assert os.listdir(tmp_path) == []


def test_write_complete_file_keeps_the_old_content_when_the_source_fails(
    tmp_path: Path,
):
    path = tmp_path / "a.csv"
    path.write_bytes(b"old content")

    with pytest.raises(requests.ConnectionError):
        cp.write_complete_file(str(path), failing_blocks(b"new"))

    assert path.read_bytes() == b"old content"
    assert os.listdir(tmp_path) == ["a.csv"]


def test_write_complete_file_cleans_up_after_a_keyboard_interrupt(tmp_path: Path):
    path = tmp_path / "a.csv"

    def blocks() -> Iterator[bytes]:
        yield b"ab"
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        cp.write_complete_file(str(path), blocks())

    assert os.listdir(tmp_path) == []


def test_write_complete_file_puts_the_content_on_disk_before_it_renames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):

    path = tmp_path / "a.csv"
    steps: List[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd: int):
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "content"
        size = os.fstat(fd).st_size
        steps.append(f"sync {kind}" if kind == "directory" else f"sync {size} bytes")
        real_fsync(fd)

    def replace(src: str, dst: str):
        steps.append(f"rename to {os.path.basename(dst)}")
        real_replace(src, dst)

    monkeypatch.setattr(cp.os, "fsync", fsync)
    monkeypatch.setattr(cp.os, "replace", replace)

    cp.write_complete_file(str(path), [b"ab", b"cd"])

    assert steps == ["sync 4 bytes", "rename to a.csv", "sync directory"]


def test_write_complete_file_replaces_a_file_and_keeps_its_permissions(tmp_path: Path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"old")
    path.chmod(0o640)

    cp.write_complete_file(str(path), [b"new"])

    assert path.read_bytes() == b"new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_write_complete_file_limits_a_new_file_to_its_owner(tmp_path: Path):
    path = tmp_path / "a.csv"

    cp.write_complete_file(str(path), [b"new"])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a user that file permissions apply to",
)
def test_write_complete_file_refuses_a_read_only_file_before_it_reads_the_source(
    tmp_path: Path,
):
    path = tmp_path / "a.csv"
    path.write_bytes(b"old")
    path.chmod(0o440)

    with pytest.raises(PermissionError):
        cp.write_complete_file(str(path), unexpected_blocks())

    assert path.read_bytes() == b"old"
    assert os.listdir(tmp_path) == ["a.csv"]


def test_write_complete_file_refuses_a_directory_before_it_reads_the_source(
    tmp_path: Path,
):
    path = tmp_path / "taken"
    path.mkdir()

    with pytest.raises(IsADirectoryError):
        cp.write_complete_file(str(path), unexpected_blocks())

    assert os.listdir(tmp_path) == ["taken"]


def test_write_complete_file_writes_through_a_symbolic_link(tmp_path: Path):

    real = tmp_path / "real"
    real.mkdir()
    (real / "a.csv").write_bytes(b"old")
    link = tmp_path / "link.csv"
    link.symlink_to(real / "a.csv")

    cp.write_complete_file(str(link), [b"new"])

    assert link.is_symlink()
    assert (real / "a.csv").read_bytes() == b"new"
    assert os.listdir(real) == ["a.csv"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_write_complete_file_streams_into_a_file_that_is_not_regular(tmp_path: Path):

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    received: List[bytes] = []

    def drain():
        with open(fifo, "rb") as f:
            received.append(f.read())

    reader = threading.Thread(target=drain)
    reader.start()
    cp.write_complete_file(str(fifo), [b"ab", b"cd"])
    reader.join(10)

    assert received == [b"abcd"]
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    assert os.listdir(tmp_path) == ["fifo"]


def test_write_complete_file_handles_the_longest_file_name(tmp_path: Path):

    path = tmp_path / ("\N{SNOWMAN}" * 85)
    assert len(path.name.encode()) == 255

    cp.write_complete_file(str(path), [b"abcd"])

    assert path.read_bytes() == b"abcd"


def test_local_put_into_a_directory_writes_the_named_file(tmp_path: Path):
    cp.LocalEntry(str(tmp_path)).put([b"ab", b"cd"], 4, "a.csv")

    assert (tmp_path / "a.csv").read_bytes() == b"abcd"


def test_local_put_onto_a_file_replaces_that_file(tmp_path: Path):
    path = tmp_path / "b.csv"
    path.write_bytes(b"old")

    cp.LocalEntry(str(path)).put([b"new"], 3, "a.csv")

    assert path.read_bytes() == b"new"
    assert os.listdir(tmp_path) == ["b.csv"]


def test_copy_of_a_local_file_onto_itself_keeps_its_content(tmp_path: Path):

    path = tmp_path / "a.csv"
    content = os.urandom(3 * cp.BLOCK_SIZE + 5)
    path.write_bytes(content)
    entry = cp.LocalEntry(str(path))

    cp.copy_file(Transfer(entry, entry), threading.Event(), no_progress())

    assert path.read_bytes() == content
    assert os.listdir(tmp_path) == ["a.csv"]


def test_recursive_upload_does_not_pick_up_a_partial_file(tmp_path: Path):

    (tmp_path / "a.csv").write_bytes(b"a")
    (tmp_path / f".b.csv.0a1b2c3d{cp.PARTIAL_SUFFIX}").write_bytes(b"half of b")

    names = [entry.name() for entry in cp.LocalEntry(str(tmp_path)).collect()]

    assert names == ["a.csv"]


# Test downloads with a local server.


def api_key_context() -> ApiKeyContext:
    """Create a test key to sign requests."""

    secret = base64.b64encode(os.urandom(32)).decode().rstrip("=")
    key = Key(uuid.UUID(int=1), Region.CA, "access-key", secret)
    return ApiKeyContext(key, Environment.Stage)


class FakeDrive:
    """An HTTP server that answers a file download with a prepared response."""

    def __init__(self):
        self.requests: List[Tuple[str, Dict[str, str]]] = []
        self.respond: Callable[[http.server.BaseHTTPRequestHandler], None]
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append((self.path, dict(self.headers.items())))
                fake.respond(self)

            def log_message(self, format: str, *args: object):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Check frequently so that the server stops quickly.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._server.server_port}{path}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDrive]:
    fake = FakeDrive()
    monkeypatch.setattr(
        "ulsdk.api_key_context._get_endpoint",
        lambda region, env, path: fake.url(path),
    )
    yield fake
    fake.close()


def respond_with(
    body: bytes, status: int = 200, headers: Optional[Dict[str, str]] = None
) -> Callable[[http.server.BaseHTTPRequestHandler], None]:
    """Answer with `body` and a `Content-Length` that matches it."""

    def respond(handler: http.server.BaseHTTPRequestHandler):
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)

    return respond


def test_read_drive_file_yields_the_file_in_pieces(drive: FakeDrive):
    body = os.urandom(3 * cp.BLOCK_SIZE + 123)
    drive.respond = respond_with(body)

    pieces = list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert b"".join(pieces) == body
    assert len(pieces) == 4
    assert max(len(piece) for piece in pieces) == cp.BLOCK_SIZE


def test_read_drive_file_reads_from_the_network_in_small_pieces(drive: FakeDrive):

    body = os.urandom(2 * cp.BLOCK_SIZE)
    drive.respond = respond_with(body)

    pieces = list(cp.read_drive_file(api_key_context(), FILE_ID, cp.CHUNK_SIZE))

    assert b"".join(pieces) == body
    assert max(len(piece) for piece in pieces) <= cp.BLOCK_SIZE


def test_read_drive_file_sends_the_signed_request_that_the_sdk_sends(drive: FakeDrive):
    drive.respond = respond_with(b"abc")

    assert b"".join(cp.read_drive_file(api_key_context(), FILE_ID, 4)) == b"abc"

    [(path, headers)] = drive.requests
    assert path == f"/v1/api/ulv2/drive/file/{FILE_ID}"
    lowered = {name.lower(): value for name, value in headers.items()}
    assert lowered["authorization"].startswith("UL1-ED25519 Credential=access-key/")
    assert "x-ul-date" in lowered


def test_read_drive_file_sets_a_timeout_and_follows_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
):

    sent: List[Dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> Response:
        sent.append(kwargs)
        response = Response()
        response.status_code = 200
        response.raw = io.BytesIO(b"abc")
        return response

    monkeypatch.setattr(requests, "get", fake_get)

    assert b"".join(cp.read_drive_file(api_key_context(), FILE_ID, 4)) == b"abc"

    [kwargs] = sent
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == cp.DOWNLOAD_TIMEOUT
    assert kwargs["allow_redirects"] is False


def test_read_drive_file_splits_a_complete_response_into_blocks():
    class Context(SimpleNamespace):
        get_stream = RequestContext.get_stream

        def get(self, path: str, params: object, headers: object, **kwargs: object):
            return b"0123456789"

    pieces = list(cp.read_drive_file(cast(RequestContext, Context()), FILE_ID, 4))

    assert pieces == [b"0123", b"4567", b"89"]


def test_read_drive_file_decodes_a_compressed_response(drive: FakeDrive):

    body = b"a,b,c\n" * 100_000
    drive.respond = respond_with(
        gzip.compress(body), headers={"Content-Encoding": "gzip"}
    )

    pieces = list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert b"".join(pieces) == body
    [(path, headers)] = drive.requests
    lowered = {name.lower(): value for name, value in headers.items()}
    assert "gzip" in lowered["accept-encoding"]


def test_read_drive_file_raises_the_error_status_of_the_drive(drive: FakeDrive):
    drive.respond = respond_with(b"no such file", status=404)

    with pytest.raises(HTTPError, match="404 Client Error"):
        list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))


@pytest.mark.parametrize("status", [400, 403, 500])
def test_read_drive_file_makes_one_request_for_an_error_status(
    drive: FakeDrive, status: int
):
    drive.respond = respond_with(b"no", status=status)

    with pytest.raises(HTTPError):
        list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert len(drive.requests) == 1


def test_read_drive_file_names_an_upload_that_did_not_complete(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDrive
):
    drive.respond = respond_with(b"Unable to find entry", status=404)
    monkeypatch.setattr(
        cp, "get_object", lambda context, id: file_object([4, 4, 0, 0, 0])
    )

    with pytest.raises(OSError, match="The drive has 2 of 5 blocks") as failure:
        list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert str(failure.value) == (
        "The upload of this file did not complete. The drive has 2 of 5 blocks."
    )
    assert type(failure.value) is OSError
    assert isinstance(failure.value.__cause__, HTTPError)
    assert len(drive.requests) == 1


@pytest.mark.parametrize(
    "entry", [file_object([4, 4]), file_object([]), entry_object(PARENT_ID)]
)
def test_read_drive_file_keeps_the_error_status_of_a_complete_file(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDrive, entry: DataCatalogObject
):
    drive.respond = respond_with(b"no such file", status=404)
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry)

    with pytest.raises(HTTPError, match="404 Client Error"):
        list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))


TAG = '"0500f1d0-5d3a-4d0c-9f3e-0c6f1b7a2e11"'


def respond_like_the_drive(
    body: bytes, limits: Sequence[int] = (), tag: Optional[str] = TAG
) -> Callable[[http.server.BaseHTTPRequestHandler], None]:
    """Answer as the drive does. Send the bytes from the start of `Range` if
    `If-Range` has `tag`. If not, send the complete file.

    For request number `n`, send at most `limits[n]` bytes of the content.
    Then drop the connection."""

    numbers = itertools.count()

    def respond(handler: http.server.BaseHTTPRequestHandler):
        number = next(numbers)
        start = 0
        requested = handler.headers["Range"]
        if requested and tag is not None and handler.headers["If-Range"] == tag:
            start = int(requested.removeprefix("bytes=").removesuffix("-"))
        content = body[start:]
        handler.send_response(206 if start else 200)
        handler.send_header("Content-Length", str(len(content)))
        if tag is not None:
            handler.send_header("ETag", tag)
        if start:
            end, size = len(body) - 1, len(body)
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if number < len(limits):
            handler.wfile.write(content[: limits[number]])
            handler.close_connection = True
        else:
            handler.wfile.write(content)

    return respond


def respond_in_turn(
    *responders: Callable[[http.server.BaseHTTPRequestHandler], None],
) -> Callable[[http.server.BaseHTTPRequestHandler], None]:
    """Answer each request with the next responder. Repeat the last one."""

    remaining = list(responders)

    def respond(handler: http.server.BaseHTTPRequestHandler):
        responder = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        responder(handler)

    return respond


def sent_ranges(drive: FakeDrive) -> List[Tuple[Optional[str], Optional[str]]]:
    """Return the `Range` and `If-Range` headers of each request."""

    ranges = []
    for _, headers in drive.requests:
        lowered = {name.lower(): value for name, value in headers.items()}
        ranges.append((lowered.get("range"), lowered.get("if-range")))
    return ranges


def test_read_drive_file_continues_after_a_response_that_ends_early(drive: FakeDrive):
    body = os.urandom(2 * cp.BLOCK_SIZE + 123)
    drive.respond = respond_like_the_drive(body, limits=[cp.BLOCK_SIZE])

    pieces = list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert b"".join(pieces) == body
    assert sent_ranges(drive) == [(None, None), (f"bytes={cp.BLOCK_SIZE}-", TAG)]


def test_read_drive_file_continues_as_long_as_content_arrives(drive: FakeDrive):
    requests_with_content = cp.DOWNLOAD_ATTEMPTS + 2
    body = os.urandom(requests_with_content * cp.BLOCK_SIZE)
    limits = [cp.BLOCK_SIZE] * (requests_with_content - 1)
    drive.respond = respond_like_the_drive(body, limits)

    pieces = list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert b"".join(pieces) == body
    assert [start for start, _ in sent_ranges(drive)] == [None] + [
        f"bytes={number * cp.BLOCK_SIZE}-" for number in range(1, requests_with_content)
    ]


def test_read_drive_file_starts_again_if_no_content_arrived(drive: FakeDrive):
    body = os.urandom(cp.BLOCK_SIZE + 5)
    drive.respond = respond_like_the_drive(body, limits=[0, 0])

    pieces = list(cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE))

    assert b"".join(pieces) == body
    assert sent_ranges(drive) == [(None, None)] * 3


def test_read_drive_file_stops_after_failures_in_a_row_without_content(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDrive
):
    body = os.urandom(2 * cp.BLOCK_SIZE)
    limits = [cp.BLOCK_SIZE] + [0] * cp.DOWNLOAD_ATTEMPTS
    drive.respond = respond_like_the_drive(body, limits)
    waits: List[float] = []
    monkeypatch.setattr(cp, "DOWNLOAD_RETRY_SECONDS", 1.0)
    monkeypatch.setattr(cp.time, "sleep", waits.append)
    blocks = cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE)

    assert next(blocks) == body[: cp.BLOCK_SIZE]
    with pytest.raises(requests.RequestException):
        next(blocks)

    assert len(drive.requests) == cp.DOWNLOAD_ATTEMPTS
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_read_drive_file_fails_if_the_file_changed(drive: FakeDrive):
    body = os.urandom(2 * cp.BLOCK_SIZE)
    drive.respond = respond_in_turn(
        respond_like_the_drive(body, limits=[cp.BLOCK_SIZE]),
        respond_like_the_drive(os.urandom(len(body)), tag='"new"'),
    )
    blocks = cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE)

    assert next(blocks) == body[: cp.BLOCK_SIZE]
    with pytest.raises(OSError, match="File changed during the copy") as failure:
        next(blocks)

    assert type(failure.value) is OSError
    assert sent_ranges(drive) == [(None, None), (f"bytes={cp.BLOCK_SIZE}-", TAG)]


@pytest.mark.parametrize("status, start", [(206, 5), (200, cp.BLOCK_SIZE)])
def test_read_drive_file_checks_the_status_and_start_of_the_remaining_bytes(
    drive: FakeDrive, status: int, start: int
):
    body = os.urandom(2 * cp.BLOCK_SIZE)
    wrong = respond_with(
        body[start:],
        status=status,
        headers={"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"},
    )
    drive.respond = respond_in_turn(
        respond_like_the_drive(body, limits=[cp.BLOCK_SIZE]), wrong
    )
    blocks = cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE)

    assert next(blocks) == body[: cp.BLOCK_SIZE]
    with pytest.raises(OSError, match="File changed during the copy"):
        next(blocks)


@pytest.mark.parametrize("tag", [None, 'W/"weak"'])
def test_read_drive_file_does_not_continue_without_a_strong_tag(
    drive: FakeDrive, tag: Optional[str]
):
    body = os.urandom(2 * cp.BLOCK_SIZE)
    drive.respond = respond_like_the_drive(body, limits=[cp.BLOCK_SIZE], tag=tag)
    blocks = cp.read_drive_file(api_key_context(), FILE_ID, cp.BLOCK_SIZE)

    assert next(blocks) == body[: cp.BLOCK_SIZE]
    with pytest.raises(requests.RequestException):
        next(blocks)

    assert len(drive.requests) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_read_drive_file_tries_again_after_a_gateway_error(
    drive: FakeDrive, status: int
):
    drive.respond = respond_in_turn(
        respond_with(b"not available", status=status), respond_with(b"abc")
    )

    assert b"".join(cp.read_drive_file(api_key_context(), FILE_ID, 4)) == b"abc"

    assert len(drive.requests) == 2


def test_read_drive_file_tries_again_after_a_timeout(monkeypatch: pytest.MonkeyPatch):
    answers: List[object] = [requests.Timeout("no answer"), io.BytesIO(b"abc")]

    def fake_get_file_stream(*args: object, **kwargs: object) -> object:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cp, "get_file_stream", fake_get_file_stream)

    pieces = list(cp.read_drive_file(cast(RequestContext, None), FILE_ID, 4))

    assert pieces == [b"abc"]
    assert answers == []


def respond_with_a_short_body(handler: http.server.BaseHTTPRequestHandler):
    """Promise 1000 bytes, send 10, and drop the connection."""

    handler.send_response(200)
    handler.send_header("Content-Length", "1000")
    handler.end_headers()
    handler.wfile.write(b"0123456789")
    handler.close_connection = True


def respond_with_unfinished_chunks(handler: http.server.BaseHTTPRequestHandler):
    """Send one chunk of a chunked body, and drop the connection before the end."""

    handler.send_response(200)
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    handler.wfile.write(b"a\r\n0123456789\r\n")
    handler.close_connection = True


@pytest.mark.parametrize(
    "respond", [respond_with_a_short_body, respond_with_unfinished_chunks]
)
def test_download_that_is_cut_short_leaves_the_destination_alone(
    monkeypatch: pytest.MonkeyPatch,
    drive: FakeDrive,
    tmp_path: Path,
    respond: Callable[[http.server.BaseHTTPRequestHandler], None],
):

    drive.respond = respond
    (tmp_path / "a.csv").write_bytes(b"old content")
    monkeypatch.setattr(cp, "get_api_context", lambda parsed: api_key_context())
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(
        cp,
        "ls",
        lambda context, root, tail: DirectoryList(
            [file_slot(FILE_ID, "a.csv", size=1000)]
        ),
    )

    with pytest.raises(requests.RequestException):
        cp.drive_cp([str(FILE_ID), str(tmp_path)])

    assert (tmp_path / "a.csv").read_bytes() == b"old content"
    assert os.listdir(tmp_path) == ["a.csv"]


def test_download_streams_from_the_drive_to_the_local_file(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDrive, tmp_path: Path
):

    body = os.urandom(5 * cp.BLOCK_SIZE + 17)
    drive.respond = respond_with(body)
    monkeypatch.setattr(cp, "get_api_context", lambda parsed: api_key_context())
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(
        cp,
        "ls",
        lambda context, root, tail: DirectoryList(
            [file_slot(FILE_ID, "a.bin", size=len(body))]
        ),
    )

    assert cp.drive_cp([str(FILE_ID), str(tmp_path)])

    assert (tmp_path / "a.bin").read_bytes() == body
    assert os.listdir(tmp_path) == ["a.bin"]


def test_download_that_ends_early_continues_into_the_same_file(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDrive, tmp_path: Path
):

    body = os.urandom(3 * cp.BLOCK_SIZE + 17)
    drive.respond = respond_like_the_drive(body, limits=[cp.BLOCK_SIZE] * 2)
    monkeypatch.setattr(cp, "get_api_context", lambda parsed: api_key_context())
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(
        cp,
        "ls",
        lambda context, root, tail: DirectoryList(
            [file_slot(FILE_ID, "a.bin", size=len(body))]
        ),
    )

    assert cp.drive_cp([str(FILE_ID), str(tmp_path)])

    assert (tmp_path / "a.bin").read_bytes() == body
    assert os.listdir(tmp_path) == ["a.bin"]
    assert len(drive.requests) == 3


@pytest.mark.parametrize("jobs", ["1", "3"])
def test_an_upload_that_did_not_complete_does_not_stop_the_other_downloads(
    monkeypatch: pytest.MonkeyPatch,
    drive: FakeDrive,
    tmp_path: Path,
    log: List[str],
    jobs: str,
):
    def respond(handler: http.server.BaseHTTPRequestHandler):
        if str(OTHER_ID) in handler.path:
            respond_with(b"Unable to find entry", status=404)(handler)
        else:
            respond_with(b"abc")(handler)

    drive.respond = respond
    slots = [
        file_slot(FILE_ID, "a.csv"),
        file_slot(OTHER_ID, "b.csv"),
        file_slot(PARENT_ID, "c.csv"),
    ]
    monkeypatch.setattr(cp, "get_api_context", lambda parsed: api_key_context())
    monkeypatch.setattr(cp, "get_object", lambda context, id: file_object([3, 0]))
    monkeypatch.setattr(cp, "ls", lambda context, root, tail: DirectoryList(slots))

    with pytest.raises(OSError, match="The drive has 1 of 2 blocks"):
        cp.drive_cp(["-j", jobs, f"{PARENT_ID}:/*", str(tmp_path)])

    assert sorted(os.listdir(tmp_path)) == ["a.csv", "c.csv"]
    assert (tmp_path / "a.csv").read_bytes() == b"abc"
    assert (tmp_path / "c.csv").read_bytes() == b"abc"
    assert (
        "Failed to copy b.csv: The upload of this file did not complete."
        " The drive has 1 of 2 blocks."
    ) in log
    assert "Copied 2 of 3 files. 1 failed." in log


def test_a_copy_that_fails_closes_its_source(tmp_path: Path):

    closed: List[bool] = []

    class Source(FakeEntry):
        def read(self, block_size: int) -> Generator[bytes, None, None]:
            try:
                yield b"ab"
                yield b"cd"
            finally:
                closed.append(True)

    class Dest(FakeEntry):
        def put(self, blocks: Iterable[bytes], size: int, filename: str):
            next(iter(blocks))
            raise OSError("disk full")

    transfer = Transfer(Source("a.csv", False, b"abcd"), Dest("dest", True))

    # Retain the error to check that cleanup does not depend on memory release.
    with pytest.raises(OSError, match="disk full") as failure:
        cp.copy_file(transfer, threading.Event(), no_progress())

    assert closed == [True]
    assert failure.type is OSError


# Coordinate active copies to check their order. Limit every wait.

WAIT_SECONDS = 20


class Recorder:
    """A log, in order, of the copies that started and ended."""

    def __init__(self):
        self.events: List[Tuple[str, str]] = []
        self._lock = threading.Lock()

    def add(self, event: str, name: str):
        with self._lock:
            self.events.append((event, name))

    def index(self, event: str, name: str) -> int:
        return self.events.index((event, name))

    def started(self) -> List[str]:
        return [name for event, name in self.events if event == "start"]


class ScriptedEntry(FakeEntry):
    """Run `before_read` before the first read of this file."""

    def __init__(
        self,
        name: str,
        content: bytes,
        recorder: Recorder,
        before_read: Callable[[], object] = lambda: None,
        label: Optional[str] = None,
    ):
        super().__init__(name, False, content)
        self._recorder = recorder
        self._before_read = before_read
        self._label = label or name

    def read(self, block_size: int) -> Generator[bytes, None, None]:
        self._recorder.add("start", self._label)
        self._before_read()
        yield from super().read(block_size)
        self._recorder.add("end", self._label)


def wait_for(event: threading.Event):
    assert event.wait(
        WAIT_SECONDS
    ), "The other copy did not start before the time limit"


def test_files_with_different_destinations_copy_at_the_same_time(
    monkeypatch: pytest.MonkeyPatch,
):

    recorder = Recorder()
    barrier = threading.Barrier(3)
    names = ["a.csv", "b.csv", "c.csv"]
    sources = [
        ScriptedEntry(name, name.encode(), recorder, lambda: barrier.wait(WAIT_SECONDS))
        for name in names
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, ["-j", "3", "folder/*", "dest"], resolved)

    assert flatten(dest) == {name: name.encode() for name in names}
    assert sorted(recorder.started()) == names


def test_recursive_copy_copies_the_files_of_a_tree_at_the_same_time(
    monkeypatch: pytest.MonkeyPatch,
):

    recorder = Recorder()
    barrier = threading.Barrier(3)

    def scripted(name: str) -> ScriptedEntry:
        return ScriptedEntry(
            name, name.encode(), recorder, lambda: barrier.wait(WAIT_SECONDS)
        )

    source = FakeEntry("folder", True)
    source.add(scripted("top.csv"))
    source.add(FakeEntry("one", True)).add(scripted("same.csv"))
    source.add(FakeEntry("two", True)).add(scripted("same.csv"))
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg([source], False), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, ["-r", "-j", "3", "folder", "dest"], resolved)

    assert flatten(dest) == {
        "top.csv": b"top.csv",
        "one/same.csv": b"same.csv",
        "two/same.csv": b"same.csv",
    }


@pytest.mark.parametrize("cpus,jobs", [(6, 6), (1, 1), (None, 1)])
def test_default_jobs_is_one_per_cpu(
    monkeypatch: pytest.MonkeyPatch, cpus: Optional[int], jobs: int
):

    monkeypatch.setattr(cp.os, "cpu_count", lambda: cpus)

    assert cp.default_jobs() == jobs


@pytest.mark.parametrize(
    "flags,cpus,limit",
    [
        # Use the requested limit.
        (["-j", "2"], 8, 2),
        (["-j", "5"], 2, 5),
        # Use one copy per processor if -j is absent.
        ([], 3, 3),
        ([], 6, 6),
    ],
)
def test_copies_active_copies_are_limited_by_j_or_else_by_the_cpu_count(
    monkeypatch: pytest.MonkeyPatch, flags: List[str], cpus: int, limit: int
):

    monkeypatch.setattr(cp.os, "cpu_count", lambda: cpus)
    recorder = Recorder()
    first = [f"{i}.csv" for i in range(limit)]
    all_active_copies = threading.Barrier(limit)
    active_copies = 0
    most = 0
    lock = threading.Lock()

    class Counted(ScriptedEntry):
        def read(self, block_size: int) -> Generator[bytes, None, None]:
            nonlocal active_copies, most
            with lock:
                active_copies += 1
                most = max(most, active_copies)
            if self.name() in first:
                all_active_copies.wait(WAIT_SECONDS)
            # Allow time for an extra copy to start if the limit fails.
            time.sleep(0.02)
            yield from super().read(block_size)
            with lock:
                active_copies -= 1

    sources = [Counted(f"{i}.csv", b"x", recorder) for i in range(4 * limit)]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, [*flags, "folder/*", "dest"], resolved)

    assert len(flatten(dest)) == 4 * limit
    assert most == limit


def test_j_1_copies_one_file_at_a_time_in_order_on_the_calling_thread(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = Recorder()
    threads: List[threading.Thread] = []
    names = ["a.csv", "b.csv", "c.csv"]
    sources = [
        ScriptedEntry(
            name, b"x", recorder, lambda: threads.append(threading.current_thread())
        )
        for name in names
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, ["-j", "1", "folder/*", "dest"], resolved)

    assert recorder.events == [
        (event, name) for name in names for event in ("start", "end")
    ]
    assert threads == [threading.current_thread()] * 3


def test_a_single_file_is_copied_on_the_calling_thread(
    monkeypatch: pytest.MonkeyPatch,
):

    recorder = Recorder()
    threads: List[threading.Thread] = []
    source = ScriptedEntry(
        "a.csv", b"x", recorder, lambda: threads.append(threading.current_thread())
    )
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg([source], False), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, ["a.csv", "dest"], resolved)

    assert threads == [threading.current_thread()]


def test_copies_to_the_same_destination_file_run_in_order(
    monkeypatch: pytest.MonkeyPatch,
):

    recorder = Recorder()
    b_started = threading.Event()
    first = ScriptedEntry(
        "a.csv", b"first", recorder, lambda: wait_for(b_started), "first a.csv"
    )
    second = ScriptedEntry("a.csv", b"second", recorder, label="second a.csv")
    other = ScriptedEntry("b.csv", b"b", recorder, b_started.set)
    dest = FakeEntry("dest", True)
    resolved = [
        ResolvedArg([first], True),
        ResolvedArg([second, other], True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-j", "2", "one/*", "two/*", "dest"], resolved)

    assert flatten(dest) == {"a.csv": b"second", "b.csv": b"b"}
    assert recorder.index("end", "first a.csv") < recorder.index(
        "start", "second a.csv"
    )


def test_group_by_target_keeps_the_order_of_conflicting_transfers():
    dest = FakeEntry("dest", True)
    other = FakeEntry("other", True)
    a1 = Transfer(FakeEntry("a.csv", False, b"1"), dest)
    b = Transfer(FakeEntry("b.csv", False), dest)
    a2 = Transfer(FakeEntry("a.csv", False, b"2"), dest)
    elsewhere = Transfer(FakeEntry("a.csv", False), other)
    a3 = Transfer(FakeEntry("a.csv", False, b"3"), dest)

    groups = cp.group_by_target([a1, b, a2, elsewhere, a3])

    assert groups == [[a1, a2, a3], [b], [elsewhere]]


def test_local_targets_treat_names_that_one_file_could_have_as_equal(
    tmp_path: Path,
):

    dest = cp.LocalEntry(str(tmp_path))

    assert dest.target("Report.CSV") == dest.target("report.csv")
    assert dest.target("caf\N{LATIN SMALL LETTER E WITH ACUTE}") == dest.target(
        "cafe\N{COMBINING ACUTE ACCENT}"
    )
    assert dest.target("a.csv") != dest.target("b.csv")


def test_local_targets_follow_a_symbolic_link_to_the_file_it_names(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")

    through_link = cp.LocalEntry(str(tmp_path / "link"))
    direct = cp.LocalEntry(str(tmp_path / "real"))

    assert through_link.target("a.csv") == direct.target("a.csv")


def test_drive_targets_are_the_directory_and_the_name(
    monkeypatch: pytest.MonkeyPatch,
):

    lookups: List[str] = []

    def fake_get_object(context: RequestContext, id: ObjectId) -> DataCatalogObject:
        lookups.append(str(id))
        return entry_object(PARENT_ID)

    monkeypatch.setattr(cp, "get_object", fake_get_object)
    context = cast(RequestContext, None)
    directory = cp.DriveEntry(context, root_slot(PARENT_ID, "Group drive"))
    file = cp.DriveEntry(context, file_slot(FILE_ID, "old.csv"))

    assert directory.target("a.csv") == (PARENT_ID, "a.csv")
    assert file.target("a.csv") == (PARENT_ID, "a.csv")
    assert file.target("A.CSV") == file.target("a.csv")
    assert lookups == [str(FILE_ID)]


def test_drive_put_uploads_into_the_directory_of_the_destination(
    monkeypatch: pytest.MonkeyPatch,
):
    puts: List[Tuple[uuid.UUID, bytes, int, str]] = []

    def fake_put_file(context, parent, blocks, size, filename):
        puts.append((parent, b"".join(blocks), size, filename))

    monkeypatch.setattr(cp, "put_file", fake_put_file)
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    context = cast(RequestContext, None)

    cp.DriveEntry(context, root_slot(OTHER_ID, "dir")).put([b"ab"], 2, "a.csv")
    cp.DriveEntry(context, file_slot(FILE_ID, "old.csv")).put([b"cd"], 2, "b.csv")

    assert puts == [(OTHER_ID, b"ab", 2, "a.csv"), (PARENT_ID, b"cd", 2, "b.csv")]


def test_directories_are_made_before_any_file_is_copied(
    monkeypatch: pytest.MonkeyPatch,
):

    events: List[Tuple[str, str, threading.Thread]] = []

    class Dest(FakeEntry):
        def mkdir(self, dir: str) -> "FakeEntry":
            events.append(("mkdir", dir, threading.current_thread()))
            existing = self.children.get(dir)
            return existing if existing is not None else self.add(Dest(dir, True))

        def put(self, blocks: Iterable[bytes], size: int, filename: str):
            events.append(("put", filename, threading.current_thread()))
            super().put(blocks, size, filename)

    dest = Dest("dest", True)
    resolved = [
        ResolvedArg(source_tree().collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["-r", "folder/*", "dest"], resolved)

    kinds = [kind for kind, name, thread in events]
    assert kinds == ["mkdir"] * 3 + ["put"] * 3
    assert {thread for kind, name, thread in events if kind == "mkdir"} == {
        threading.current_thread()
    }


@pytest.fixture
def log() -> Iterator[List[str]]:
    """The messages that the command logs."""

    messages: List[str] = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    yield messages
    logger.remove(sink)


def test_a_failure_does_not_stop_the_other_copies(
    monkeypatch: pytest.MonkeyPatch, log: List[str]
):

    recorder = Recorder()
    slow_started = threading.Event()
    failure_logged = threading.Event()
    sink = logger.add(
        lambda message: failure_logged.set() if "Failed to copy" in message else None
    )

    def fail():
        wait_for(slow_started)
        raise IOError("the drive said no")

    def finish_after_the_failure():
        slow_started.set()
        wait_for(failure_logged)

    sources = [
        ScriptedEntry("bad.csv", b"bad", recorder, fail),
        ScriptedEntry("slow.csv", b"slow", recorder, finish_after_the_failure),
        ScriptedEntry("late.csv", b"late", recorder),
        ScriptedEntry("later.csv", b"later", recorder),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    try:
        with pytest.raises(IOError, match="the drive said no"):
            run_cp(monkeypatch, ["-j", "2", "folder/*", "dest"], resolved)
    finally:
        logger.remove(sink)

    assert flatten(dest) == {
        "slow.csv": b"slow",
        "late.csv": b"late",
        "later.csv": b"later",
    }
    assert sorted(recorder.started()) == sorted(source.name() for source in sources)
    assert "Failed to copy bad.csv: the drive said no" in log
    assert "Copied 3 of 4 files. 1 failed." in log


@pytest.mark.parametrize("jobs", ["1", "2"])
def test_the_first_of_several_failures_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, log: List[str], jobs: str
):
    recorder = Recorder()
    first_failed = threading.Event()

    def fail_first():
        try:
            raise IOError("first failure")
        finally:
            first_failed.set()

    def fail_second():
        wait_for(first_failed)
        raise ValueError("second failure")

    sources = [
        ScriptedEntry("a.csv", b"a", recorder, fail_first),
        ScriptedEntry("b.csv", b"b", recorder, fail_second),
        ScriptedEntry("c.csv", b"c", recorder),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    with pytest.raises(IOError, match="first failure"):
        run_cp(monkeypatch, ["-j", jobs, "folder/*", "dest"], resolved)

    assert flatten(dest) == {"c.csv": b"c"}
    assert "Failed to copy b.csv: second failure" in log
    assert "Copied 1 of 3 files. 2 failed." in log


def test_an_error_that_is_not_a_file_failure_stops_new_copies(
    monkeypatch: pytest.MonkeyPatch,
):
    class Fatal(BaseException):
        pass

    recorder = Recorder()
    slow_started = threading.Event()
    workers: List[threading.Thread] = []

    def die():
        wait_for(slow_started)
        workers.append(threading.current_thread())
        raise Fatal()

    def finish_after_the_error():
        slow_started.set()
        wait_until(lambda: len(workers) == 1)
        workers[0].join(WAIT_SECONDS)

    sources = [
        ScriptedEntry("bad.csv", b"bad", recorder, die),
        ScriptedEntry("slow.csv", b"slow", recorder, finish_after_the_error),
        ScriptedEntry("late.csv", b"late", recorder),
        ScriptedEntry("later.csv", b"later", recorder),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    with pytest.raises(Fatal):
        run_cp(monkeypatch, ["-j", "2", "folder/*", "dest"], resolved)

    assert flatten(dest) == {"slow.csv": b"slow"}
    assert sorted(recorder.started()) == ["bad.csv", "slow.csv"]


def test_a_worker_that_dies_of_anything_fails_the_command(
    monkeypatch: pytest.MonkeyPatch,
):
    class Fatal(BaseException):
        pass

    def die():
        raise Fatal()

    recorder = Recorder()
    sources = [
        ScriptedEntry("bad.csv", b"bad", recorder, die),
        ScriptedEntry("good.csv", b"good", recorder),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    with pytest.raises(Fatal):
        run_cp(monkeypatch, ["-j", "2", "folder/*", "dest"], resolved)


def test_a_failure_with_j_1_does_not_stop_the_next_copies(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = Recorder()

    def fail():
        raise IOError("the drive said no")

    sources = [
        ScriptedEntry("a.csv", b"a", recorder),
        ScriptedEntry("bad.csv", b"bad", recorder, fail),
        ScriptedEntry("late.csv", b"late", recorder),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    with pytest.raises(IOError, match="the drive said no"):
        run_cp(monkeypatch, ["-j", "1", "folder/*", "dest"], resolved)

    assert flatten(dest) == {"a.csv": b"a", "late.csv": b"late"}
    assert recorder.started() == ["a.csv", "bad.csv", "late.csv"]


def test_ctrl_c_stops_the_copies_and_removes_their_partial_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: List[str]
):

    monkeypatch.setattr(cp, "INTERRUPT_GRACE_SECONDS", 0.5)
    both_writing = threading.Barrier(2)
    unstick = threading.Event()
    stopped: List[str] = []

    class Streaming(FakeEntry):
        def read(self, block_size: int) -> Generator[bytes, None, None]:
            try:
                yield b"first block"
                both_writing.wait(WAIT_SECONDS)
                _thread.interrupt_main()
                while True:
                    time.sleep(0.01)
                    yield b"more"
            finally:
                stopped.append(self.name())

    class Stuck(FakeEntry):
        def read(self, block_size: int) -> Generator[bytes, None, None]:
            yield b"first block"
            both_writing.wait(WAIT_SECONDS)
            unstick.wait(WAIT_SECONDS)
            yield b"never written"

    (tmp_path / "stuck.csv").write_bytes(b"old content")
    sources = [
        Streaming("streaming.csv", False, b"x" * 100),
        Stuck("stuck.csv", False, b"x" * 100),
    ]
    dest = cp.LocalEntry(str(tmp_path))
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]

    try:
        with pytest.raises(KeyboardInterrupt):
            run_cp(monkeypatch, ["-j", "2", "folder/*", str(tmp_path)], resolved)

        assert os.listdir(tmp_path) == ["stuck.csv"]
        assert (tmp_path / "stuck.csv").read_bytes() == b"old content"
        assert stopped == ["streaming.csv"]
        assert not [message for message in log if "Failed to copy" in message]
    finally:
        unstick.set()

    # The delayed copy must not write after the command stops.
    deadline = time.monotonic() + WAIT_SECONDS
    while threading.active_count() > 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert os.listdir(tmp_path) == ["stuck.csv"]
    assert (tmp_path / "stuck.csv").read_bytes() == b"old content"


def test_sigterm_ends_the_copy_as_an_exit_that_runs_the_cleanup(tmp_path: Path):
    path = tmp_path / "a.csv"
    before = signal.getsignal(signal.SIGTERM)

    def blocks() -> Iterator[bytes]:
        yield b"ab"
        signal.raise_signal(signal.SIGTERM)
        yield b"cd"

    with pytest.raises(SystemExit) as exit:
        with cp.exit_on_termination():
            cp.write_complete_file(str(path), blocks())

    assert exit.value.code == 128 + signal.SIGTERM
    assert os.listdir(tmp_path) == []
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_signal_that_is_ignored_stays_ignored():

    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with cp.exit_on_termination():
            signal.raise_signal(signal.SIGHUP)
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


def test_exit_on_termination_does_nothing_off_the_main_thread():
    errors: List[BaseException] = []

    def run():
        try:
            with cp.exit_on_termination():
                assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        except BaseException as error:
            errors.append(error)

    before = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(WAIT_SECONDS)
    finally:
        signal.signal(signal.SIGTERM, before)

    assert errors == []


@pytest.mark.parametrize("jobs", ["0", "-2"])
def test_j_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch, jobs: str):
    dest = FakeEntry("dest", True)
    resolved = [
        ResolvedArg([FakeEntry("a.csv", False)], False),
        ResolvedArg([dest], False),
    ]

    with pytest.raises(Exception, match="-j must be at least 1"):
        run_cp(monkeypatch, ["-j", jobs, "a.csv", "dest"], resolved)

    assert flatten(dest) == {}


def test_copy_of_nothing_succeeds(monkeypatch: pytest.MonkeyPatch):
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg([], True), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, ["folder/*", "dest"], resolved)
    assert flatten(dest) == {}


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../outside", "/outside", "a/b", "a\\b", "C:outside", "a\0b"],
)
@pytest.mark.parametrize("operation", ["put", "mkdir"])
def test_local_destination_rejects_unsafe_names(
    tmp_path: Path, name: str, operation: str
):
    destination = cp.LocalEntry(str(tmp_path))
    with pytest.raises(ValueError, match="Invalid file name"):
        if operation == "put":
            destination.put(unexpected_blocks(), 0, name)
        else:
            destination.mkdir(name)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("directory", [False, True])
def test_download_cannot_follow_a_child_link_outside_the_destination(
    tmp_path: Path, directory: bool
):
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside"
    if directory:
        outside.mkdir()
    else:
        outside.write_bytes(b"old")
    (destination / "link").symlink_to(outside, target_is_directory=directory)
    entry = cp.LocalEntry(str(destination))
    with pytest.raises(ValueError, match="outside the destination"):
        if directory:
            entry.mkdir("link")
        else:
            entry.put(unexpected_blocks(), 0, "link")
    assert outside.is_dir() if directory else outside.read_bytes() == b"old"


@pytest.mark.parametrize("content", [b"ab", b"abcd"])
def test_local_copy_rejects_wrong_size_and_keeps_previous_file(
    tmp_path: Path, content: bytes
):
    path = tmp_path / "file"
    path.write_bytes(b"old")
    with pytest.raises(OSError, match="Expected 3 bytes"):
        cp.LocalEntry(str(path)).put([content], 3, "file")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_download_rejects_wrong_size_with_a_complete_response(
    drive: FakeDrive, tmp_path: Path
):
    drive.respond = respond_with(b"short")
    slot = file_slot(FILE_ID, "file", size=10)
    path = tmp_path / "file"
    path.write_bytes(b"old")
    with pytest.raises(OSError, match="Expected 10 bytes"):
        cp.copy_file(
            Transfer(
                cp.DriveEntry(api_key_context(), slot), cp.LocalEntry(str(tmp_path))
            ),
            threading.Event(),
            no_progress(),
        )
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_download_does_not_follow_redirects(drive: FakeDrive):
    drive.respond = respond_with(
        b"", status=302, headers={"Location": drive.url("/other")}
    )
    with pytest.raises(HTTPError) as error:
        list(cp.read_drive_file(api_key_context(), FILE_ID, 4))
    assert error.value.response is not None
    assert error.value.response.status_code == 302
    assert [path for path, _ in drive.requests] == [
        f"/v1/api/ulv2/drive/file/{FILE_ID}"
    ]


def test_temporary_file_stays_private_until_the_copy_completes(tmp_path: Path):
    path = tmp_path / "file"
    path.write_bytes(b"old")
    path.chmod(0o644)

    def blocks():
        yield b"private content"
        partial = tmp_path / partial_files(tmp_path)[0]
        assert stat.S_IMODE(partial.stat().st_mode) == 0o600

    cp.write_complete_file(str(path), blocks())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_replacement_removes_special_execution_permissions(tmp_path: Path):
    path = tmp_path / "file"
    path.write_bytes(b"old")
    path.chmod(0o6755)
    cp.write_complete_file(str(path), [b"new"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_permission_failure_keeps_the_previous_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "file"
    path.write_bytes(b"old")

    def refuse(*args):
        raise PermissionError("Cannot set file permissions")

    monkeypatch.setattr(cp.os, "chmod", refuse)
    with pytest.raises(PermissionError):
        cp.write_complete_file(str(path), [b"new"])
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("when", ["before", "after"])
def test_cancellation_prevents_an_empty_file_from_replacing_the_destination(
    tmp_path: Path, when: str
):
    cancel = threading.Event()
    if when == "before":
        cancel.set()

    class Source(FakeEntry):
        def read(self, block_size: int) -> Generator[bytes, None, None]:
            assert when == "after"
            cancel.set()
            yield from ()

    path = tmp_path / "file"
    path.write_bytes(b"old")
    with pytest.raises(cp.Cancelled):
        cp.copy_file(
            Transfer(Source("file", False), cp.LocalEntry(str(tmp_path))),
            cancel,
            no_progress(),
        )
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("size", [0, -1])
def test_file_blocks_rejects_invalid_block_size(size: int):
    with pytest.raises(ValueError, match="at least 1"):
        list(cp.file_blocks([b"data"], size))


def test_file_blocks_does_not_copy_a_large_piece_into_a_large_buffer():
    content = bytes(8 * 1024 * 1024)
    tracemalloc.start()
    try:
        assert sum(map(len, cp.file_blocks([content], 1024))) == len(content)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024


@pytest.mark.parametrize("block_size", [0, -1])
def test_read_drive_file_rejects_invalid_block_sizes(
    monkeypatch: pytest.MonkeyPatch, block_size: int
):
    monkeypatch.setattr(cp, "get_file_stream", unexpected_call)
    with pytest.raises(ValueError, match="Block size must be at least 1"):
        list(cp.read_drive_file(cast(RequestContext, None), FILE_ID, block_size))


def test_read_drive_file_closes_the_reader_after_an_early_stop(
    monkeypatch: pytest.MonkeyPatch,
):
    reader = io.BytesIO(b"0123456789")
    monkeypatch.setattr(cp, "get_file_stream", lambda *args, **kwargs: reader)
    blocks = cp.read_drive_file(cast(RequestContext, None), FILE_ID, 4)
    try:
        assert next(blocks) == b"0123"
        assert not reader.closed
    finally:
        blocks.close()
    assert reader.closed


# Show the progress of downloads on a terminal.


class Terminal(io.StringIO):
    """Keep the text that a terminal shows on its screen."""

    def isatty(self) -> bool:
        return True

    def screen(self) -> List[str]:
        """Return the rows that the terminal shows. Remove empty rows at the end."""

        rows = [""]
        row = column = 0
        for part in re.split(r"(\x1b\[\d*[A-Z]|[\r\n])", self.getvalue()):
            if part == "\r":
                column = 0
            elif part == "\n":
                row, column = row + 1, 0
                if row == len(rows):
                    rows.append("")
            elif part.startswith("\x1b["):
                if part[-1] == "A":
                    row -= int(part[2:-1])
                    assert row >= 0, "The cursor moved above the first row"
                else:
                    assert part in ("\x1b[K", "\x1b[J"), f"Unknown command {part!r}"
                    rows[row] = rows[row][:column]
                    if part[-1] == "J":
                        del rows[row + 1 :]
            else:
                assert "\x1b" not in part, f"Unknown command in {part!r}"
                text = rows[row][:column].ljust(column) + part
                rows[row] = text + rows[row][len(text) :]
                column += len(part)
        while rows and not rows[-1]:
            rows.pop()
        return rows


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    """Return a terminal that can change its text in place.

    Stop the clock. The speed is then zero unless a test sets the time."""

    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(progress, "monotonic", lambda: 0.0)
    return Terminal()


def blocks_of(*blocks: bytes) -> Generator[bytes, None, None]:
    """Return the blocks of one file."""

    yield from blocks


def wait_until(condition: Callable[[], bool]):
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "No change before the time limit"
        time.sleep(0.001)


def set_terminal_size(monkeypatch: pytest.MonkeyPatch, columns: int, rows: int):
    monkeypatch.setattr(
        progress, "terminal_size", lambda stream: os.terminal_size((columns, rows))
    )


def test_progress_shows_each_active_download_and_the_total(terminal: Terminal):
    display = Progress(terminal, 3, 4096)
    first = display.track("first.csv", 2048, blocks_of(bytes(1024), bytes(1024)))
    second = display.track("second.csv", 500, blocks_of(bytes(100), bytes(400)))
    assert next(first) == bytes(1024)
    assert next(second) == bytes(100)

    display.refresh()

    assert terminal.screen() == [
        "Downloaded 0 of 3 files, 1.1 KiB of 4.0 KiB (27%), 0 B/s",
        "  50%     1.0 KiB of 2.0 KiB     first.csv",
        "  20%       100 B of 500 B       second.csv",
    ]


def test_progress_counts_a_download_that_ends_and_removes_it(terminal: Terminal):
    display = Progress(terminal, 2, 14)
    first = display.track("first.csv", 4, blocks_of(b"ab", b"cd"))
    second = display.track("second.csv", 10, blocks_of(b"abc", b"defghij"))
    assert next(first) == b"ab"
    assert next(second) == b"abc"
    display.refresh()
    assert len(terminal.screen()) == 3

    assert list(first) == [b"cd"]
    display.complete()
    display.refresh()

    assert terminal.screen() == [
        "Downloaded 1 of 2 files, 7 B of 14 B (50%), 0 B/s",
        "  30%         3 B of 10 B        second.csv",
    ]


def test_progress_does_not_count_a_download_that_stops(terminal: Terminal):
    closed: List[bool] = []

    def source() -> Generator[bytes, None, None]:
        try:
            yield b"ab"
            yield b"cd"
        finally:
            closed.append(True)

    display = Progress(terminal, 1, 4)
    # Keep the source object to make sure that the download closes it.
    blocks = source()
    download = display.track("a.csv", 4, blocks)
    assert next(download) == b"ab"

    download.close()
    display.refresh()

    assert closed == [True]
    assert terminal.screen() == ["Downloaded 0 of 1 files, 2 B of 4 B (50%), 0 B/s"]


def test_progress_does_not_count_a_download_that_fails(terminal: Terminal):
    def source() -> Generator[bytes, None, None]:
        yield b"ab"
        raise requests.ConnectionError("connection dropped")

    display = Progress(terminal, 1, 4)
    download = display.track("a.csv", 4, source())
    assert next(download) == b"ab"

    with pytest.raises(requests.ConnectionError):
        next(download)
    display.refresh()

    assert terminal.screen() == ["Downloaded 0 of 1 files, 2 B of 4 B (50%), 0 B/s"]


def test_progress_leaves_the_terminal_clean_after_an_interruption(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    # Update the display only on request.
    monkeypatch.setattr(progress, "REFRESH_SECONDS", WAIT_SECONDS)
    display = Progress(terminal, 1, 4)
    download = display.track("a.csv", 4, blocks_of(b"ab", b"cd"))

    with pytest.raises(KeyboardInterrupt):
        with display:
            assert next(download) == b"ab"
            display.refresh()
            assert len(terminal.screen()) == 2
            raise KeyboardInterrupt()

    assert terminal.screen() == []

    # A copy that could not stop in time can continue after the display ends.
    written = terminal.getvalue()
    assert next(download) == b"cd"
    with display.hidden():
        pass
    display.refresh()
    assert terminal.getvalue() == written


@pytest.mark.parametrize("case", ["file", "dumb terminal", "no downloads"])
def test_progress_writes_nothing_without_a_terminal_or_without_downloads(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal, case: str
):

    stream = io.StringIO() if case == "file" else terminal
    if case == "dumb terminal":
        monkeypatch.setenv("TERM", "dumb")
    files = 0 if case == "no downloads" else 1

    with Progress(stream, files, 4) as display:
        assert list(display.track("a.csv", 4, blocks_of(b"ab", b"cd"))) == [
            b"ab",
            b"cd",
        ]
        with display.hidden():
            pass
        display.refresh()

    assert stream.getvalue() == ""
    assert threading.active_count() == 1


def test_progress_keeps_other_output_above_the_display(terminal: Terminal):
    display = Progress(terminal, 1, 4)
    download = display.track("a.csv", 4, blocks_of(b"ab", b"cd"))
    assert next(download) == b"ab"
    display.refresh()

    with display.hidden():
        assert terminal.screen() == []
        terminal.write("Copying b.csv\n")

    expected = [
        "Copying b.csv",
        "Downloaded 0 of 1 files, 2 B of 4 B (50%), 0 B/s",
        "  50%         2 B of 4 B         a.csv",
    ]
    assert terminal.screen() == expected
    display.refresh()
    assert terminal.screen() == expected


def test_progress_shows_only_the_downloads_that_fit_in_the_rows(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    display = Progress(terminal, 6, 60)
    downloads = [
        display.track(f"{number}.csv", 10, blocks_of(b"x")) for number in range(6)
    ]
    for download in downloads:
        assert next(download) == b"x"
    total = "Downloaded 0 of 6 files, 6 B of 60 B (10%), 0 B/s"
    files = [f"  10%         1 B of 10 B        {number}.csv" for number in range(6)]

    # One row stays free for the cursor.
    set_terminal_size(monkeypatch, 80, 5)
    display.refresh()
    assert terminal.screen() == [total] + files[:2] + ["  and 4 more"]

    set_terminal_size(monkeypatch, 80, 8)
    display.refresh()
    assert terminal.screen() == [total] + files

    set_terminal_size(monkeypatch, 80, 7)
    display.refresh()
    assert terminal.screen() == [total] + files[:4] + ["  and 2 more"]

    set_terminal_size(monkeypatch, 80, 2)
    display.refresh()
    assert terminal.screen() == [total]

    set_terminal_size(monkeypatch, 80, 1)
    display.refresh()
    assert terminal.screen() == []


def test_progress_shows_an_empty_file_as_complete(terminal: Terminal):
    display = Progress(terminal, 1, 0)
    seen: List[List[str]] = []

    def source() -> Generator[bytes, None, None]:
        display.refresh()
        seen.append(terminal.screen())
        yield from ()

    assert list(display.track("empty.txt", 0, source())) == []

    assert seen == [
        [
            "Downloaded 0 of 1 files, 0 B of 0 B (100%), 0 B/s",
            " 100%         0 B of 0 B         empty.txt",
        ]
    ]


def test_progress_cuts_each_line_to_the_width_of_the_terminal(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    set_terminal_size(monkeypatch, 40, 24)
    display = Progress(terminal, 2, 8)
    long = display.track("a-long-name-" * 5, 4, blocks_of(b"ab"))
    wide = display.track("報告書" * 5, 4, blocks_of(b"ab"))
    assert next(long) == b"ab"
    assert next(wide) == b"ab"

    display.refresh()

    # The last column stays free, and a wide character uses two columns.
    assert terminal.screen() == [
        "Downloaded 0 of 2 files, 4 B of 8 B (50",
        "  50%         2 B of 4 B         a-long",
        "  50%         2 B of 4 B         報告書",
    ]


@pytest.mark.parametrize(
    "text,columns,expected",
    [
        ("", 0, ""),
        ("abc", 0, ""),
        ("abc", 2, "ab"),
        ("abc", 3, "abc"),
        ("abc", 9, "abc"),
        ("報告書", 3, "報"),
        ("報告書", 4, "報告"),
        ("a報告", 2, "a"),
        ("é", 1, ""),
    ],
)
def test_fit_cuts_text_to_a_number_of_columns(text: str, columns: int, expected: str):
    assert progress.fit(text, columns) == expected


def test_progress_replaces_characters_that_can_change_the_terminal(
    terminal: Terminal,
):
    display = Progress(terminal, 1, 4)
    download = display.track("a\x1b[2J\r\n\tb\x7f.csv", 4, blocks_of(b"ab"))
    assert next(download) == b"ab"

    display.refresh()

    assert terminal.screen() == [
        "Downloaded 0 of 1 files, 2 B of 4 B (50%), 0 B/s",
        "  50%         2 B of 4 B         a?[2J???b?.csv",
    ]


def test_progress_shows_the_speed_and_the_time_left(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    megabyte = bytes(1024 * 1024)
    times = iter([10.0, 12.0])
    monkeypatch.setattr(progress, "monotonic", lambda: next(times))
    display = Progress(terminal, 1, 5 * len(megabyte))
    download = display.track("a.bin", 5 * len(megabyte), blocks_of(megabyte, megabyte))

    display.refresh()
    assert next(download) == megabyte
    display.refresh()

    assert terminal.screen()[0] == (
        "Downloaded 0 of 1 files, 1.0 MiB of 5.0 MiB (20%), 512.0 KiB/s, 0:08 left"
    )


def test_progress_rounds_the_time_left_up(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    times = iter([0.0, 1.0])
    monkeypatch.setattr(progress, "monotonic", lambda: next(times))
    display = Progress(terminal, 1, 150)
    download = display.track("a.bin", 150, blocks_of(bytes(100), bytes(50)))

    display.refresh()
    assert next(download) == bytes(100)
    display.refresh()

    # Half a second of the download remains.
    assert terminal.screen()[0].endswith("(66%), 100 B/s, 0:01 left")


def test_progress_measures_the_speed_over_the_last_seconds_only(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    monkeypatch.setattr(progress, "SPEED_SECONDS", 2)
    times = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(progress, "monotonic", lambda: next(times))
    display = Progress(terminal, 1, 200)
    download = display.track("a.bin", 200, blocks_of(bytes(100), bytes(100)))

    display.refresh()
    assert next(download) == bytes(100)
    display.refresh()
    assert terminal.screen()[0].endswith("(50%), 100 B/s, 0:01 left")
    display.refresh()
    assert terminal.screen()[0].endswith("(50%), 50 B/s, 0:02 left")

    # The download made no progress in the last two seconds.
    display.refresh()
    assert terminal.screen()[0].endswith("(50%), 0 B/s")


def test_progress_shows_no_time_left_at_a_very_low_speed(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    times = iter([0.0, 5.0])
    monkeypatch.setattr(progress, "monotonic", lambda: next(times))
    size = 1024**4
    display = Progress(terminal, 1, size)
    download = display.track("a.bin", size, blocks_of(b"x"))

    display.refresh()
    assert next(download) == b"x"
    display.refresh()

    assert terminal.screen()[0] == (
        "Downloaded 0 of 1 files, 1 B of 1.0 TiB (0%), 0 B/s"
    )


def test_progress_updates_the_display_at_intervals(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    monkeypatch.setattr(progress, "REFRESH_SECONDS", 0.001)

    with Progress(terminal, 1, 4) as display:
        download = display.track("a.csv", 4, blocks_of(b"ab", b"cd"))
        assert next(download) == b"ab"
        wait_until(
            lambda: terminal.screen()
            == [
                "Downloaded 0 of 1 files, 2 B of 4 B (50%), 0 B/s",
                "  50%         2 B of 4 B         a.csv",
            ]
        )

        assert list(download) == [b"cd"]
        display.complete()
        wait_until(
            lambda: terminal.screen()
            == ["Downloaded 1 of 1 files, 4 B of 4 B (100%), 0 B/s"]
        )

    assert terminal.screen() == []
    assert threading.active_count() == 1


def test_a_terminal_that_fails_does_not_stop_a_download(terminal: Terminal):
    class Broken(Terminal):
        def write(self, text: str) -> int:
            raise OSError("the terminal closed")

    with Progress(Broken(), 1, 4) as display:
        download = display.track("a.csv", 4, blocks_of(b"ab", b"cd"))
        assert next(download) == b"ab"
        display.refresh()
        with display.hidden():
            pass
        assert list(download) == [b"cd"]


def test_a_terminal_that_is_slow_does_not_delay_a_download(terminal: Terminal):
    writing = threading.Event()
    release = threading.Event()

    class Slow(Terminal):
        def write(self, text: str) -> int:
            writing.set()
            # Wait longer than the test waits for the download.
            release.wait(2 * WAIT_SECONDS)
            return super().write(text)

    display = Progress(Slow(), 1, 4)
    received: List[bytes] = []
    refresh = threading.Thread(target=display.refresh)
    download = threading.Thread(
        target=lambda: received.extend(
            display.track("a.csv", 4, blocks_of(b"ab", b"cd"))
        )
    )

    try:
        refresh.start()
        wait_for(writing)
        download.start()
        download.join(WAIT_SECONDS)
        assert received == [b"ab", b"cd"]
    finally:
        release.set()
        refresh.join(WAIT_SECONDS)
        download.join(WAIT_SECONDS)


def test_terminal_size_has_a_default_for_an_unknown_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):

    # This stream has no file descriptor.
    assert progress.terminal_size(io.StringIO()) == (80, 24)

    # Some terminals report a size of zero.
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((0, 0)))
    with open(tmp_path / "terminal", "w") as stream:
        assert progress.terminal_size(stream) == (80, 24)

    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((132, 50)))
    with open(tmp_path / "terminal", "w") as stream:
        assert progress.terminal_size(stream) == (132, 50)


@pytest.mark.parametrize(
    "size,expected",
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2 - 1, "1.0 MiB"),
        (5 * 1024**3, "5.0 GiB"),
        (1024**4, "1.0 TiB"),
        (2048 * 1024**4, "2048.0 TiB"),
        (1023.99, "1.0 KiB"),
        (0.5, "0 B"),
    ],
)
def test_format_size_uses_units_of_1024_bytes(size: float, expected: str):
    assert progress.format_size(size) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0:00"),
        (59.9, "0:59"),
        (61, "1:01"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
    ],
)
def test_format_time_omits_zero_hours(seconds: float, expected: str):
    assert progress.format_time(seconds) == expected


def test_copy_file_counts_the_bytes_of_a_download(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    display = Progress(terminal, 1, 10)
    seen: List[List[str]] = []

    def fake_read_drive_file(
        context: RequestContext, id: uuid.UUID, block_size: int
    ) -> Iterator[bytes]:
        yield b"abc"
        display.refresh()
        seen.append(terminal.screen())
        yield b"defghij"

    monkeypatch.setattr(cp, "read_drive_file", fake_read_drive_file)
    slot = file_slot(FILE_ID, "a.csv", size=10)
    source = cp.DriveEntry(cast(RequestContext, None), slot)
    dest = FakeEntry("dest", True)

    cp.copy_file(Transfer(source, dest), threading.Event(), display)
    display.refresh()

    assert flatten(dest) == {"a.csv": b"abcdefghij"}
    assert seen == [
        [
            "Downloaded 0 of 1 files, 3 B of 10 B (30%), 0 B/s",
            "  30%         3 B of 10 B        a.csv",
        ]
    ]
    assert terminal.screen() == ["Downloaded 1 of 1 files, 10 B of 10 B (100%), 0 B/s"]


def test_copy_file_shows_no_progress_for_a_source_outside_the_drive(
    terminal: Terminal,
):
    display = Progress(terminal, 1, 10)

    class Source(FakeEntry):
        def read(self, block_size: int) -> Generator[bytes, None, None]:
            yield b"abc"
            display.refresh()
            yield b"d"

    source = Source("local.csv", False, b"abcd")
    dest = FakeEntry("dest", True)

    cp.copy_file(Transfer(source, dest), threading.Event(), display)

    assert flatten(dest) == {"local.csv": b"abcd"}
    assert terminal.screen() == ["Downloaded 0 of 1 files, 0 B of 10 B (0%), 0 B/s"]


def test_drive_cp_shows_the_progress_of_a_download_and_removes_it(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    # The total counts only the download.
    during_the_download = [
        "Copying a.csv",
        "Downloaded 0 of 1 files, 3 B of 4 B (75%), 0 B/s",
        "  75%         3 B of 4 B         a.csv",
    ]
    shown: List[bool] = []

    def fake_read_drive_file(
        context: RequestContext, id: uuid.UUID, block_size: int
    ) -> Iterator[bytes]:
        yield b"abc"
        # The command shows this block without a request.
        wait_until(lambda: terminal.screen() == during_the_download)
        shown.append(True)
        yield b"d"

    monkeypatch.setattr(sys, "stderr", terminal)
    monkeypatch.setattr(progress, "REFRESH_SECONDS", 0.001)
    monkeypatch.setattr(cp, "read_drive_file", fake_read_drive_file)
    context = cast(RequestContext, None)
    sources = [
        cp.DriveEntry(context, file_slot(FILE_ID, "a.csv", size=4)),
        FakeEntry("local.csv", False, b"local"),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]
    sink = logger.add(terminal, format="{message}")

    try:
        assert run_cp(monkeypatch, ["-j", "1", "folder/*", "dest"], resolved)
    finally:
        logger.remove(sink)

    assert flatten(dest) == {"a.csv": b"abcd", "local.csv": b"local"}
    assert shown == [True]
    assert terminal.screen() == ["Copying a.csv", "Copying local.csv"]
    assert threading.active_count() == 1


def test_drive_cp_writes_no_progress_without_a_download(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    monkeypatch.setattr(sys, "stderr", terminal)
    sources = [FakeEntry("a.csv", False, b"abc")]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, False), ResolvedArg([dest], False)]

    assert run_cp(monkeypatch, ["a.csv", "dest"], resolved)

    assert flatten(dest) == {"a.csv": b"abc"}
    assert terminal.getvalue() == ""


def test_local_entry_knows_if_it_is_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):

    controller, device = os.openpty()
    try:
        assert cp.LocalEntry(os.ttyname(device)).isatty()
    finally:
        os.close(controller)
        os.close(device)
    assert not cp.LocalEntry(os.devnull).isatty()

    (tmp_path / "file").write_bytes(b"content")
    os.mkfifo(tmp_path / "pipe")
    # An open can change the state of a pipe, so the entry opens only devices.
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", unexpected_call)
        for name in ("file", "pipe", "missing", "."):
            assert not cp.LocalEntry(str(tmp_path / name)).isatty()
    assert (tmp_path / "file").read_bytes() == b"content"


def test_drive_cp_shows_no_progress_for_a_copy_to_a_terminal(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):
    def fake_read_drive_file(
        context: RequestContext, id: uuid.UUID, block_size: int
    ) -> Iterator[bytes]:
        yield from (b"ab", b"cd")

    monkeypatch.setattr(sys, "stderr", terminal)
    monkeypatch.setattr(cp, "read_drive_file", fake_read_drive_file)
    context = cast(RequestContext, None)
    sources = [cp.DriveEntry(context, file_slot(FILE_ID, "a.csv", size=4))]
    controller, device = os.openpty()
    try:
        dest = cp.LocalEntry(os.ttyname(device))
        resolved = [ResolvedArg(sources, False), ResolvedArg([dest], False)]

        assert run_cp(monkeypatch, ["a.csv", "terminal"], resolved)

        # The terminal received the file.
        assert os.read(controller, 16) == b"abcd"
    finally:
        os.close(controller)
        os.close(device)
    assert terminal.getvalue() == ""


def test_log_messages_stay_complete_above_the_progress_display(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):

    slow_started = threading.Event()
    failure_logged = threading.Event()

    def write_log(message: str):
        terminal.write(message)
        if "Failed to copy" in message:
            failure_logged.set()

    def fake_read_drive_file(
        context: RequestContext, id: uuid.UUID, block_size: int
    ) -> Iterator[bytes]:
        if id == FILE_ID:
            wait_for(slow_started)
            raise IOError("the drive said no")
        slow_started.set()
        yield b"sl"
        wait_for(failure_logged)
        yield b"ow"

    monkeypatch.setattr(sys, "stderr", terminal)
    monkeypatch.setattr(progress, "REFRESH_SECONDS", 0.001)
    monkeypatch.setattr(cp, "read_drive_file", fake_read_drive_file)
    context = cast(RequestContext, None)
    sources = [
        cp.DriveEntry(context, file_slot(FILE_ID, "bad.csv", size=3)),
        cp.DriveEntry(context, file_slot(OTHER_ID, "slow.csv", size=4)),
    ]
    dest = FakeEntry("dest", True)
    resolved = [ResolvedArg(sources, True), ResolvedArg([dest], False)]
    sink = logger.add(write_log, format="{message}")

    try:
        with pytest.raises(IOError, match="the drive said no"):
            run_cp(monkeypatch, ["-j", "2", "folder/*", "dest"], resolved)
    finally:
        logger.remove(sink)

    assert flatten(dest) == {"slow.csv": b"slow"}
    rows = terminal.screen()
    assert sorted(rows[:2]) == ["Copying bad.csv", "Copying slow.csv"]
    assert rows[2:] == [
        "Failed to copy bad.csv: the drive said no",
        "Copied 1 of 2 files. 1 failed.",
    ]


def test_copy_messages_replace_terminal_commands():
    name = "report\x1b[2J\n.csv"
    source = FakeEntry(name, False, b"data")
    output = io.StringIO()
    sink = logger.add(output, format="{message}")
    try:
        cp.copy_file(
            Transfer(source, FakeEntry("dest", True)), threading.Event(), no_progress()
        )
    finally:
        logger.remove(sink)

    assert output.getvalue() == "Copying report?[2J?.csv\n"


def test_copy_failure_messages_replace_terminal_commands():
    class Failing(FakeEntry):
        def put(self, blocks: Iterable[bytes], size: int, filename: str):
            if filename.startswith("a"):
                raise OSError("denied\x1b]52;c;YQ==\x07\nend")
            super().put(blocks, size, filename)

    sources = [FakeEntry("a\x1b[2J.csv", False), FakeEntry("b.csv", False)]
    dest = Failing("dest", True)
    output = io.StringIO()
    sink = logger.add(output, format="{message}")
    try:
        with pytest.raises(OSError):
            cp.run_transfers(
                [Transfer(source, dest) for source in sources], 2, no_progress()
            )
    finally:
        logger.remove(sink)

    text = output.getvalue()
    assert "Failed to copy a?[2J.csv: denied?]52;c;YQ==??end\n" in text
    assert "\x1b" not in text
    assert "\x07" not in text


@pytest.mark.parametrize("failure", ["size", "write"])
def test_failed_downloads_do_not_count_as_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, terminal: Terminal, failure: str
):
    def read(context: RequestContext, id: uuid.UUID, block_size: int):
        yield b"data"

    def fail_replace(source: str, dest: str):
        raise OSError("disk full")

    monkeypatch.setattr(cp, "read_drive_file", read)
    size = 5 if failure == "size" else 4
    if failure == "write":
        monkeypatch.setattr(os, "replace", fail_replace)
    source = cp.DriveEntry(
        cast(RequestContext, None), file_slot(FILE_ID, "a.csv", size=size)
    )
    display = Progress(terminal, 1, size)

    with pytest.raises(OSError):
        cp.copy_file(
            Transfer(source, cp.LocalEntry(str(tmp_path))), threading.Event(), display
        )
    display.refresh()

    assert terminal.screen()[0].startswith("Downloaded 0 of 1 files,")
    assert list(tmp_path.iterdir()) == []


def test_progress_discards_speed_measurements_after_a_delay(
    monkeypatch: pytest.MonkeyPatch, terminal: Terminal
):
    times = iter([0.0, 1.0, 10.0])
    monkeypatch.setattr(progress, "monotonic", lambda: next(times))
    display = Progress(terminal, 1, 200)
    download = display.track("a.csv", 200, blocks_of(bytes(100), bytes(100)))

    display.refresh()
    assert next(download) == bytes(100)
    display.refresh()
    assert terminal.screen()[0].endswith("100 B/s, 0:01 left")
    display.refresh()

    assert terminal.screen()[0].endswith("0 B/s")


def test_terminal_check_does_not_wait_if_a_device_becomes_a_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    entry = cp.LocalEntry(str(path))
    get_status = os.stat

    def device_status(name, *args, **kwargs):
        if name == str(path):
            return SimpleNamespace(st_mode=stat.S_IFCHR)
        return get_status(name, *args, **kwargs)

    monkeypatch.setattr(os, "stat", device_status)
    results: List[bool] = []
    thread = threading.Thread(
        target=lambda: results.append(entry.isatty()), daemon=True
    )
    thread.start()
    try:
        thread.join(1)
        assert results == [False]
    finally:
        # Release a writer if the test fails because the open waits for a reader.
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            thread.join(WAIT_SECONDS)
        finally:
            os.close(reader)


def test_progress_accepts_a_closed_output_stream():
    stream = io.StringIO()
    stream.close()

    with Progress(stream, 1, 4) as display:
        assert list(display.track("a.csv", 4, blocks_of(b"data"))) == [b"data"]
        display.complete()
        display.refresh()
