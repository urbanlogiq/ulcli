# Copyright (c), CommunityLogiq Software
import ulcli.argparser
from typing import List
from loguru import logger
from ulsdk.api.drive import unlink
from flatbuffer import ulcli
from ulcli.commands.common import get_api_context
from .utils import is_directory_entry_id


def drive_rm(args: List[str]):
    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive rm", description="Remove files/directories from the Drive"
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="pattern of files/directories to remove. Please quote all wildcards",
    )

    parsed = parser.parse_args(args)
    context = get_api_context(parsed)
    files = parsed.files

    assert is_directory_entry_id(files)
    unlink(context, files)
    logger.info(f"Unlinked {files}")
    return True
