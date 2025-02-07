# Copyright (c), CommunityLogiq Software

from typing import List
from datetime import datetime
from tabulate import tabulate

from ulcli.commands.common import get_api_context, uuid_from_id
import ulcli.argparser
from ulsdk.api.drive import ls, get_roots
from ulsdk.types.fs import (
    ListSlot,
    ListFile,
    ListDirectory,
    ListObject,
    TopLevelDirectory,
)
from ulsdk.types.generated.PermissionTy import PermissionTy


def parse_slot(slot: ListSlot) -> List[str]:
    ty = "<unknown>"
    if isinstance(slot.entry.value, ListFile):
        ty = "File"
    elif isinstance(slot.entry.value, ListDirectory):
        ty = "Directory"
    elif isinstance(slot.entry.value, ListObject):
        ty = "Object"
    elif isinstance(slot.entry.value, TopLevelDirectory):
        ty = "Directory"

    time = datetime.utcfromtimestamp(slot.time / 1000).strftime("%Y-%m-%d %H:%M:%S")
    slot_perm = slot.user_permissions
    perms = PermissionTy()
    permissions = ""
    if slot_perm & perms.PERM_BROWSE:
        permissions += "B"
    if slot_perm & perms.PERM_READ:
        permissions += "R"
    if slot_perm & perms.PERM_APPEND:
        permissions += "A"
    if slot_perm & perms.PERM_MODIFY:
        permissions += "M"

    size = str(slot.size)
    id = uuid_from_id(slot.id)
    if id is None:
        id_str = "<unknown>"
    else:
        id_str = str(id)

    return [ty, permissions, time, size, id_str, slot.name]


# Example usage:
# To list the content of a directory with id=0500b351-a8ff-9be6-4294-952890075152:
# ul drive ls -env prod -region us "0500b351-a8ff-9be6-4294-952890075152/*"
def drive_ls(args: List[str]):
    parser = ulcli.argparser.ArgumentParser(
        prog="ul drive ls", description="List files in the specified Drive directory"
    )
    parser.add_argument(
        "-union",
        action="store_true",
        help="show files in the unioned drive search space",
    )
    parser.add_argument("-me", action="store_true", help="show files in your drive")
    parser.add_argument(
        "paths", nargs="*", help="ls pattern. please quote all wildcards"
    )
    parsed = parser.parse_args(args)
    if parsed.union and parsed.me:
        raise Exception("cannot specify both -union and -me; pick one!")

    context = get_api_context(parsed)

    results = []
    if len(parsed.paths) == 0:
        roots = get_roots(context)
        results += roots.slots

    for path in parsed.paths:
        print(f"Gathering {path}", end="\r")

        if parsed.union:
            res = ls(context, "union", path)
            results += res.slots
        elif parsed.me:
            res = ls(context, "me", path)
            results += res.slots
        else:
            slash_idx = path.find("/")
            root = path
            if slash_idx != -1:
                root = path[:slash_idx]
                path = path[slash_idx + 1 :]
            else:
                path = ""
            res = ls(context, root, path)
            results += res.slots

        # clear the line of all the "gathering" text
        print(" " * (len(path) + 10), end="\r")

    table = map(parse_slot, results)
    print(
        tabulate(table, headers=["Type", "Permissions", "Time", "Size", "Id", "Name"])
    )
    return True
