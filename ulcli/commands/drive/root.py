# Copyright (c), CommunityLogiq Software

import argparse
import sys
from typing import List
from loguru import logger

import ulcli.argparser
from ulcli.commands.common import get_api_context
from ulsdk.api.drive import get_root_id


def drive_root(args: List[str]):
    description = "Fetches the id of the drive root of a user or group."

    epilog = """Example:
    ul root -env prod -region us <guid>
"""

    parser = ulcli.argparser.ArgumentParser(
        "ul drive root",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "id",
        help="user or group UUID",
    )
    parsed = parser.parse_args(sys.argv[2:])
    context = get_api_context(parsed)

    root = get_root_id(context, parsed.id)

    if root is None:
        logger.error("Unable to get root")
        return False

    print(root)
    return True
