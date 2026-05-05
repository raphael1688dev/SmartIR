from abc import ABC, abstractmethod
from base64 import b64encode
import binascii
import json
import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import Helper

_LOGGER = logging.getLogger(__name__)

BROADLINK_CONTROLLER = 'Broadlink'
XIAOMI_CONTROLLER = 'Xiaomi'
MQTT_CONTROLLER = 'MQTT'
LOOKIN_CONTROLLER = 'LOOKin'
ESPHOME_CONTROLLER = 'ESPHome'

ENC_BASE64 = 'Base64'
ENC_HEX = 'Hex'
ENC_PRONTO = 'Pronto'
ENC_RAW = 'Raw'


def get_controller(hass: HomeAssistant, controller: str, encoding: str, controller_data: str, delay: float):
    """Return a controller compatible with the specification provided."""
    controllers = {
        BROADLINK_CONTROLLER: BroadlinkController,
        XIAOMI_CONTROLLER: XiaomiController,
        MQTT_CONTROLLER: MQTTController,
        LOOKIN_CONTROLLER: LookinController,
        ESPHOME_CONTROLLER: ESPHomeController
    }
    
    ctrl_class = controllers.get(controller)
    if not ctrl_class:
        raise ValueError(f"The controller '{controller}' is not supported.")
        
    return ctrl_class(hass, controller, encoding, controller_data, delay)


class AbstractController(ABC):
    """Representation of a controller."""
    
    # 子類別需定義支援的編碼清單
    SUPPORTED_ENCODINGS: set[str] = set()

    def __init__(self, hass: HomeAssistant, controller: str, encoding: str, controller_data: str, delay: float):
        self.hass = hass
        self._controller = controller
        self._encoding = encoding
        self._controller_data = controller_data
        self._delay = delay
        self.check_encoding(encoding)

    def check_encoding(self, encoding: str) -> None:
        """Check if the encoding is supported by the controller."""
        if encoding not in self.SUPPORTED_ENCODINGS:
            raise ValueError(
                f"The encoding '{encoding}' is not supported by the {self._controller} controller."
            )

    @abstractmethod
    async def send(self, command: str | list[str]) -> None:
        """Send a command."""
        pass


class BroadlinkController(AbstractController):
    """Controls a Broadlink device."""
    SUPPORTED_ENCODINGS = {ENC_BASE64, ENC_HEX, ENC_PRONTO}

    def _convert_pronto_to_base64(self, command: str) -> str:
        """
        純 CPU 運算邏輯。
        將繁重的轉換過程獨立出來，以便丟入背景 Thread 執行。
        """
        try:
            _command_clean = command.replace(' ', '')
            _bytes = bytearray.fromhex(_command_clean)
            _lirc = Helper.pronto2lirc(_bytes)
            _broadlink = Helper.lirc2broadlink(_lirc)
            return b64encode(_broadlink).decode('utf-8')
        except ValueError as err:
            raise ValueError("Error while converting Pronto to Base64 encoding") from err

    async def send(self, command: str | list[str]) -> None:
        """Send a command."""
        commands = []
        if not isinstance(command, list): 
            command = [command]

        for _command in command:
            if self._encoding == ENC_HEX:
                try:
                    _bytes = binascii.unhexlify(_command)
                    _command = b64encode(_bytes).decode('utf-8')
                except binascii.Error as err:
                    raise ValueError("Error while converting Hex to Base64 encoding") from err

            elif self._encoding == ENC_PRONTO:
                # 將 CPU 密集的轉換工作交給背景執行緒 (Executor) 處理，避免阻塞 HA 主事件迴圈
                _command = await self.hass.async_add_executor_job(
                    self._convert_pronto_to_base64, _command
                )

            commands.append(f"b64:{_command}")

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            'command': commands,
            'delay_secs': self._delay
        }

        await self.hass.services.async_call('remote', 'send_command', service_data)


class XiaomiController(AbstractController):
    """Controls a Xiaomi device."""
    SUPPORTED_ENCODINGS = {ENC_PRONTO, ENC_RAW}

    async def send(self, command: str) -> None:
        """Send a command."""
        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            'command': f"{self._encoding.lower()}:{command}"
        }
        await self.hass.services.async_call('remote', 'send_command', service_data)


class MQTTController(AbstractController):
    """Controls an MQTT device."""
    SUPPORTED_ENCODINGS = {ENC_RAW}

    async def send(self, command: str) -> None:
        """Send a command."""
        _command = command.replace("\\", "")
        service_data = {
            'topic': self._controller_data,
            'payload': _command
        }
        await self.hass.services.async_call('mqtt', 'publish', service_data)


class LookinController(AbstractController):
    """Controls a Lookin device."""
    SUPPORTED_ENCODINGS = {ENC_PRONTO, ENC_RAW}

    async def send(self, command: str) -> None:
        """Send a command."""
        encoding = self._encoding.lower().replace('pronto', 'prontohex')
        url = f"http://{self._controller_data}/commands/ir/{encoding}/{command}"
        
        # 改用 HA 原生的 aiohttp session 處理非同步網路請求
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
        except aiohttp.ClientError as err:
            _LOGGER.error("Failed to send LOOKin command to %s: %s", url, err)


class ESPHomeController(AbstractController):
    """Controls an ESPHome device."""
    SUPPORTED_ENCODINGS = {ENC_RAW}
    
    async def send(self, command: str) -> None:
        """Send a command."""
        try:
            service_data = {'command': json.loads(command)}
        except json.JSONDecodeError as err:
            raise ValueError(f"Invalid JSON command for ESPHome: {command}") from err

        await self.hass.services.async_call('esphome', self._controller_data, service_data)
