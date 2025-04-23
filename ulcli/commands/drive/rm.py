# Copyright (c), CommunityLogiq Software
import argparse
import urllib.parse
import ulcli.argparser

from typing import List
from loguru import logger
from ulsdk.api.drive import ls
from ulcli.commands.common import get_api_context
from ulsdk.request_context import RequestContext

from .utils import is_directory_entry_id, parse_timestamp_arg, timestamp_in_range
from .cp import get_dir_list_slot, DriveEntry


def do_rm_r(context: RequestContext, source: DriveEntry) -> bool:
    if source.isdir():
        src_entries = source.collect()
        if len(src_entries) == 0:
            logger.info(f"Nothing to delete: '{source.name()}' is already empty")
            return True
        logger.info(f"Deleting all content for directory '{source.name()}'")
        for src in src_entries:
            do_rm_r(context, src)
    else:
        logger.info(f"Deleting {source.name()}")
        source.rm()
    return True


def parse_pattern(context: RequestContext, pattern: str) -> List[DriveEntry]:
    resolved_files = []
    if "%" in pattern:
        pattern = urllib.parse.unquote(pattern)

    if "*" in pattern and not pattern.endswith("*"):
        raise ValueError("Currently only trailing wildcards are supported")

    splits = pattern.split("/")

    # 1. directory
    # Drive location is a directory id
    # e.g., 05006c77-e69f-893e-40d1-842b64c961a5
    if is_directory_entry_id(splits[0]):
        if len(splits) > 1:
            raise Exception(
                "to reference drive roots use the syntax <guid>:/<path>"
            )

        resolved_files.append(get_dir_list_slot(context, splits[0]))
        return resolved_files

    # 2. pattern
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
    epilog = """Example:
    ul drive rm -profile us '050040d2-6a9e-344c-4dfa-93c18ad2bfaa:/Dataset upload folder/*'
    """

    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive rm",
        description="Remove files/directories from the drive",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-r", help="recursively remove all subdirectories", action="store_true")
    parser.add_argument(
        "-start",
        help="Earliest last modified date of files to remove. Format: unix second or YYYY-MM-DD string (midnight local time)",
        type=str,
        default=None,
    )
    parser.add_argument(
        "-end",
        help="Latest last modified date of files to remove. Format: unix second or YYYY-MM-DD string (midnight local time)",
        type=str,
        default=None,
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="pattern of files/directories to remove. Please quote all wildcards",
    )
    # parse and validate args
    parsed = parser.parse_args(args)
    context = get_api_context(parsed)
    files = parsed.files
    logger.info(f"Gathering {files}")

    if len(files) != 1:
        raise ValueError("Only one file/directory pattern is allowed; you supplied more than one.")
    pattern = files[0]

    # list all files matching pattern
    entries = parse_pattern(context, pattern)

    # parse start & end args
    earliest = parse_timestamp_arg(parsed.start)
    latest = parse_timestamp_arg(parsed.end)
    if earliest is not None and latest is not None and earliest > latest:
        raise Exception("Earliest timestamp must be less than latest timestamp")

    # non‑empty directory & recursive
    # -> delete all content (files/sudirs)
    if parsed.r:
        for entry in entries:
            if not timestamp_in_range(entry.time(), earliest, latest):
                continue
            do_rm_r(context, entry)
        return True

    # non-empty directory & non-recursive
    # -> delete only files, keep subdirs
    for entry in entries:
        if entry.isdir():
            logger.warning(
                f"Skipping deletion of '{entry.name()}' because it is a directory. Use -r, if you intend to recursively delete directory contents."
            )
            continue
        if not timestamp_in_range(entry.time(), earliest, latest):
            continue
        logger.info(f"Deleting {entry.name()}")
        entry.rm()

    return True
