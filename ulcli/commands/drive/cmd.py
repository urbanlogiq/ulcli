# Copyright (c), CommunityLogiq Software

import sys
from ulcli.internal import Console
from ulcli.commands.command import UlcliCommand
import ulcli.cmdparser

from .rename import drive_rename
from .move import drive_move
from .ls import drive_ls
from .cp import drive_cp
from .mkdir import drive_mkdir
from .rm import drive_rm
from .root import drive_root


class Drive(UlcliCommand):
    __help__ = "Drive commands"

    def __init__(self):
        super().__init__("drive")

    def run(self):
        parser = ulcli.cmdparser.CmdParser("drive")
        parser.add_cmd("ls", "list files", drive_ls)
        parser.add_cmd("cp", "copy files", drive_cp)
        parser.add_cmd("mkdir", "make directories", drive_mkdir)
        parser.add_cmd("rm", "unlink files/directories", drive_rm)
        parser.add_cmd("mv", "move files/directories", drive_move)
        parser.add_cmd("rename", "rename files/directories", drive_rename)
        parser.add_cmd("root", "get the id of the drive root id of a group", drive_root)

        try:
            return parser.dispatch(sys.argv[2:])
        except ulcli.cmdparser.UnsupportedArgException:
            Console.log(parser.get_help())
            return False
