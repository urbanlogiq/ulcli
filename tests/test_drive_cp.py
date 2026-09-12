# Copyright (c), CommunityLogiq Software

"""Tests for `ul drive cp`.

The copy tests replace `parse_files` with prepared results and copy between
in-memory trees. The bare-id tests replace the SDK calls that `cp` makes. So no
test touches the network, and only the download test writes to the filesystem
(under `tmp_path`).
"""

import uuid
from pathlib import Path
from typing import Dict, List, NoReturn, Optional, cast

import pytest

from ulcli.commands.common import uuid_from_id
from ulcli.commands.drive import cp
from ulcli.commands.drive.cp import ResolvedArg
from ulsdk.request_context import RequestContext
from ulsdk.types.fs import (
    DirectoryEntry,
    DirectoryList,
    Entry,
    ListEntry,
    ListFile,
    ListSlot,
    TopLevelDirectory,
)
from ulsdk.types.id import ObjectId
from ulsdk.types.object import DataCatalogObject


class FakeEntry(cp.Entry):
    """An in-memory file or directory."""

    def __init__(self, name: str, isdir: bool, content: bytes = b"", time: float = 0.0):
        self._name = name
        self._isdir = isdir
        self._content = content
        self._time = time
        self.children: Dict[str, "FakeEntry"] = {}

    def add(self, child: "FakeEntry") -> "FakeEntry":
        self.children[child.name()] = child
        return child

    def get(self) -> bytes:
        return self._content

    def name(self) -> str:
        return self._name

    def time(self) -> float:
        return self._time

    def isdir(self) -> bool:
        return self._isdir

    def put(self, content: bytes, filename: str):
        assert self._isdir
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
    """Return every file under `entry`, keyed by its path."""

    files = {}
    for child in entry.collect():
        path = f"{prefix}{child.name()}"
        if child.isdir():
            files.update(flatten(child, f"{path}/"))
        else:
            files[path] = child.get()
    return files


def source_tree() -> FakeEntry:
    """A directory that holds one file and two nested subdirectories."""

    root = FakeEntry("folder", True)
    root.add(FakeEntry("top.csv", False, b"top"))
    fire = root.add(FakeEntry("Fire incidents", True))
    fire.add(FakeEntry("2026.csv", False, b"fire"))
    geo = root.add(FakeEntry("Geospatial assets", True))
    nested = geo.add(FakeEntry("nested", True))
    nested.add(FakeEntry("deep.geojson", False, b"deep"))
    return root


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
    """`cp -r 'folder/*' dest` is the case that used to fail outright."""

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
    """A path without a wildcard keeps its established behaviour."""

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
    """A wildcard match count of one must not be read as a plain path."""

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
        # The drive glob turns a trailing slash into a trailing wildcard.
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


def test_non_recursive_copy_skips_directories(monkeypatch: pytest.MonkeyPatch):
    source = source_tree()
    dest = FakeEntry("dest", True)

    resolved = [
        ResolvedArg(source.collect(), True),
        ResolvedArg([dest], False),
    ]

    assert run_cp(monkeypatch, ["folder/*", "dest"], resolved)
    assert flatten(dest) == {"top.csv": b"top"}


# Bare entry ids
#
# `ul drive cp <id> <dest>` names one file or directory by its id alone. The
# parent is read from the drive, so these tests build the entry object and the
# parent listing that the SDK calls return.

PARENT_ID = uuid.UUID("05005b63-7183-a068-417b-b73392b83856")
FILE_ID = uuid.UUID("0500addd-0f04-fe98-4ff2-bdef69cb097d")
OTHER_ID = uuid.UUID("0500e62f-9ac7-6323-85d6-0ca330b10480")


def entry_object(parent: uuid.UUID) -> DataCatalogObject:
    """The data catalog object of a drive entry whose parent is `parent`."""

    obj = DataCatalogObject.make_default()
    entry = DirectoryEntry(Entry.make_default(), ObjectId.from_uuid(parent))
    obj.obj = entry.to_bytes()
    return obj


def file_slot(id: uuid.UUID, name: str, time_ms: int = 0) -> ListSlot:
    """A listing slot for a file, as `ls` returns it."""

    entry = ListEntry(ListFile("text/csv", 3, None))
    return ListSlot(None, entry, ObjectId.from_uuid(id), None, name, 3, time_ms, 0)


def root_slot(id: uuid.UUID, name: str) -> ListSlot:
    """A listing slot for a drive root, as `get_roots` returns it."""

    entry = ListEntry(TopLevelDirectory.make_default())
    return ListSlot(None, entry, ObjectId.from_uuid(id), None, name, 0, 0, 0)


def unexpected_call(*args: object) -> NoReturn:
    raise AssertionError(f"unexpected SDK call with {args}")


def test_bare_file_id_resolves_through_the_parent_listing(
    monkeypatch: pytest.MonkeyPatch,
):
    """A file id resolves to its own slot, and only the parent is listed."""

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
    """A root has the nil id as its parent, so it is found among the roots."""

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
    """`ul drive cp <file-id> ./dir` downloads the file with no parent named."""

    def fake_get_file(context: RequestContext, id: ObjectId) -> bytes:
        assert uuid_from_id(id) == FILE_ID
        return b"abc"

    monkeypatch.setattr(cp, "get_api_context", lambda parsed: None)
    monkeypatch.setattr(cp, "get_object", lambda context, id: entry_object(PARENT_ID))
    monkeypatch.setattr(
        cp,
        "ls",
        lambda context, root, tail: DirectoryList([file_slot(FILE_ID, "a.csv")]),
    )
    monkeypatch.setattr(cp, "get_file", fake_get_file)

    assert cp.drive_cp([str(FILE_ID), str(tmp_path)])

    assert (tmp_path / "a.csv").read_bytes() == b"abc"
