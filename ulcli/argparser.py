# Copyright (c), CommunityLogiq Software

"""
The ArgumentParser class subclasses the Python argparse.ArgumentParser class
in order to add support for common environment related arguments, such as
-env, -profile, and -region.
"""

import argparse

envs = ["stage", "prod", "local"]
regions = ["ca", "us"]


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_argument(
            "-region",
            help=f"Specify the region in which to deploy pipeline: {','.join(regions)}",
            choices=regions,
        )
        self.add_argument(
            "-env",
            help=f"the environment {','.join(envs)} in which to deploy the pipeline",
            choices=envs,
        )
        self.add_argument(
            "-profile",
            required=False,
            help="the profile you wish to use if not using the regular us/ca",
        )
