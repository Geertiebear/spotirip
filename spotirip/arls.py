import requests
import re
import os
import logging

from streamrip.client import DeezerClient
from streamrip.config import ConfigData, DeezerConfig
from streamrip.exceptions import AuthenticationError

import asyncio

ARL_LIST_URL = "https://rentry.org/firehawk52"
ARL_ENV = "SPOTIRIP_ARL"

logger = logging.getLogger(__name__)

class ConfigMock:
    def __init__(self, data: ConfigData):
        self.session = data

def _build_config(arl: str) -> ConfigMock:
    deezer_config = DeezerConfig(arl=arl, quality=2, use_deezloader=True,
                                 deezloader_warnings=True)
    config_data = ConfigData(deezer=deezer_config, toml=None, downloads=None,
                             qobuz=None, tidal=None, soundcloud=None, youtube=None,
                             lastfm=None, filepaths=None, artwork=None,
                             metadata=None, qobuz_filters=None, cli=None,
                             database=None, conversion=None, misc=None)

    return ConfigMock(config_data)

def retrieve_arls() -> list[str]:
    resp = requests.get(ARL_LIST_URL)
    if not resp.ok:
        raise Exception(f"Failed to retrieve ARLS: {resp.status_code}")

    body = resp.content.decode('utf-8')
    return re.findall("[a-z0-9]{192}", body)

async def find_working_arl(arls: list[str]) -> str:
    env_arl = os.getenv(ARL_ENV)
    if env_arl is not None:
        arls.append(env_arl)
        arls = list(reversed(arls))

    for arl in arls:
        logger.info(f"Trying ARL: {arl}")
        config = _build_config(arl)
        client = DeezerClient(config)

        try:
            await client.login()
        except AuthenticationError as e:
            continue
        finally:
            await client.session.close()

        return arl

    logger.warn(f"No working ARL found :(")
    return None


if __name__ == "__main__":
    arls = retrieve_arls()

    print(asyncio.run(find_working_arl(arls)))
