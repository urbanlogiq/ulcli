# Copyright (c), CommunityLogiq Software

import sys
import argparse
import base64
import json
from getpass import getpass
from typing import Dict, Optional, Tuple, Union, Any, List
import requests
from configparser import ConfigParser
import os
from pathlib import Path
import uuid

import ulcli.cmdparser
from ulcli.commands.common import get_api_context
from ulcli.commands import UlcliCommand
from ulcli.internal.console import Console
from ulsdk.keys import Region
from ulsdk.api_key_context import ApiKeyContext
from ulsdk.request_context import RequestContext, File


def save_keys(config: ConfigParser):
    ul = os.path.join(Path.home(), ".ul")
    if not os.path.exists(ul):
        os.makedirs(ul)

    keys_file = os.path.join(ul, "keys")
    with open(keys_file, "w") as f:
        config.write(f)


# This function must take an argument due to how it is invoked by `parser.dispatch`
def list_keys(args: List[str]) -> bool:
    from ulsdk.keys import _load_keys
    from tabulate import tabulate

    config = _load_keys()
    table = []
    for profile in config.sections():
        user_id = config[profile]["user_id"]
        region = config[profile]["region"]
        access_key = config[profile]["access_key"]

        table.append([profile, user_id, region, access_key])

    print(tabulate(table, headers=["Profile", "User ID", "Region", "Access Key ID"]))
    return True


class BearerTokenContext(RequestContext):
    def __init__(
        self,
        env: Optional[str],
        region: Region,
        token: str,
        user_id: str,
    ):
        self._region = region
        self._env = env
        self._user_id = uuid.UUID(user_id)
        self._token = token
        self._api = None

    def user_id(self):
        return self._user_id

    def region(self) -> Region:
        return self._region

    def _build_api_string(self, path: str) -> Tuple[str, str]:
        api_path = path

        # Assuming that if the user specifies a path starting with "/v1/api" (ie:
        # the ulgate prefix) they want a specific API that isn't ulv2), but if
        # no such prefix exists they want ulv2.
        if path.find("/v1/api") == -1:
            api_path = "/v1/api/ulv2" + path

        if self._api is None:
            server = "api" if self._env == "prod" else "stage"
            api = "https://" + server + ".urbanlogiq." + self._region.str()
            return (api + api_path, api_path)

        return (self._api + api_path, api_path)

    def _get_headers(
        self,
        bearer_token: Optional[str] = None,
        data: Optional[Union[str, bytes]] = None,
        mimetype: Optional[str] = None,
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {}

        if bearer_token is not None:
            headers["authorization"] = "Bearer " + bearer_token

        if mimetype is not None:
            headers["content-type"] = mimetype

        if data is not None:
            headers["content-length"] = str(len(data))

        return headers

    def get(
        self,
        path: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> bytes:
        headers = self._get_headers(bearer_token=self._token)
        url, path = self._build_api_string(path)
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.content

    def post(
        self,
        path: str,
        body: Union[bytes, str, None] = None,
        mimetype: str = "application/octet-stream",
        params: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> bytes:
        headers = self._get_headers(
            bearer_token=self._token, data=body, mimetype=mimetype
        )
        url, path = self._build_api_string(path)
        response = requests.post(url, headers=headers, data=body)
        response.raise_for_status()
        return response.content

    def put(
        self,
        path: str,
        body: Union[bytes, str, None] = None,
        mimetype: str = "application/octet-stream",
        params: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> bytes:
        mimetype = (
            "application/json" if type(body) is str else "application/octet-stream"
        )
        headers = self._get_headers(
            bearer_token=self._token, data=body, mimetype=mimetype
        )
        url, path = self._build_api_string(path)
        response = requests.put(url, headers=headers, data=body)
        response.raise_for_status()
        return response.content

    def upload(
        self,
        path: str,
        files: List[File],
    ) -> bytes:
        raise Exception("not implemented")

    def delete(
        self,
        path: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> bytes:
        raise Exception("not implemented")


def test_profile(profile: str) -> bool:
    from ulsdk.keys import load_key, Environment

    key = load_key(profile)
    if key is None:
        return False

    context = ApiKeyContext(key, Environment.Prod)

    try:
        res = context.get("/v1/bootstrap/")
        return True
    except Exception as e:
        return False


client_ids = {
    "ca": "6ec724c2-0929-4873-a987-be92949fc905",
    "us": "c829131d-27e8-473f-8c77-7986f02a6913",
}

b2c_regions = {
    "ca": "canada",
    "us": "us",
}


def get_bearer_token(
    username: str, password: str, region: Region
) -> Union[Tuple[str, str], None]:
    client_id = client_ids[region.str()]
    b2c_region = b2c_regions[region.str()]

    params = {
        "p": "B2C_1_ropc",
        "grant_type": "password",
        "response_type": "token",
        "client_id": client_id,
        "username": username,
        "password": password,
        "scope": "openid " + client_id,
    }

    endpoint = (
        "https://urbanlogiq"
        + b2c_region
        + ".b2clogin.com/urbanlogiq"
        + b2c_region
        + ".onmicrosoft.com/oauth2/v2.0/token?"
    )

    response = requests.post(endpoint, params=params)
    if response.status_code != 200:
        return None

    token = response.json()["access_token"]
    split_token = token.split(".")
    claims = json.loads(base64.b64decode(split_token[1] + "=="))

    return (claims["oid"], token)


def add_key(args: List[str]) -> bool:
    from ulsdk.keys import _load_keys

    parser = argparse.ArgumentParser(prog="ul keys add")
    parser.add_argument(
        "-profile", help="Profile ID to identify this set of credentials"
    )
    parser.add_argument("-userid", help="The UrbanLogiq system user GUID")
    parser.add_argument("-region", help="Region key was created in ('us', 'ca')")
    parser.add_argument("-accesskey", help="Access key ID ('AKIA...')")
    parser.add_argument("-secretkey", help="Secret key")

    parsed = parser.parse_args(args)
    if parsed.profile is None:
        Console.error("Profile was not specified")
        return False

    try:
        uuid.UUID(parsed.userid)
    except:
        Console.error(
            "Valid user ID not specified, must be in GUID form (ie: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX)"
        )
        return False

    if parsed.region is None or parsed.region not in ["ca", "us"]:
        Console.error("Valid region not specified; must be 'ca' or 'us'")
        return False

    if parsed.accesskey is None or parsed.accesskey.find("AKIA") != 0:
        Console.error("Valid access key not specified; must start with AKIA")
        return False

    if parsed.secretkey is None:
        Console.error("Secret key not specified")
        return False

    config = _load_keys()
    config[parsed.profile] = {}
    config[parsed.profile]["user_id"] = parsed.userid
    config[parsed.profile]["region"] = parsed.region
    config[parsed.profile]["access_key"] = parsed.accesskey
    config[parsed.profile]["secret_key"] = parsed.secretkey
    save_keys(config)

    return True


def remove_key(args: List[str]) -> bool:
    from ulsdk.keys import _load_keys

    parser = argparse.ArgumentParser(prog="ul keys remove")
    parser.add_argument("-profile", help="Profile ID to remove")

    parsed = parser.parse_args(args)
    if parsed.profile is None:
        Console.error("Profile was not specified")
        return False

    config = _load_keys()
    if config.remove_section(parsed.profile):
        save_keys(config)
        return True
    else:
        Console.warn("Profile does not exist in config")
        return False


def create_new_key(
    context: RequestContext, profile: str, id: str, region: Region
) -> Optional[str]:
    from ulsdk.keys import _load_keys

    try:
        res = context.post("/v1/api/uldirectory/v1/keys/")
    except requests.HTTPError as e:
        print(
            "Request to /keys failed with status code: {}. {}".format(
                e.response.status_code, e.response.content
            )
        )
        return None

    body = json.loads(res.decode("utf-8"))
    access_key = body["id"]
    secret_key = body["secretKey"]

    config = _load_keys()
    config[profile] = {}
    config[profile]["user_id"] = id
    config[profile]["region"] = region.str()
    config[profile]["access_key"] = access_key
    config[profile]["secret_key"] = secret_key
    save_keys(config)

    return access_key


def create_key(args: List[str]) -> bool:
    from ulsdk.keys import _load_keys

    parser = argparse.ArgumentParser(prog="ul keys create")
    parser.add_argument(
        "-newprofile", help="Name of the new profile to be associated with the new key"
    )
    parser.add_argument("-region", help="Region, one of ('us', 'ca')")
    parser.add_argument(
        "-env",
        nargs="?",
        help="Environment, either 'prod' or 'stage'.",
    )
    parser.add_argument(
        "-overwrite",
        nargs="?",
        help='If "true", allow an existing profile to be overwritten',
    )

    parsed = parser.parse_args(args)
    if parsed.newprofile is None:
        Console.error("newprofile was not specified")
        return False

    config = _load_keys()
    if parsed.newprofile in config.keys():
        if parsed.overwrite == "true":
            Console.warn('Overwriting existing profile "{}"'.format(parsed.newprofile))
        else:
            Console.error(
                'Profile "{}" already exists. Use argument "-overwrite true" to overwrite.'.format(
                    parsed.newprofile
                )
            )
            return False

    context = get_api_context(parsed)
    access_key = create_new_key(
        context, parsed.newprofile, str(context._key.user_id), Region.parse(parsed.region)
    )

    if access_key is not None:
        Console.log(
            "Generated new key with id {} and saved to new profile {}".format(
                access_key, parsed.newprofile
            )
        )

    return access_key is not None


def do_install(region: Region) -> bool:
    Console.log(f'Running user install for region "{region}"')
    username = input(f"Enter your urbanlogiq username (aka your email) for {region}: ")
    password = getpass(f"Enter your urbanlogiq password for {region}: ")

    bearer_token = get_bearer_token(username, password, region)
    if bearer_token is None:
        Console.error("Invalid username or password")
        return False
    user_id, token = bearer_token
    env = "prod"
    context = BearerTokenContext(env, region, token, user_id)
    succeeded = create_new_key(context, region.str(), user_id, region)

    if succeeded:
        print(f"Validating {region}... ", end="")
        test_result = test_profile(region.str())
        if test_result:
            Console.log("success!")
        else:
            Console.error("no joy")

        return test_result

    return False


def install_keys(args: List[str]) -> bool:
    from ulsdk.keys import _load_keys

    need_regions = {"ca", "us"}

    config = _load_keys()
    for profile in config.sections():
        region = config[profile]["region"]
        need_regions.remove(region)

    regions = sorted(list(need_regions))
    for region in regions:
        parsed_region = Region.parse(region)
        while not do_install(parsed_region):
            pass

    Console.log("Good to go!")
    return True


def test_keys(args: List[str]) -> bool:
    from ulsdk.keys import _load_keys

    config = _load_keys()
    success = True
    for profile in config.sections():
        print(f"validating {profile}... ", end="")
        sys.stdout.flush()

        if test_profile(profile):
            print("success!")
        else:
            print("no joy")
            success = False

    return success


class Keys(UlcliCommand):
    __help__ = "manage API keys"

    def __init__(self):
        super().__init__("keys")

    def run(self):
        parser = ulcli.cmdparser.CmdParser("keys")
        parser.add_cmd("list", "list keys", list_keys)
        parser.add_cmd("add", "add key", add_key)
        parser.add_cmd("remove", "remove key", remove_key)
        parser.add_cmd("create", "create key", create_key)
        parser.add_cmd("install", "do initial installation of keys", install_keys)
        parser.add_cmd("test", "test key setup", test_keys)

        try:
            return parser.dispatch(sys.argv[2:])
        except ulcli.cmdparser.UnsupportedArgException:
            Console.log(parser.get_help())
            return False

        return True
