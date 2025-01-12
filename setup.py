# Copyright (c), CommunityLogiq Software

import os
import setuptools

setuptools.setup(
    name="ulcli",
    version=os.getenv("PY_PACKAGE_VERSION") or "0.1.0",
    author="Max Burke",
    author_email="max@urbanlogiq.com",
    description="UrbanLogiq Command Line Interface",
    packages=setuptools.find_packages(),
    package_data={"ulcli": ["py.typed"]},
    python_requires=">=3.12",
    zip_safe=False,
    install_requires=[
        "flatbuffers",
        "loguru",
        "pyarrow",
        "python-magic",
        "tabulate",
        "termcolor",
        "ulsdk",
    ],
)
