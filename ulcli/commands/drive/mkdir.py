# Copyright (c), CommunityLogiq Software

from typing import List
from uuid import UUID
from loguru import logger

import ulcli.argparser
from ulsdk.api.drive import create_entry
from ulcli.commands.common import get_api_context


def drive_mkdir(args: List[str]):
    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive mkdir", description="Make a new directory in the Drive"
    )
    parser.add_argument(
        "-parent", help="id of the drive directory in which to create this directory"
    )
    parser.add_argument("name", help="name of the directory to create")
    parsed = parser.parse_args(args)
    context = get_api_context(parsed)

    parent = parsed.parent
    name = parsed.name

    assert UUID(parent)
    summary = create_entry(context, str(parent), name, "directory", "", 0)
    dir_id = summary.id

    logger.info(f'Created "{name}" folder with id: {dir_id} in parent: {parent}')
    return True
