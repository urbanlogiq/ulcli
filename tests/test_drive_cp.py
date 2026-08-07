# Copyright (c), CommunityLogiq Software

"""Tests for the recursive path of `ul drive cp`.

The tests replace `parse_files` with prepared results and copy between in-memory
trees, so they never touch the network or the filesystem.
"""

from typing import Dict, List, Optional, cast

import pytest

from ulcli.commands.drive import cp
from ulcli.commands.drive.cp import ResolvedArg
from ulsdk.request_context import RequestContext


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
