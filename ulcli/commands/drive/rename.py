# Copyright (c), CommunityLogiq Software

import argparse
from typing import List
from loguru import logger
import uuid

from ulcli.commands.common import get_api_context
import ulcli.argparser
from ulsdk.api.drive import move
from ulsdk.types.id import ObjectId
from ulsdk.types.fs import MoveRequest


def drive_rename(args: List[str]):
    epilog = """Example: 

    ul drive rename -env prod -region ca -id 050019a6-b332-6a74-4974-91657391216c -name my_new_name
"""

    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive move",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-id",
        help="id of the drive file or directory to rename",
    )
    parser.add_argument("-name", help="new name for the drive file or directory")
    parser.add_argument(
        "-overwrite",
        action="store_true",
        help="overwrite the destination if it already exists. Defaults to false",
    )
    parsed = parser.parse_args(args)

    context = get_api_context(parsed)

    logger.info(
        f"Renaming {parsed.id} to {parsed.name} with overwrite={parsed.overwrite}"
    )
    id = ObjectId([b for b in uuid.UUID(parsed.id).bytes])
    move_request = MoveRequest(parsed.Name, None, id, parsed.overwrite)
    move(context, move_request)
    return True
