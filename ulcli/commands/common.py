# Copyright (c), CommunityLogiq Software

from ulsdk.types.id import (
    B2cId,
    ColumnGroupId,
    ContentId,
    DataStateId,
    GenericId,
    GraphNodeId,
    ObjectId,
    StreamId,
)
from typing import Union, Optional
from ulsdk.keys import Environment, load_key
from ulsdk.api_key_context import ApiKeyContext
import uuid
import os


def get_api_context(parsed):
    # prioritize passed env then env variable and then by default prod
    env_str = parsed.env or os.getenv("UL_ENV") or "prod"
    match env_str:
        case "prod":
            env = Environment.Prod
        case "stage":
            env = Environment.Stage
        case _:
            raise Exception(f"Invalid env: {env_str}")

    # prioritize passed profile, then region and then env variable
    profile_arg = parsed.profile if hasattr(parsed, "profile") else None
    profile = profile_arg or parsed.region or os.getenv("UL_PROFILE")
    if profile is None:
        raise Exception(
            "Profile is None, make sure to pass a profile or a region or have UL_PROFILE env variable set up"
        )

    key = load_key(profile)
    if key is None:
        raise Exception(
            "Unable to find API keys. Please run `ul keys install` to setup your API keys"
        )

    return ApiKeyContext(key, env)


def uuid_from_id(
    id: Optional[
        Union[
            B2cId,
            ColumnGroupId,
            ContentId,
            DataStateId,
            GenericId,
            GraphNodeId,
            ObjectId,
            StreamId,
        ]
    ]
) -> Optional[uuid.UUID]:
    if id is None:
        return None

    return uuid.UUID(bytes=bytes(id.b))


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False
    except TypeError:
        return False
    return False
