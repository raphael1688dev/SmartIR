import asyncio
import json
import logging
import os

import aiofiles
import voluptuous as vol

from homeassistant.components.fan import (
    FanEntity, FanEntityFeature,
    PLATFORM_SCHEMA, DIRECTION_REVERSE, DIRECTION_FORWARD)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item
)

from . import COMPONENT_ABS_DIR, Helper
from .controller import get_controller

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Fan"
DEFAULT_DELAY = 0.5

CONF_UNIQUE_ID = 'unique_id'
CONF_DEVICE_CODE = 'device_code'
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = 'power_sensor'

SPEED_OFF = "off"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_DEVICE_CODE): cv.positive_int,
    vol.Required(CONF_CONTROLLER_DATA): cv.string,
    vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.string,
    vol.Optional(CONF_POWER_SENSOR): cv.entity_id
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the IR Fan platform."""
    device_code = config.get(CONF_DEVICE_CODE)
    device_files_subdir = os.path.join('codes', 'fan')
    device_files_absdir = os.path.join(COMPONENT_ABS_DIR, device_files_subdir)

    # 透過 executor 執行同步的 OS 檔案操作，避免阻塞 Event Loop
    await hass.async_add_executor_job(os.makedirs, device_files_absdir, exist_ok=True)

    device_json_filename = f"{device_code}.json"
    device_json_path = os.path.join(device_files_absdir, device_json_filename)
    file_exists = await hass.async_add_executor_job(os.path.exists, device_json_path)

    if not file_exists:
        _LOGGER.warning("Couldn't find the device JSON file. The component will "
                        "try to download it from the GitHub repo.")
        try:
            codes_source = f"https://raw.githubusercontent.com/smartHomeHub/SmartIR/master/codes/fan/{device_code}.json"
            await Helper.downloader(codes_source, device_json_path)
        except Exception:
            _LOGGER.exception("There was an error while downloading the device JSON file. "
                              "Please check your internet connection or manually place the file.")
            return

    try:
        async with aiofiles.open(device_json_path, mode='r') as j:
            _LOGGER.debug("Loading JSON file: %s", device_json_path)
            content = await j.read()
            device_data = json.loads(content)
            _LOGGER.debug("File loaded: %s", device_json_path)
    except Exception:
        _LOGGER.exception("The device JSON file is invalid or corrupted.")
        return

    async_add_entities([SmartIRFan(hass, config, device_data)])


class SmartIRFan(FanEntity, RestoreEntity):
    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._power_sensor = config.get(CONF_POWER_SENSOR)

        self._manufacturer = device_data['manufacturer']
        self._supported_models = device_data['supportedModels']
        self._supported_controller = device_data['supportedController']
        self._commands_encoding = device_data['commandsEncoding']
        self._speed_list = device_data['speed']
        self._commands = device_data['commands']
        
        self._speed = SPEED_OFF
        self._direction = None
        self._last_on_speed = None
        self._oscillating = None
        
        self._support_flags = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_OFF
            | FanEntityFeature.TURN_ON
        )

        if DIRECTION_REVERSE in self._commands and DIRECTION_FORWARD in self._commands:
            self._direction = DIRECTION_REVERSE
            self._support_flags |= FanEntityFeature.DIRECTION
            
        if 'oscillate' in self._commands:
            self._oscillating = False
            self._support_flags |= FanEntityFeature.OSCILLATE

        self._temp_lock = asyncio.Lock()
        self._on_by_remote = False

        # Init the IR/RF controller
        self._controller = get_controller(
            self.hass,
            self._supported_controller, 
            self._commands_encoding,
            self._controller_data,
            self._delay
        )

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()
    
        last_state = await self.async_get_last_state()

        if last_state is not None:
            if 'speed' in last_state.attributes:
                self._speed = last_state.attributes['speed']

            # If _direction has a value the direction controls appears 
            # in UI even if SUPPORT_DIRECTION is not provided in the flags
            if 'direction' in last_state.attributes and (self._support_flags & FanEntityFeature.DIRECTION):
                self._direction = last_state.attributes['direction']

            if 'last_on_speed' in last_state.attributes:
                self._last_on_speed = last_state.attributes['last_on_speed']

        if self._power_sensor:
            async_track_state_change_event(
                self.hass, self._power_sensor, self._async_power_sensor_changed
            )

    @property
    def unique_id(self): return self._unique_id

    @property
    def name(self): return self._name

    @property
    def state(self):
        if self._on_by_remote or self._speed != SPEED_OFF:
            return STATE_ON
        return SPEED_OFF

    @property
    def percentage(self):
        if self._speed == SPEED_OFF:
            return 0
        return ordered_list_item_to_percentage(self._speed_list, self._speed)

    @property
    def speed_count(self):
        return len(self._speed_list)

    @property
    def oscillating(self): return self._oscillating

    @property
    def current_direction(self): return self._direction

    @property
    def last_on_speed(self): return self._last_on_speed

    @property
    def supported_features(self): return self._support_flags

    @property
    def extra_state_attributes(self):
        return {
            'last_on_speed': self._last_on_speed,
            'device_code': self._device_code,
            'manufacturer': self._manufacturer,
            'supported_models': self._supported_models,
            'supported_controller': self._supported_controller,
            'commands_encoding': self._commands_encoding,
        }

    async def async_set_percentage(self, percentage: int):
        """Set the desired speed for the fan."""
        if percentage == 0:
            self._speed = SPEED_OFF
        else:
            self._speed = percentage_to_ordered_list_item(self._speed_list, percentage)

        if self._speed != SPEED_OFF:
            self._last_on_speed = self._speed

        await self.send_command()
        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        """Set oscillation of the fan."""
        self._oscillating = oscillating
        await self.send_command()
        self.async_write_ha_state()

    async def async_set_direction(self, direction: str):
        """Set the direction of the fan"""
        self._direction = direction

        if self._speed != SPEED_OFF:
            await self.send_command()

        self.async_write_ha_state()

    async def async_turn_on(self, percentage: int = None, preset_mode: str = None, **kwargs):
        """Turn on the fan."""
        if percentage is None:
            percentage = ordered_list_item_to_percentage(
                self._speed_list, self._last_on_speed or self._speed_list[0]
            )
        await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs):
        """Turn off the fan."""
        await self.async_set_percentage(0)

    async def send_command(self):
        async with self._temp_lock:
            self._on_by_remote = False
            speed = self._speed
            direction = self._direction or 'default'
            oscillating = self._oscillating

            if speed == SPEED_OFF:
                command = self._commands.get('off')
            elif oscillating and 'oscillate' in self._commands:
                command = self._commands.get('oscillate')
            else:
                # 確保結構存在，避免引發 KeyError
                command = self._commands.get(direction, {}).get(speed)

            if not command:
                _LOGGER.error("Command not found for Fan state. Direction: %s, Speed: %s", direction, speed)
                return

            try:
                await self._controller.send(command)
            except Exception:
                _LOGGER.exception("Failed to send command to the Fan controller")

    @callback
    async def _async_power_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle power sensor changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if new_state is None or (old_state and new_state.state == old_state.state):
            return

        if new_state.state == STATE_ON and self._speed == SPEED_OFF:
            self._on_by_remote = True
            # 將速度重置為 None 會讓前端或其它邏輯出錯，這裡保留原本框架，或是重置為上一次速度
            # 為兼容舊版行為，此處先保持 HA 原有的處理方式 (None)
            self._speed = None
            self.async_write_ha_state()

        elif new_state.state == STATE_OFF and self._speed != SPEED_OFF:
            self._on_by_remote = False
            self._speed = SPEED_OFF
            self.async_write_ha_state()
