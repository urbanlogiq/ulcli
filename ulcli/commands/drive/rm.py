# Copyright (c), CommunityLogiq Software
import urllib.parse
import ulcli.argparser

from typing import List
from loguru import logger
from ulsdk.api.drive import ls
from ulcli.commands.common import get_api_context
from ulsdk.request_context import RequestContext

from .utils import is_directory_entry_id
from .cp import get_dir_list_slot, DriveEntry


def do_rm_r(context: RequestContext, source: DriveEntry) -> bool:
    src_entries = source.collect()
    for src in src_entries:
        if src.isdir(): 
            do_rm_r(context, src)
        else:
            logger.info(f"Deleting {src.name()}")
            src.rm()
    return True


def parse_pattern(context: RequestContext, pattern: str) -> List[DriveEntry]:
    resolved_files = []
    if "%" in pattern:
        pattern = urllib.parse.unquote(pattern)

    if "*" in pattern and not pattern.endswith("*"):
        raise ValueError("Currently only trailing wildcards are supported")

    splits = pattern.split("/")

    # Drive location is a directory id
    # e.g., 05006c77-e69f-893e-40d1-842b64c961a5
    if is_directory_entry_id(splits[0]):
        if len(splits) > 1:
            raise Exception(
                "to reference drive roots use the syntax <guid>:/<path>"
            )

        resolved_files.append(get_dir_list_slot(context, splits[0]))
        return resolved_files

    # Dirve location is a directory id followed by a relative path
    # e.g., 05006c77-e69f-893e-40d1-842b64c961a5:/Dataset upload folder/Transportation/Turning movement counts/*
    if splits[0].endswith(":") and len(splits[0]) == 37:
        # remote (drive) path
        root = splits[0][:-1]
        path = "/".join(splits[1:])

        entries = ls(context, root, path)
        slots = entries.slots
        resolved_files += [DriveEntry(context, entry) for entry in slots]

    return resolved_files


def drive_rm(args: List[str]) -> bool:
    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive rm", description="Remove files/directories from the Drive"
    )
    parser.add_argument("-r", help="recursively delete all subdirectories")
    parser.add_argument(
        "files",
        nargs="+",
        help="pattern of files/directories to remove. Please quote all wildcards",
    )

    parsed = parser.parse_args(args)
    context = get_api_context(parsed)
    files = parsed.files
    entries = parse_pattern(context, files)

    # non‑empty directory & recursive
    # -> delete all content (files/sudirs)
    if parsed.r:
        for entry in entries:
            do_rm_r(context, entry)
        return True

    # non-empty directory & non-recursive
    # -> delete only files, keep subdirs
    for entry in entries:
        if entry.isdir():
            logger.warning(
                f"Skipping deletion of {entry.name()} because it is a directory. Use -r, if you intend to recursively delete directory contents."
            )
            continue
        logger.info(f"Deleting {entry.name()}")
        entry.rm()

    return True
