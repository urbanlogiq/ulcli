# Copyright (c), CommunityLogiq Software

import argparse
import uuid
from typing import List
from loguru import logger

import ulcli.argparser
from ulsdk.request_context import RequestContext
from ulsdk.api.drive import ls, move
from ulsdk.types.fs import MoveRequest
from ulsdk.types.id import ObjectId
from ulcli.commands.common import get_api_context, is_uuid, uuid_from_id
from .utils import parse_timestamp_arg, timestamp_in_range


def do_move(
    context: RequestContext,
    source: str,
    target: str,
    overwrite: bool,
    earliest_timestamp: int | None = None,
    latest_timestamp: int | None = None,
) -> None:
    target_id = ObjectId.from_uuid(target)

    # Source is a single file or single directory, provided as an id
    if is_uuid(source):
        if earliest_timestamp is not None or latest_timestamp is not None:
            raise ValueError(
                "Cannot provide earliest or latest timestamp when moving a single item by id!"
            )

        logger.info(f"Moving {source} to {target} with overwrite={overwrite}")
        source_id = ObjectId.from_uuid(source)

        move_request = MoveRequest(
            None,
            target_id,
            source_id,
            overwrite,
        )
        move(context, move_request)
        return

    splits = source.split("/")
    logger.info(f"source: {source}, splits: {splits}")
    if len(splits) < 2:
        raise ValueError(
            f"Invalid source: {source}. Must be a single id or a glob pattern rooted at a drive directory id."
        )

    root_id = splits[0]
    if not uuid.UUID(root_id):
        raise ValueError(f"Invalid root id: {root_id}")

    result = ls(context, root_id, splits[1])
    items = result.slots

    num_moved = 0
    for item in items:
        obj_id = item.id
        if obj_id is None:
            continue

        if not timestamp_in_range(
            item.time,
            earliest_timestamp,
            latest_timestamp,
        ):
            continue

        logger.info(f"Moving {item.name} ({id}) to {target} with overwrite={overwrite}")

        move_request = MoveRequest(
            None,
            target_id,
            obj_id,
            overwrite,
        )
        move(context, move_request)
        num_moved += 1

    logger.info(f"Moved {num_moved} items to {target}")


def drive_move(args: List[str]):
    epilog = """
Example usage:

SINGLE FILE
Move one file/directory, with id 050019a6-b332-6a74-4974-91657391216c, into a directory with id 05001709-8654-fa31-4d82-9e60f522c788:
ul drive mv -env prod -region ca 050019a6-b332-6a74-4974-91657391216c 05001709-8654-fa31-4d82-9e60f522c788

ENTIRE DIRECTORY CONTENT
Move the entire contents of 0500e590-2309-85ae-4404-bf0a63e56483 into 050077f4-588b-45ba-46ee-a108a0fa2fcc
ul drive mv -env prod -region ca "0500e590-2309-85ae-4404-bf0a63e56483/*" 050077f4-588b-45ba-46ee-a108a0fa2fcc

DIRECTORY CONTENT BY FILE NAME PATTERN
Move every file in 0500e590-2309-85ae-4404-bf0a63e56483 whose file name starts with the string "test", into 050077f4-588b-45ba-46ee-a108a0fa2fcc:
ul drive mv -env prod -region ca "0500e590-2309-85ae-4404-bf0a63e56483/test*" 050077f4-588b-45ba-46ee-a108a0fa2fcc

DIRECTORY CONTENT BY LAST MODIFIED DATE RANGE
Move every file in 0500e590-2309-85ae-4404-bf0a63e56483 whose last modified date is between 2024-01-01 and 2024-02-01, into 050077f4-588b-45ba-46ee-a108a0fa2fcc:
ul drive mv -env prod -region ca "0500e590-2309-85ae-4404-bf0a63e56483/*" 050077f4-588b-45ba-46ee-a108a0fa2fcc -start 2024-01-01 -end 2024-02-01
"""

    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive mv",
        description="Move files within the drive",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source",
        help="drive file(s) or directory(s) to move. Can be a single id, or a glob pattern rooted at a drive directory id",
    )
    parser.add_argument("target", help="id of the destination directory")
    parser.add_argument(
        "-overwrite",
        action="store_true",
        help="overwrite upon naming conflict. Defaults to false",
    )
    parser.add_argument(
        "-start",
        help="Earliest last modified date of files to move. Format: unix millisecond or YYYY-MM-DD string (midnight local time)",
    )
    parser.add_argument(
        "-end",
        help="Latest last modified date of files to move. Format: unix millisecond or YYYY-MM-DD string (midnight local time)",
    )

    parsed = parser.parse_args(args)

    context = get_api_context(parsed)

    target = parsed.target
    assert uuid.UUID(target)

    earliest = parse_timestamp_arg(parsed.start)
    latest = parse_timestamp_arg(parsed.end)

    if earliest is not None and latest is not None and earliest > latest:
        raise Exception("Earliest timestamp must be less than latest timestamp")

    do_move(context, parsed.source, target, parsed.overwrite, earliest, latest)

    return True
