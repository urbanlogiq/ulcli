# Copyright (c), CommunityLogiq Software

import os
import sys
import inspect
import importlib.util
from loguru import logger
from pathlib import Path
from types import ModuleType
from typing import NamedTuple, List, Dict, Any, Union

import ulcli.commands
from ulcli.internal import Console
import ulcli.cmdparser


modules = []


class Command(NamedTuple):
    module: ModuleType
    ty: str
    help: str


def register_module(module):
    global modules
    modules.append(module)


def _get_command_list() -> List[Command]:
    global modules

    module_commands = []
    for mod in modules:
        raw = inspect.getmembers(mod, inspect.isclass)
        for element in raw:
            element_name = element[0]
            if element_name != "UlcliCommand":
                help = ""
                try:
                    target = getattr(mod, element_name)
                    help = target.__help__
                except:
                    pass

                module_commands.append(Command(mod, element_name, help))

    return module_commands


def _instantiate_module(command: Command):
    target = getattr(command.module, command.ty)
    instance = target()
    return instance


def _get_command_name(command: Command) -> str:
    target = getattr(command.module, command.ty)
    instance = target()
    return str(instance)


def _get_command_to_module_mapping() -> Dict[str, Command]:
    mapping = dict()
    commands = _get_command_list()
    for command in commands:
        name = _get_command_name(command)
        mapping[name] = command
    return mapping


def _print_help():
    print("usage:")
    print("    ul --help")
    print("    ul [command] --help")
    print("    ul [command] [parameters]")
    print("")
    print("Supported commands:")
    mapping = _get_command_to_module_mapping()
    keys = sorted(mapping.keys())

    for key in keys:
        value = mapping[key]
        cmd_help = "    {key: <20} {help}".format(key=key, help=value.help)
        print(cmd_help)


def main():
    register_module(ulcli.commands)

    if len(sys.argv) < 2:
        _print_help()
        return -1

    command = sys.argv[1]
    if command == "--help" or command == "-h" or command == "-help":
        _print_help()
        return 0

    mapping = _get_command_to_module_mapping()
    if command not in mapping:
        print('ERROR: Command "%s" not recognized. Try "ulcli --help".' % (command))
        return -1

    instance = _instantiate_module(mapping[command])
    if not instance.run():
        return -1

    return 0
