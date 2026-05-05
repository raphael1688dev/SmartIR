import asyncio
import json
import logging
import os

import aiofiles
import voluptuous as vol

from homeassistant.components.media_player import (
    MediaPlayerEntity, PLATFORM_SCHEMA)
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature, MediaType)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity

from . import COMPONENT_ABS_DIR, Helper
from .controller import get_controller

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Media Player"
DEFAULT_DEVICE_CLASS = "tv"
DEFAULT_DELAY = 0.5

CONF_UNIQUE_ID = 'unique_id'
CONF_DEVICE_CODE = 'device_code'
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = 'power_sensor'
CONF_SOURCE_NAMES = 'source_names'
CONF_DEVICE_CLASS = 'device_class'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_DEVICE_CODE): cv.positive_int,
    vol.Required(CONF_CONTROLLER_DATA): cv.string,
    vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.string,
    vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
    vol.Optional(CONF_SOURCE_NAMES): dict,
    vol.Optional(CONF_DEVICE_CLASS, default=DEFAULT_DEVICE_CLASS): cv.string
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the IR Media Player platform."""
    device_code = config.get(CONF_DEVICE_CODE)
    device_files_subdir = os.path.join('codes', 'media_player')
    device_files_absdir = os.path.join(COMPONENT_ABS_DIR, device_files_subdir)

    # 透過 executor 執行同步的 OS 檔案操作
    await hass.async_add_executor_job(os.makedirs, device_files_absdir, exist_ok=True)

    device_json_filename = f"{device_code}.json"
    device_json_path = os.path.join(device_files_absdir, device_json_filename)
    file_exists = await hass.async_add_executor_job(os.path.exists, device_json_path)

    if not file_exists:
        _LOGGER.warning("Couldn't find the device JSON file. The component will "
                        "try to download it from the GitHub repo.")
        try:
            codes_source = f"https://raw.githubusercontent.com/smartHomeHub/SmartIR/master/codes/media_player/{device_code}.json"
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

    async_add_entities([SmartIRMediaPlayer(hass, config, device_data)])


class SmartIRMediaPlayer(MediaPlayerEntity, RestoreEntity):
    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._device_class = config.get(CONF_DEVICE_CLASS)

        self._manufacturer = device_data['manufacturer']
        self._supported_models = device_data['supportedModels']
        self._supported_controller = device_data['supportedController']
        self._commands_encoding = device_data['commandsEncoding']
        self._commands = device_data.get('commands', {})

        self._state = STATE_OFF
        self._sources_list = []
        self._source = None
        self._support_flags = 0

        # Supported features (使用現代賦值語法與安全的 dict.get)
        if self._commands.get('off'):
            self._support_flags |= MediaPlayerEntityFeature.TURN_OFF
        if self._commands.get('on'):
            self._support_flags |= MediaPlayerEntityFeature.TURN_ON
        if self._commands.get('previousChannel'):
            self._support_flags |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        if self._commands.get('nextChannel'):
            self._support_flags |= MediaPlayerEntityFeature.NEXT_TRACK
        if self._commands.get('volumeDown') or self._commands.get('volumeUp'):
            self._support_flags |= MediaPlayerEntityFeature.VOLUME_STEP
        if self._commands.get('mute'):
            self._support_flags |= MediaPlayerEntityFeature.VOLUME_MUTE

        if self._commands.get('sources'):
            self._support_flags |= (MediaPlayerEntityFeature.SELECT_SOURCE | MediaPlayerEntityFeature.PLAY_MEDIA)

            # Source 名稱映射處理
            for source, new_name in config.get(CONF_SOURCE_NAMES, {}).items():
                if source in self._commands['sources']:
                    # 使用 dict.pop 可以直接將舊 key 移除並取值，寫法更乾淨
                    source_cmd = self._commands['sources'].pop(source)
                    if new_name is not None:
                        self._commands['sources'][new_name] = source_cmd

            self._sources_list = list(self._commands['sources'].keys())

        self._temp_lock = asyncio.Lock()

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
            self._state = last_state.state

        # 將 Polling 改為 Event-Driven 監聽
        if self._power_sensor:
            async_track_state_change_event(
                self.hass, self._power_sensor, self._async_power_sensor_changed
            )

    @property
    def should_poll(self):
        """Return False as updates are now pushed via events."""
        # 修正：既然已改用 Event 監聽，就不應該持續 Polling 浪費資源
        return False

    @property
    def unique_id(self): return self._unique_id

    @property
    def name(self): return self._name

    @property
    def device_class(self): return self._device_class

    @property
    def state(self): return self._state

    @property
    def media_title(self): return None

    @property
    def media_content_type(self): return MediaType.CHANNEL

    @property
    def source_list(self): return self._sources_list
        
    @property
    def source(self): return self._source

    @property
    def supported_features(self): return self._support_flags

    @property
    def extra_state_attributes(self):
        return {
            'device_code': self._device_code,
            'manufacturer': self._manufacturer,
            'supported_models': self._supported_models,
            'supported_controller': self._supported_controller,
            'commands_encoding': self._commands_encoding,
        }

    async def async_turn_off(self):
        """Turn the media player off."""
        await self.send_command(self._commands.get('off'))
        if self._power_sensor is None:
            self._state = STATE_OFF
            self._source = None
            self.async_write_ha_state()

    async def async_turn_on(self):
        """Turn the media player on."""
        await self.send_command(self._commands.get('on'))
        if self._power_sensor is None:
            self._state = STATE_ON
            self.async_write_ha_state()

    async def async_media_previous_track(self):
        """Send previous track command."""
        await self.send_command(self._commands.get('previousChannel'))

    async def async_media_next_track(self):
        """Send next track command."""
        await self.send_command(self._commands.get('nextChannel'))

    async def async_volume_down(self):
        """Turn volume down for media player."""
        await self.send_command(self._commands.get('volumeDown'))

    async def async_volume_up(self):
        """Turn volume up for media player."""
        await self.send_command(self._commands.get('volumeUp'))
    
    async def async_mute_volume(self, mute):
        """Mute the volume."""
        await self.send_command(self._commands.get('mute'))

    async def async_select_source(self, source):
        """Select channel from source."""
        self._source = source
        source_cmd = self._commands.get('sources', {}).get(source)
        if source_cmd:
            await self.send_command(source_cmd)
            self.async_write_ha_state()

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Support channel change through play_media service."""
        if self._state == STATE_OFF:
            await self.async_turn_on()

        if media_type != MediaType.CHANNEL:
            _LOGGER.error("Invalid media type. Expected %s", MediaType.CHANNEL)
            return
            
        if not str(media_id).isdigit():
            _LOGGER.error("media_id must be a numeric channel number")
            return

        self._source = f"Channel {media_id}"
        for digit in str(media_id):
            digit_cmd = self._commands.get('sources', {}).get(f"Channel {digit}")
            if digit_cmd:
                await self.send_command(digit_cmd)
                
        self.async_write_ha_state()

    async def send_command(self, command):
        """Send command securely."""
        if not command:
            return
            
        async with self._temp_lock:
            try:
                await self._controller.send(command)
            except Exception:
                _LOGGER.exception("Failed to send command to the Media Player controller")

    @callback
    async def _async_power_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle power sensor changes (Replaces polling async_update)."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        if new_state.state == STATE_OFF:
            self._state = STATE_OFF
            self._source = None
        elif new_state.state == STATE_ON:
            self._state = STATE_ON

        self.async_write_ha_state()
