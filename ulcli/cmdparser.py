# Copyright (c), CommunityLogiq Software

from typing import Callable, List, Optional


class UnsupportedArgException(Exception):
    pass


class ArgTuple:
    def __init__(
        self,
        arg: str,
        helpstr: str,
        fn: Callable[[List[str]], bool],
        aliases: List[str],
    ):
        self.arg = arg
        self.helpstr = helpstr
        self.fn = fn
        self.aliases = aliases


class CmdParser:
    def __init__(self, module: str, module_help: Optional[str] = None):
        self.supported_cmds = list()
        self.module = module
        self.module_help = module_help

    def add_cmd(
        self,
        arg: str,
        helpstr: str,
        fn: Callable[[List[str]], bool],
        aliases: List[str] = list(),
    ):
        arg_tuple = ArgTuple(arg, helpstr, fn, aliases)
        self.supported_cmds.append(arg_tuple)

    def get_help(self):
        max_len = 0
        help_str = f"ul {self.module} commands:\n\n"
        for arg_tuple in self.supported_cmds:
            max_alias_len = 0
            if len(arg_tuple.aliases) > 0:
                max_alias_len = max(map(lambda alias: len(alias), arg_tuple.aliases))
            max_len = max(max_len, len(arg_tuple.arg), max_alias_len)

        for arg_tuple in self.supported_cmds:
            help_str = (
                help_str
                + "    "
                + arg_tuple.arg.ljust(max_len + 1)
                + arg_tuple.helpstr
                + "\n"
            )
            if len(arg_tuple.aliases) > 0:
                for alias in arg_tuple.aliases:
                    help_str = (
                        help_str
                        + "    "
                        + alias.ljust(max_len + 1)
                        + f"alias for {arg_tuple.arg}"
                        + "\n"
                    )

        if self.module_help is not None:
            help_str = help_str + "\n" + self.module_help + "\n"

        return help_str

    def print_help(self):
        print(self.get_help())

    def dispatch(self, args: List[str]):
        self.command = None
        self.rest = args[1:]

        if len(args) > 0:
            self.command = args[0]

        for arg_tuple in self.supported_cmds:
            if self.command == arg_tuple.arg or self.command in arg_tuple.aliases:
                return arg_tuple.fn(self.rest)

        raise UnsupportedArgException()
