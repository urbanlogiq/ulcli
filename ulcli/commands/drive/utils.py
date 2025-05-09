# Copyright (c), CommunityLogiq Software

"""
Shared functions for the drive commands
"""

import datetime
import re
from typing import Any
from uuid import UUID


def parse_timestamp_arg(arg: Any) -> int | None:
    if arg is None:
        return None
    if isinstance(arg, str) and re.match(r"\d{4}-\d{2}-\d{2}", arg):
        return int(datetime.datetime.strptime(arg, "%Y-%m-%d").timestamp())
    if isinstance(arg, int) or (isinstance(arg, str) and arg.isdigit()):
        return int(arg)
    raise ValueError(
        f"Invalid timestamp: {arg}. Must be a number (unix seconds) or a string in YYYY-MM-DD format"
    )


def timestamp_in_range(
    ts: float,
    earliest_timestamp: int | None,
    latest_timestamp: int | None,
) -> bool:
    if earliest_timestamp is not None and ts < earliest_timestamp:
        return False
    if latest_timestamp is not None and ts > latest_timestamp:
        return False
    return True


def is_directory_entry_id(id: str) -> bool:
    try:
        assert UUID(id)
    except Exception:
        return False
    return id.startswith("0500")
