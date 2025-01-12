# Copyright (c), CommunityLogiq Software

from abc import ABC, abstractmethod


class UlcliCommand(ABC):
    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return self.name

    @abstractmethod
    def run(self) -> bool:
        """Run the command"""
