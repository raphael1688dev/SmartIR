import asyncio
import binascii
import logging
import os.path
import struct

import aiofiles
import aiohttp
import voluptuous as vol
from awesomeversion import AwesomeVersion

from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.const import __version__ as current_ha_version
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'smartir'
VERSION = '1.18.1'
MANIFEST_URL = "https://raw.githubusercontent.com/smartHomeHub/SmartIR/{}/custom_components/smartir/manifest.json"
REMOTE_BASE_URL = "https://raw.githubusercontent.com/smartHomeHub/SmartIR/{}/custom_components/smartir/"
COMPONENT_ABS_DIR = os.path.dirname(os.path.abspath(__file__))

CONF_CHECK_UPDATES = 'check_updates'
CONF_UPDATE_BRANCH = 'update_branch'

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_CHECK_UPDATES, default=True): cv.boolean,
        vol.Optional(CONF_UPDATE_BRANCH, default='master'): vol.In(['master', 'rc'])
    })
}, extra=vol.ALLOW_EXTRA)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SmartIR component."""
    conf = config.get(DOMAIN)

    if conf is None:
        return True

    check_updates = conf[CONF_CHECK_UPDATES]
    update_branch = conf[CONF_UPDATE_BRANCH]

    async def _check_updates(service: ServiceCall) -> None:
        await _update(hass, update_branch)

    async def _update_component(service: ServiceCall) -> None:
        await _update(hass, update_branch, do_update=True)

    hass.services.async_register(DOMAIN, 'check_updates', _check_updates)
    hass.services.async_register(DOMAIN, 'update_component', _update_component)

    if check_updates:
        # 使用背景任務執行更新檢查，避免阻塞 Home Assistant 啟動流程
        hass.async_create_task(_update(hass, update_branch, do_update=False, notify_if_latest=False))

    return True

async def _update(hass: HomeAssistant, branch: str, do_update: bool = False, notify_if_latest: bool = True) -> None:
    try:
        session = async_get_clientsession(hass)
        url = MANIFEST_URL.format(branch)
        
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                _LOGGER.error("Failed to fetch manifest. Status code: %s", response.status)
                return
            
            data = await response.json(content_type='text/plain')
            min_ha_version = data['homeassistant']
            last_version = data['updater']['version']
            release_notes = data['updater']['releaseNotes']

            if AwesomeVersion(last_version) <= AwesomeVersion(VERSION):
                if notify_if_latest:
                    hass.components.persistent_notification.async_create(
                        "You're already using the latest version!", 
                        title='SmartIR'
                    )
                return

            if AwesomeVersion(current_ha_version) < AwesomeVersion(min_ha_version):
                hass.components.persistent_notification.async_create(
                    "There is a new version of SmartIR integration, but it is **incompatible** "
                    "with your system. Please first update Home Assistant.", 
                    title='SmartIR'
                )
                return

            if not do_update:
                hass.components.persistent_notification.async_create(
                    f"A new version of SmartIR integration is available ({last_version}).\n"
                    f"Call the ``smartir.update_component`` service to update "
                    f"the integration.\n\n**Release notes:**\n{release_notes}", 
                    title='SmartIR'
                )
                return

            # 開始更新檔案
            files = data['updater']['files']
            has_errors = False

            for file in files:
                try:
                    source = REMOTE_BASE_URL.format(branch) + file
                    dest = os.path.join(COMPONENT_ABS_DIR, file)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    # 將 session 傳遞給 downloader 進行共用
                    await Helper.downloader(session, source, dest)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                    has_errors = True
                    _LOGGER.error("Error updating %s: %s. Please update the file manually.", file, err)
                except Exception:
                    has_errors = True
                    _LOGGER.exception("Unexpected error updating %s", file)

            if has_errors:
                hass.components.persistent_notification.async_create(
                    "There was an error updating one or more files of SmartIR. "
                    "Please check the logs for more information.", 
                    title='SmartIR'
                )
            else:
                hass.components.persistent_notification.async_create(
                    f"Successfully updated to {last_version}. Please restart Home Assistant.", 
                    title='SmartIR'
                )
                
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.error("Network error while checking for updates: %s", err)
    except Exception:
        _LOGGER.exception("An unexpected error occurred while checking for updates")


class Helper:
    @staticmethod
    async def downloader(session: aiohttp.ClientSession, source: str, dest: str) -> None:
        """Download a file using the shared aiohttp session."""
        async with session.get(source, timeout=10) as response:
            if response.status == 200:
                async with aiofiles.open(dest, mode='wb') as f:
                    await f.write(await response.read())
            else:
                raise aiohttp.ClientResponseError(
                    request_info=response.request_info,
                    history=response.history,
                    status=response.status,
                    message=f"File not found or inaccessible: {source}"
                )

    @staticmethod
    def pronto2lirc(pronto: str) -> list[int]:
        codes = [int(binascii.hexlify(pronto[i:i+2].encode('utf-8') if isinstance(pronto, str) else pronto[i:i+2]), 16) for i in range(0, len(pronto), 2)]

        if codes[0]:
            raise ValueError("Pronto code should start with 0000")
        if len(codes) != 4 + 2 * (codes[2] + codes[3]):
            raise ValueError("Number of pulse widths does not match the preamble")

        frequency = 1 / (codes[1] * 0.241246)
        return [int(round(code / frequency)) for code in codes[4:]]

    @staticmethod
    def lirc2broadlink(pulses: list[int]) -> bytearray:
        array = bytearray()

        for pulse in pulses:
            pulse = int(pulse * 269 / 8192)

            if pulse < 256:
                array += bytearray(struct.pack('>B', pulse))
            else:
                array += bytearray([0x00])
                array += bytearray(struct.pack('>H', pulse))

        packet = bytearray([0x26, 0x00])
        packet += bytearray(struct.pack('<H', len(array)))
        packet += array
        packet += bytearray([0x0d, 0x05])

        # Add 0s to make ultimate packet size a multiple of 16 for 128-bit AES encryption.
        remainder = (len(packet) + 4) % 16
        if remainder:
            packet += bytearray(16 - remainder)
        return packet
