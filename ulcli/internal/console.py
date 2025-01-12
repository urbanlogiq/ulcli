# Copyright (c), CommunityLogiq Software

from termcolor import colored


class Console:
    @staticmethod
    def error(s: str):
        print(colored(s, "red"))

    @staticmethod
    def strerror(s: str):
        return colored(s, "red")

    @staticmethod
    def warn(s: str):
        print(colored(s, "yellow"))

    @staticmethod
    def strwarn(s: str):
        return colored(s, "yellow")

    @staticmethod
    def log(s: str):
        print(s)

    @staticmethod
    def ok(s: str):
        print(colored(s, "green"))

    @staticmethod
    def strok(s: str):
        return colored(s, "green")
