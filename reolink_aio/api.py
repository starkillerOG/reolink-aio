""" Reolink NVR/camera network API """
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import ssl
import traceback
import uuid
from os.path import basename
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib import parse
from xml.etree import ElementTree as XML

import aiohttp

from . import templates, typings
from .exceptions import ApiError, CredentialsInvalidError, InvalidContentTypeError, ReolinkError, NoDataError, UnexpectedDataError, InvalidParameterError, NotSupportedError
from .software_version import SoftwareVersion

MANUFACTURER = "Reolink"
DEFAULT_STREAM = "sub"
DEFAULT_PROTOCOL = "rtmp"
DEFAULT_TIMEOUT = 60
DEFAULT_RTMP_AUTH_METHOD = "PASSWORD"
SUBSCRIPTION_TERMINATION_TIME = 15

MOTION_DETECTION_TYPE = "motion"
FACE_DETECTION_TYPE = "face"
PERSON_DETECTION_TYPE = "person"
VEHICLE_DETECTION_TYPE = "vehicle"
PET_DETECTION_TYPE = "pet"
VISITOR_DETECTION_TYPE = "visitor"

_LOGGER = logging.getLogger(__name__)
_LOGGER_DATA = logging.getLogger(__name__ + ".data")

# ref_sw_version_3_0_0_0_0 = SoftwareVersion("v3.0.0.0_0")
# ref_sw_version_3_1_0_0_0 = SoftwareVersion("v3.1.0.0_0")

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.set_ciphers("DEFAULT")
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


##########################################################################################################################################################
# API class
##########################################################################################################################################################
class Host:
    """Reolink network API class."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: Optional[int] = None,
        use_https: Optional[bool] = None,
        protocol: str = DEFAULT_PROTOCOL,
        stream: str = DEFAULT_STREAM,
        timeout: int = DEFAULT_TIMEOUT,
        rtmp_auth_method: str = DEFAULT_RTMP_AUTH_METHOD,
        aiohttp_get_session_callback=None,
    ):

        self._send_mutex = asyncio.Lock()
        self._login_mutex = asyncio.Lock()

        ##############################################################################
        # Host
        self._url: str = ""
        self._use_https: Optional[bool] = use_https
        self._host: str = host
        self._external_host: Optional[str] = None
        self._external_port: Optional[str] = None
        self._port: Optional[int] = port
        self._rtsp_port: Optional[int] = None
        self._rtmp_port: Optional[int] = None
        self._onvif_port: Optional[int] = None
        self._rtsp_enabled: Optional[bool] = None
        self._rtmp_enabled: Optional[bool] = None
        self._onvif_enabled: Optional[bool] = None
        self._mac_address: Optional[str] = None

        ##############################################################################
        # Login session
        self._username: str = username
        self._password: str = password[:31]
        self._token: Optional[str] = None
        self._lease_time: Optional[datetime] = None
        # Connection session
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout)
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._aiohttp_get_session_callback = None
        if aiohttp_get_session_callback is not None:
            self._aiohttp_get_session_callback = aiohttp_get_session_callback
        else:
            self._aiohttp_session = aiohttp.ClientSession(timeout=self._timeout, connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT))

        ##############################################################################
        # NVR (host-level) attributes
        self._is_nvr: bool = False
        self._nvr_name: Optional[str] = None
        self._nvr_serial: Optional[str] = None
        self._nvr_model: Optional[str] = None
        self._nvr_num_channels: int = 0
        self._nvr_hw_version: Optional[str] = None
        self._nvr_sw_version: Optional[str] = None
        self._nvr_sw_version_object: Optional[SoftwareVersion] = None

        ##############################################################################
        # Channels of cameras, used in this NVR ([0] for a directly connected camera)
        self._GetChannelStatus_present: bool = False
        self._GetChannelStatus_has_name: bool = False
        self._channels: list[int] = []
        self._channel_names: dict[int, str] = {}
        self._channel_models: dict[int, str] = {}

        ##############################################################################
        # Video-stream formats
        self._stream: str = stream
        self._protocol: str = protocol
        self._rtmp_auth_method: str = rtmp_auth_method
        self._rtsp_mainStream: dict[int, str] = {}
        self._rtsp_subStream: dict[int, str] = {}

        ##############################################################################
        # Presets
        self._ptz_support: dict[int, int] = {}
        self._ptz_presets: dict[int, dict] = {}
        self._sensitivity_presets: dict[int, dict] = {}

        ##############################################################################
        # Saved info response-blocks
        self._hdd_info: Optional[dict] = None
        self._local_link: Optional[dict] = None
        self._users: Optional[dict] = None

        ##############################################################################
        # Saved settings response-blocks
        # Host-level
        self._time_settings: Optional[dict] = None
        self._ntp_settings: Optional[dict] = None
        self._netport_settings: Optional[dict] = None
        # Camera-level
        self._zoom_focus_settings: dict[int, dict] = {}
        self._auto_focus_settings: dict[int, dict] = {}
        self._isp_settings: dict[int, dict] = {}
        self._ftp_settings: dict[int, dict] = {}
        self._osd_settings: dict[int, dict] = {}
        self._push_settings: dict[int, dict] = {}
        self._enc_settings: dict[int, dict] = {}
        self._ptz_presets_settings: dict[int, dict] = {}
        self._email_settings: dict[int, dict] = {}
        self._ir_settings: dict[int, dict] = {}
        self._power_led_settings: dict[int, dict] = {}
        self._whiteled_settings: dict[int, dict] = {}
        self._recording_settings: dict[int, dict] = {}
        self._alarm_settings: dict[int, dict] = {}
        self._audio_alarm_settings: dict[int, dict] = {}

        ##############################################################################
        # States
        self._motion_detection_states: dict[int, bool] = {}
        self._is_doorbell_enabled: dict[int, bool] = {}
        self._ai_detection_support: dict[int, dict[str, bool]] = {}

        ##############################################################################
        # Camera-level states
        # Actually these must be divided into host-level and camera-level parts.
        # BUT there is a data-normalization bug in Reolink network API: in commands/responses Host-level attributes are mixed with
        # Channel-level attributes. So if you want to obtain some host-level switch (like e.g. "ftp enabled") - you still need to supply
        # a channel-number, and will get a not-requested bulky schedule-block response for that camera among requested host-level attributes.
        self._email_enabled: dict[int, bool] = {}
        self._recording_enabled: dict[int, bool] = {}
        self._audio_alarm_enabled: dict[int, bool] = {}
        self._ftp_enabled: dict[int, bool] = {}
        self._push_enabled: dict[int, bool] = {}
        self._audio_enabled: dict[int, bool] = {}
        self._ir_enabled: dict[int, bool] = {}
        self._power_led_enabled: dict[int, bool] = {}
        self._doorbell_light_enabled: dict[int, bool] = {}
        self._whiteled_enabled: dict[int, bool] = {}
        self._whiteled_modes: dict[int, int | None] = {}
        self._daynight_state: dict[int, str] = {}
        self._backlight_state: dict[int, str] = {}
        self._ai_detection_states: dict[int, dict[str, bool]] = {}
        self._visitor_states: dict[int, bool] = {}

        ##############################################################################
        # API-versions of commands
        self._api_version_getevents: int = 0
        self._api_version_getemail: int = 1
        self._api_version_getrec: int = 1
        self._api_version_getftp: int = 1
        self._api_version_getpush: int = 1
        self._api_version_getalarm: int = 1

        self.refresh_base_url()

        ##############################################################################
        # SUBSCRIPTION managing
        self._subscribe_url: Optional[str] = None

        self._subscription_manager_url: Optional[str] = None
        self._subscription_termination_time: Optional[datetime] = None
        self._subscription_time_difference: Optional[float] = None
        self._onvif_only_motion = True
        self._log_once: list[str] = []

    ##############################################################################
    # Properties
    @property
    def host(self) -> str:
        return self._host

    @property
    def username(self) -> str:
        return self._username

    @property
    def external_host(self) -> Optional[str]:
        return self._external_host

    @external_host.setter
    def external_host(self, value: Optional[str]):
        self._external_host = value

    @property
    def use_https(self) -> Optional[bool]:
        return self._use_https

    @property
    def external_port(self) -> Optional[str]:
        return self._external_port

    @external_port.setter
    def external_port(self, value: Optional[str]):
        self._external_port = value

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def onvif_port(self) -> Optional[int]:
        return self._onvif_port

    @property
    def rtmp_port(self) -> Optional[int]:
        return self._rtmp_port

    @property
    def rtsp_port(self) -> Optional[int]:
        return self._rtsp_port

    @property
    def onvif_enabled(self) -> Optional[bool]:
        return self._onvif_enabled

    @property
    def rtmp_enabled(self) -> Optional[bool]:
        return self._rtmp_enabled

    @property
    def rtsp_enabled(self) -> Optional[bool]:
        return self._rtsp_enabled

    @property
    def mac_address(self) -> Optional[str]:
        return self._mac_address

    @property
    def serial(self) -> Optional[str]:
        return self._nvr_serial

    @property
    def is_nvr(self) -> bool:
        return self._is_nvr

    @property
    def nvr_name(self) -> Optional[str]:
        if not self._is_nvr and (self._nvr_name is None or self._nvr_name == ""):
            if len(self._channels) > 0 and self._channel_names is not None and self._channels[0] in self._channel_names:
                return self._channel_names[self._channels[0]]

            return "Unknown"
        return self._nvr_name

    @property
    def sw_version(self) -> Optional[str]:
        return self._nvr_sw_version

    @property
    def model(self) -> Optional[str]:
        return self._nvr_model

    @property
    def hardware_version(self) -> Optional[str]:
        return self._nvr_hw_version

    @property
    def manufacturer(self) -> str:
        return MANUFACTURER

    @property
    def num_channels(self) -> int:
        """Return the total number of channels in the NVR (should be 1 for a standalone camera, maybe 2 for DUO cameras)."""
        return self._nvr_num_channels

    @property
    def num_cameras(self) -> int:
        """Return the number of channels IN USE in that NVR (should be 1 for a standalone camera, maybe 2 for DUO cameras)."""
        return len(self._channels)

    @property
    def channels(self) -> list[int]:
        """Return the list of indices of channels' in use."""
        return self._channels

    @property
    def hdd_info(self) -> Optional[dict]:
        return self._hdd_info

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def session_active(self) -> bool:
        if self._token is not None and self._lease_time is not None and self._lease_time > (datetime.now() + timedelta(seconds=5)):
            return True
        return False

    @property
    def timeout(self) -> Optional[float]:
        return self._timeout.total

    @timeout.setter
    def timeout(self, value: float):
        self._timeout = aiohttp.ClientTimeout(total=value)

    @property
    def user_level(self) -> str:
        """Check if the user has admin authorisation."""
        if self._users is None or len(self._users) < 1:
            return "unknown"

        for user in self._users:
            if user["userName"] == self._username:
                return user["level"]

        return "unknown"

    @property
    def is_admin(self) -> bool:
        """
        Check if the user has admin authorisation.
        Only admin users can change camera settings, not everything will work if account is not admin
        """
        return self.user_level == "admin"

    ##############################################################################
    # Channel-level getters/setters

    def camera_name(self, channel: int) -> Optional[str]:
        if self._channel_names is None or channel not in self._channel_names:
            return "Unknown"
        return self._channel_names[channel]

    def camera_model(self, channel: int) -> Optional[str]:
        if self._channel_models is None or channel not in self._channel_models:
            return "Unknown"
        return self._channel_models[channel]

    def motion_detected(self, channel: int) -> bool:
        """Return the motion detection state (polled)."""
        return channel in self._motion_detection_states and self._motion_detection_states[channel]

    def ai_detected(self, channel: int, object_type: str) -> bool:
        """Return the AI object detection state (polled)."""
        if channel not in self._ai_detection_states or self._ai_detection_states[channel] is None:
            return False

        for key, value in self._ai_detection_states[channel].items():
            if key == object_type or (object_type == PERSON_DETECTION_TYPE and key == "people") or (object_type == PET_DETECTION_TYPE and key == "dog_cat"):
                return value

        return False

    def ai_detection_states(self, channel: int) -> dict[str, bool]:
        """Return all the AI object detection state."""
        return self._ai_detection_states[channel]

    def visitor_detected(self, channel: int) -> bool:
        """Return the visitor detection state (polled)."""
        return channel in self._visitor_states and self._visitor_states[channel]

    def ai_supported(self, channel: int, object_type: Optional[str] = None) -> bool:
        """Return if the AI object type detection is supported or not."""
        if channel not in self._ai_detection_support or not self._ai_detection_support[channel]:
            return False

        if object_type is not None:
            for key, value in self._ai_detection_support[channel].items():
                if key == object_type or (object_type == PERSON_DETECTION_TYPE and key == "people") or (object_type == PET_DETECTION_TYPE and key == "dog_cat"):
                    return value
            return False

        return True

    def audio_alarm_enabled(self, channel: int) -> bool:
        return self._audio_alarm_enabled is not None and channel in self._audio_alarm_enabled and self._audio_alarm_enabled[channel]

    def ir_enabled(self, channel: int) -> bool:
        return self._ir_enabled is not None and channel in self._ir_enabled and self._ir_enabled[channel]

    def power_led_enabled(self, channel: int) -> bool:
        return self._power_led_enabled is not None and channel in self._power_led_enabled and self._power_led_enabled[channel]

    def doorbell_light_enabled(self, channel: int) -> bool:
        return self._doorbell_light_enabled is not None and channel in self._doorbell_light_enabled and self._doorbell_light_enabled[channel]

    def whiteled_enabled(self, channel: int) -> bool:
        return self._whiteled_enabled is not None and channel in self._whiteled_enabled and self._whiteled_enabled[channel]

    def ftp_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._ftp_enabled is not None and 0 in self._ftp_enabled and self._ftp_enabled[0]

        return self._ftp_enabled is not None and channel in self._ftp_enabled and self._ftp_enabled[channel]

    def email_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._email_enabled is not None and 0 in self._email_enabled and self._email_enabled[0]

        return self._email_enabled is not None and channel in self._email_enabled and self._email_enabled[channel]

    def push_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._push_enabled is not None and 0 in self._push_enabled and self._push_enabled[0]

        return self._push_enabled is not None and channel in self._push_enabled and self._push_enabled[channel]

    def recording_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._recording_enabled is not None and 0 in self._recording_enabled and self._recording_enabled[0]

        return self._recording_enabled is not None and channel in self._recording_enabled and self._recording_enabled[channel]

    def whiteled_mode(self, channel: int) -> Optional[int]:
        if channel in self._whiteled_modes:
            return self._whiteled_modes[channel]

        return None

    def whiteled_schedule(self, channel: int) -> Optional[dict]:
        """Return the spotlight state."""
        if self._whiteled_settings is not None and channel in self._whiteled_settings:
            return self._whiteled_settings[channel]["WhiteLed"]["LightingSchedule"]

        return None

    def whiteled_settings(self, channel: int) -> Optional[dict]:
        """Return the spotlight state."""
        if self._whiteled_settings is not None and channel in self._whiteled_settings:
            return self._whiteled_settings[channel]

        return None

    def daynight_state(self, channel: int) -> Optional[str]:
        if self._daynight_state is not None and channel in self._daynight_state:
            return self._daynight_state[channel]

        return None

    def backlight_state(self, channel: int) -> Optional[str]:
        if self._backlight_state is not None and channel in self._backlight_state:
            return self._backlight_state[channel]

        return None

    def audio_state(self, channel: int) -> bool:
        return self._audio_enabled is not None and channel in self._audio_enabled and self._audio_enabled[channel]

    def audio_alarm_settings(self, channel: int) -> Optional[dict]:
        if self._audio_alarm_settings is not None and channel in self._audio_alarm_settings:
            return self._audio_alarm_settings[channel]

        return None

    def ptz_presets(self, channel: int) -> dict:
        if self._ptz_presets is not None and channel in self._ptz_presets:
            return self._ptz_presets[channel]

        return {}

    def sensitivity_presets(self, channel: int) -> dict:
        if self._sensitivity_presets is not None and channel in self._sensitivity_presets:
            return self._sensitivity_presets[channel] if self._sensitivity_presets[channel] is not None else {}

        return {}

    def ptz_supported(self, channel: int) -> bool:
        return channel in self._ptz_support and self._ptz_support[channel] != 0

    def zoom_supported(self, channel: int) -> bool:
        return channel in self._ptz_support and self._ptz_support[channel] in [1, 2, 5]

    def pan_tilt_supported(self, channel: int) -> bool:
        return channel in self._ptz_support and self._ptz_support[channel] in [2, 3, 5]

    def is_doorbell_enabled(self, channel: int) -> bool:
        """Wether or not the camera supports doorbell"""
        return self._is_doorbell_enabled is not None and channel in self._is_doorbell_enabled and self._is_doorbell_enabled[channel]

    def enable_https(self, enable: bool):
        self._use_https = enable
        self.refresh_base_url()

    def refresh_base_url(self):
        if self._use_https:
            self._url = f"https://{self._host}:{self._port}/cgi-bin/api.cgi"
        else:
            self._url = f"http://{self._host}:{self._port}/cgi-bin/api.cgi"

    async def login(self) -> bool:
        if self._port is None or self._use_https is None:
            return await self._login_try_ports()

        await self._login_mutex.acquire()

        try:
            if self._token is not None and self._lease_time is not None and self._lease_time > (datetime.now() + timedelta(seconds=300)):
                return True

            await self.logout(True)  # Ensure there would be no "max session" error

            _LOGGER.debug(
                "Host %s:%s, trying to login with user %s...",
                self._host,
                self._port,
                self._username,
            )

            body = [
                {
                    "cmd": "Login",
                    "action": 0,
                    "param": {
                        "User": {
                            "userName": self._username,
                            "password": self._password,
                        }
                    },
                }
            ]
            param = {"cmd": "Login"}

            try:
                json_data = await self.send(body, param, expected_content_type="json")
            except ApiError:
                return False
            except aiohttp.ClientConnectorError:
                return False
            except InvalidContentTypeError:
                _LOGGER.error(
                    "Host %s:%s: error translating login response.",
                    self._host,
                    self._port,
                )
                return False
            if json_data is None:
                _LOGGER.error(
                    "Host: %s:%s: error receiving Reolink login response.",
                    self._host,
                    self._port,
                )
                return False

            _LOGGER.debug("Got login response from %s:%s: %s", self._host, self._port, json_data)

            try:
                if json_data[0]["code"] == 0:
                    self._lease_time = datetime.now() + timedelta(seconds=float(json_data[0]["value"]["Token"]["leaseTime"]))
                    self._token = str(json_data[0]["value"]["Token"]["name"])

                    _LOGGER.debug(
                        "Logged in at host %s:%s. Leasetime %s, token %s",
                        self._host,
                        self._port,
                        self._lease_time.strftime("%d-%m-%Y %H:%M"),
                        self._token,
                    )
                    # Looks like some devices fail with not-logged-in if subsequent command sent with no delay, not sure 100% though...
                    # I've seen RLC-520A failed with 0.5s, but did not try to set more. Need to gather some more logging data from users...
                    # asyncio.sleep(0.5)
                    return True
            except Exception:
                _LOGGER.error(
                    "Host %s:%s: login error, unknown response format.",
                    self._host,
                    self._port,
                )
                self.clear_token()
                return False

            _LOGGER.error("Failed to login at host %s:%s.", self._host, self._port)
            return False
        finally:
            self._login_mutex.release()

    async def _login_try_ports(self) -> bool:
        self._port = 443
        self.enable_https(True)
        if await self.login():
            return True

        self._port = 80
        self.enable_https(False)
        return await self.login()

    async def logout(self, mutex_owned=False):
        body = [{"cmd": "Logout", "action": 0, "param": {}}]

        if not mutex_owned:
            await self._login_mutex.acquire()

        try:
            if self._token:
                param = {"cmd": "Logout"}
                await self.send(body, param)
            # Reolink has a bug in some cameras' firmware: the Logout command issued without a token breaks the subsequent commands:
            # even if Login command issued AFTER that successfully returns a token, any command with that token would return "Please login first" error.
            # Thus it is not available for now to exit the previous "stuck" sessions after sudden crash or power failure:
            # Reolink has restricted amount of sessions on a device, so in such case the component would not be able to login
            # into a device before some previos session expires an hour later...
            # If Reolink fixes this and makes Logout work with login/pass pair instead of a token - this can be uncommented...
            # else:
            #     body  = [{"cmd": "Logout", "action": 0, "param": {"User": {"userName": self._username, "password": self._password}}}]
            #     param = {"cmd": "Logout"}
            #     await self.send(body, param)

            self.clear_token()
            if self._aiohttp_session is not None:
                await self._aiohttp_session.close()
        finally:
            if not mutex_owned:
                self._login_mutex.release()

    def expire_session(self):
        if self._lease_time is not None:
            self._lease_time = datetime.now() - timedelta(seconds=5)

    def clear_token(self):
        self._token = None
        self._lease_time = None

    async def get_switchable_capabilities(self, channel: int) -> list[str]:
        """Return the capabilities of the NVR/camera that could be switched on/off."""
        capabilities: list[str] = []

        if self._ftp_enabled is not None and channel in self._ftp_enabled and self._ftp_enabled[channel] is not None:
            capabilities.append("ftp")

        if self._push_enabled is not None and channel in self._push_enabled and self._push_enabled[channel] is not None:
            capabilities.append("push")

        if self._ir_enabled is not None and channel in self._ir_enabled and self._ir_enabled[channel] is not None:
            capabilities.append("irLights")

        if self._power_led_enabled is not None and channel in self._power_led_enabled and self._power_led_enabled[channel] is not None:
            capabilities.append("powerLed")

        if self._doorbell_light_enabled is not None and channel in self._doorbell_light_enabled and self._doorbell_light_enabled[channel] is not None:
            capabilities.append("doorbellLight")

        if self._whiteled_enabled is not None and channel in self._whiteled_enabled and self._whiteled_enabled[channel] is not None:
            capabilities.append("spotlight")

        if self._audio_alarm_enabled is not None and channel in self._audio_alarm_enabled and self._audio_alarm_enabled[channel] is not None:
            capabilities.append("siren")

        if self._recording_enabled is not None and channel in self._recording_enabled and self._recording_enabled[channel] is not None:
            capabilities.append("recording")

        if self._email_enabled is not None and channel in self._email_enabled and self._email_enabled[channel] is not None:
            capabilities.append("email")

        if self._audio_enabled is not None and channel in self._audio_enabled and self._audio_enabled[channel] is not None:
            capabilities.append("audio")

        if self.ptz_supported(channel):
            capabilities.append("ptzControl")
            if self.zoom_supported(channel):
                capabilities.append("zoomControl")
            if self.pan_tilt_supported(channel):
                capabilities.append("ptControl")
            if self._ptz_presets is not None and channel in self._ptz_presets and len(self._ptz_presets[channel]) != 0:
                capabilities.append("ptzPresets")

        if self._sensitivity_presets is not None and channel in self._sensitivity_presets and len(self._sensitivity_presets[channel]) != 0:
            capabilities.append("sensitivityPresets")

        if channel in self._motion_detection_states:
            capabilities.append("motionDetection")

        if self._daynight_state is not None and channel in self._daynight_state and self._daynight_state[channel] is not None:
            capabilities.append("dayNight")

        if self._backlight_state is not None and channel in self._backlight_state and self._backlight_state[channel] is not None:
            capabilities.append("backLight")

        return capabilities

    async def get_state(self, cmd: str) -> None:
        body = []
        channels = []
        for channel in self._channels:
            ch_body = []
            if cmd == "GetEnc":
                ch_body = [{"cmd": "GetEnc", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetIsp":
                ch_body = [{"cmd": "GetIsp", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetIrLights":
                ch_body = [{"cmd": "GetIrLights", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPowerLed":
                ch_body = [{"cmd": "GetPowerLed", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetWhiteLed":
                ch_body = [{"cmd": "GetWhiteLed", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPtzPreset":
                ch_body = [{"cmd": "GetPtzPreset", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetAutoFocus":
                ch_body = [{"cmd": "GetAutoFocus", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetZoomFocus":
                ch_body = [{"cmd": "GetZoomFocus", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetOsd":
                ch_body = [{"cmd": "GetOsd", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetAlarm":
                ch_body = [
                    {
                        "cmd": "GetAlarm",
                        "action": 0,
                        "param": {"Alarm": {"channel": channel, "type": "md"}},
                    }
                ]
            elif cmd in ["GetEmail", "GetEmailV20"]:
                if self._api_version_getemail == 0:
                    ch_body = [{"cmd": "GetEmail", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetEmailV20", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetPush", "GetPushV20"]:
                if self._api_version_getpush == 0:
                    ch_body = [{"cmd": "GetPush", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [
                        {
                            "cmd": "GetPushV20",
                            "action": 0,
                            "param": {"channel": channel},
                        }
                    ]
            elif cmd in ["GetFtp", "GetFtpV20"]:
                if self._api_version_getftp == 0:
                    ch_body = [{"cmd": "GetFtp", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetFtpV20", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetRec", "GetRecV20"]:
                if self._api_version_getrec == 0:
                    ch_body = [{"cmd": "GetRec", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetRecV20", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetAudioAlarm", "GetAudioAlarmV20"]:
                if self._api_version_getalarm == 0:
                    ch_body = [
                        {
                            "cmd": "GetAudioAlarm",
                            "action": 0,
                            "param": {"channel": channel},
                        }
                    ]
                else:
                    ch_body = [{"cmd": "GetAudioAlarmV20", "action": 0, "param": {"channel": channel}}]
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        if not channels:
            if cmd == "Getchannelstatus":
                body = [{"cmd": "Getchannelstatus"}]
            elif cmd == "GetDevInfo":
                body = [{"cmd": "GetDevInfo", "action": 0, "param": {}}]
            elif cmd == "GetLocalLink":
                body = [{"cmd": "GetLocalLink", "action": 0, "param": {}}]
            elif cmd == "GetNetPort":
                body = [{"cmd": "GetNetPort", "action": 0, "param": {}}]
            elif cmd == "GetHddInfo":
                body = [{"cmd": "GetHddInfo", "action": 0, "param": {}}]
            elif cmd == "GetUser":
                body = [{"cmd": "GetUser", "action": 0, "param": {}}]
            elif cmd == "GetNtp":
                body = [{"cmd": "GetNtp", "action": 0, "param": {}}]
            elif cmd == "GetTime":
                body = [{"cmd": "GetTime", "action": 0, "param": {}}]
            elif cmd == "GetAbility":
                body = [{"cmd": "GetAbility", "action": 0, "param": {"User": {"userName": self._username}}}]

        if body:
            try:
                json_data = await self.send(body, expected_content_type="json")
            except InvalidContentTypeError as err:
                raise InvalidContentTypeError(f"get_state cmd '{body[0]['cmd']}': {str(err)}") from err
            if json_data is None:
                raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining get_state response for cmd '{body[0]['cmd']}'")

            if channels:
                self.map_channels_json_response(json_data, channels)
            else:
                self.map_host_json_response(json_data)

        return

    async def get_states(self) -> None:
        body = []
        channels = []
        for channel in self._channels:
            ch_body = [
                {"cmd": "GetEnc", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetIsp", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetIrLights", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetPowerLed", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetWhiteLed", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetOsd", "action": 0, "param": {"channel": channel}},
            ]
            if self._api_version_getevents >= 1:
                ch_body.append({"cmd": "GetEvents", "action": 0, "param": {"channel": channel}})
            else:
                ch_body.append({"cmd": "GetAiState", "action": 0, "param": {"channel": channel}})
                if self._api_version_getalarm >= 1:
                    ch_body.append({"cmd": "GetMdState", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetAlarm", "action": 0, "param": {"Alarm": {"channel": channel, "type": "md"}}})

            if self.pan_tilt_supported(channel):
                ch_body.append({"cmd": "GetPtzPreset", "action": 0, "param": {"channel": channel}})

            if self.zoom_supported(channel):
                ch_body.append({"cmd": "GetZoomFocus", "action": 0, "param": {"channel": channel}})

            if self.pan_tilt_supported(channel) and self.zoom_supported(channel):
                ch_body.append({"cmd": "GetAutoFocus", "action": 0, "param": {"channel": channel}})

            if self._api_version_getemail >= 1:
                ch_body.append({"cmd": "GetEmailV20", "action": 0, "param": {"channel": channel}})
            else:
                ch_body.append({"cmd": "GetEmail", "action": 0, "param": {"channel": channel}})

            if self._api_version_getpush >= 1:
                ch_body.append({"cmd": "GetPushV20", "action": 0, "param": {"channel": channel}})
            else:
                ch_body.append({"cmd": "GetPush", "action": 0, "param": {"channel": channel}})

            if self._api_version_getftp >= 1:
                ch_body.append({"cmd": "GetFtpV20", "action": 0, "param": {"channel": channel}})
            else:
                ch_body.append({"cmd": "GetFtp", "action": 0, "param": {"channel": channel}})

            if self._api_version_getrec >= 1:
                ch_body.append({"cmd": "GetRecV20", "action": 0, "param": {"channel": channel}})
            else:
                ch_body.append({"cmd": "GetRec", "action": 0, "param": {"channel": channel}})

            if self._api_version_getalarm >= 1:
                ch_body.append(
                    {
                        "cmd": "GetAudioAlarmV20",
                        "action": 0,
                        "param": {"channel": channel},
                    }
                )
            else:
                ch_body.append({"cmd": "GetAudioAlarm", "action": 0, "param": {"channel": channel}})

            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"channel-state: {str(err)}") from err
        if json_data is None:
            raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining channel-state response")

        self.map_channels_json_response(json_data, channels)

    async def get_host_data(self) -> None:
        """Fetch the host settings/capabilities."""
        body = [
            {"cmd": "Getchannelstatus"},
            {"cmd": "GetDevInfo", "action": 0, "param": {}},
            {"cmd": "GetLocalLink", "action": 0, "param": {}},
            {"cmd": "GetNetPort", "action": 0, "param": {}},
            {"cmd": "GetHddInfo", "action": 0, "param": {}},
            {"cmd": "GetUser", "action": 0, "param": {}},
            {"cmd": "GetNtp", "action": 0, "param": {}},
            {"cmd": "GetTime", "action": 0, "param": {}},
            {
                "cmd": "GetAbility",
                "action": 0,
                "param": {"User": {"userName": self._username}},
            },
        ]

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Get host-settings error: {str(err)}") from err
        if json_data is None:
            raise NoDataError(f"Host: {self._host}:{self._port}: returned no data when obtaining host-settings")

        self.map_host_json_response(json_data)

        body = []
        channels = []
        for channel in self._channels:
            ch_body = [
                {
                    "cmd": "GetAiState",
                    "action": 0,
                    "param": {"channel": channel},
                },  # to capture AI capabilities
                {"cmd": "GetEvents", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetRtspUrl", "action": 0, "param": {"channel": channel}},
            ]
            # checking API versions (because Reolink dev quality sucks big time we cannot fully trust GetAbility)
            if self._api_version_getemail >= 1:
                ch_body.append({"cmd": "GetEmailV20", "action": 0, "param": {"channel": channel}})
            if self._api_version_getpush >= 1:
                ch_body.append({"cmd": "GetPushV20", "action": 0, "param": {"channel": channel}})
            if self._api_version_getftp >= 1:
                ch_body.append({"cmd": "GetFtpV20", "action": 0, "param": {"channel": channel}})
            if self._api_version_getrec >= 1:
                ch_body.append({"cmd": "GetRecV20", "action": 0, "param": {"channel": channel}})
            if self._api_version_getalarm >= 1:
                ch_body.append(
                    {
                        "cmd": "GetAudioAlarmV20",
                        "action": 0,
                        "param": {"channel": channel},
                    }
                )

            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Channel-settings: {str(err)}") from err
        if json_data is None:
            raise NoDataError(f"Host: {self._host}:{self._port}: returned no data when obtaining initial channel-settings")
        typing.cast(dict, json_data)

        self.map_channels_json_response(json_data, channels)

        # Let's assume all channels of an NVR or multichannel-camera always have the same versions of commands... Not sure though...
        def check_command_exists(cmd: str) -> bool:
            for x in json_data:
                if x["cmd"] == cmd:
                    return True
            return False

        if check_command_exists("GetEvents"):
            self._api_version_getevents = 1
        else:
            self._api_version_getevents = 0

        if self._api_version_getemail >= 1:
            if not check_command_exists("GetEmailV20"):
                self._api_version_getemail = 0

        if self._api_version_getpush >= 1:
            if not check_command_exists("GetPushV20"):
                self._api_version_getpush = 0

        if self._api_version_getftp >= 1:
            if not check_command_exists("GetFtpV20"):
                self._api_version_getftp = 0

        if self._api_version_getrec >= 1:
            if not check_command_exists("GetRecV20"):
                self._api_version_getrec = 0

        if self._api_version_getalarm >= 1:
            if not check_command_exists("GetAudioAlarmV20"):
                self._api_version_getalarm = 0

    async def get_motion_state(self, channel: int) -> Optional[bool]:
        if channel not in self._channels:
            return None

        body = [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}}]

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                "Host %s:%s: error translating motion detection state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            return False
        if json_data is None:
            _LOGGER.error(
                "Host %s:%s: error obtaining motion state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            return False

        self.map_channel_json_response(json_data, channel)

        return None if channel not in self._motion_detection_states else self._motion_detection_states[channel]

    async def get_ai_state(self, channel: int) -> Optional[dict[str, bool]]:
        if channel not in self._channels:
            return None

        body = [{"cmd": "GetAiState", "action": 0, "param": {"channel": channel}}]

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                "Host %s:%s: error translating AI detection state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._ai_detection_states[channel] = {}
            return None
        if json_data is None:
            _LOGGER.error(
                "Host %s:%s: error obtaining AI detection state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._ai_detection_states[channel] = {}
            return None

        self.map_channel_json_response(json_data, channel)

        return (
            None
            if self._ai_detection_states is None or channel not in self._ai_detection_states or self._ai_detection_states[channel] is None
            else self._ai_detection_states[channel]
        )

    async def get_all_motion_states(self, channel: int) -> Optional[bool]:
        """Fetch All motions states at once (regular + AI + visitor)."""
        if channel not in self._channels:
            return None

        if self._api_version_getevents >= 1:
            body = [{"cmd": "GetEvents", "action": 0, "param": {"channel": channel}}]
        else:
            body = [
                {"cmd": "GetMdState", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetAiState", "action": 0, "param": {"channel": channel}},
            ]

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                "Host %s:%s: error translating All Motion States response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            self._ai_detection_states[channel] = {}
            return False
        if json_data is None:
            _LOGGER.error(
                "Host %s:%s: error obtaining All Motion States response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            self._ai_detection_states[channel] = {}
            return False

        self.map_channel_json_response(json_data, channel)

        return None if channel not in self._motion_detection_states else self._motion_detection_states[channel]

    async def get_motion_state_all_ch(self) -> Optional[bool]:
        """Fetch All motions states of all channels at once (regular + AI + visitor)."""
        body = []
        channels = []
        for channel in self._channels:
            if self._api_version_getevents >= 1:
                ch_body = [{"cmd": "GetEvents", "action": 0, "param": {"channel": channel}}]
            else:
                ch_body = [
                    {"cmd": "GetMdState", "action": 0, "param": {"channel": channel}},
                    {"cmd": "GetAiState", "action": 0, "param": {"channel": channel}},
                ]
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError as e:
            _LOGGER.error(
                "Host %s:%s: error translating All Motion States response: %s",
                self._host,
                self._port,
                e,
            )
            for channel in self._channels:
                self._motion_detection_states[channel] = False
                self._ai_detection_states[channel] = {}
            return False
        if json_data is None:
            _LOGGER.error(
                "Host %s:%s: error obtaining All Motion States response.",
                self._host,
                self._port,
            )
            for channel in self._channels:
                self._motion_detection_states[channel] = False
                self._ai_detection_states[channel] = {}
            return False

        self.map_channels_json_response(json_data, channels)
        return True

    async def check_new_firmware(self) -> bool | str:
        """check for new firmware, returns False if no new firmware available."""
        body = [{"cmd": "CheckFirmware"}]

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Check firmware: {str(err)}") from err
        if json_data is None:
            raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining CheckFirmware response")

        new_firmware = json_data[0]["value"]["newFirmware"]
        if new_firmware == 0:
            return False

        return json_data[0]["value"]["newFirmware"]

    async def update_firmware(self) -> None:
        """check for new firmware."""
        body = [{"cmd": "UpgradeOnline"}]
        await self.send_setting(body)

    async def update_progress(self) -> bool | int:
        """check progress of firmware update, returns False if not in progress."""
        body = [{"cmd": "UpgradeStatus"}]

        try:
            json_data = await self.send(body, expected_content_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Update progress: {str(err)}") from err
        if json_data is None:
            raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining update progress response")

        if json_data[0]["code"] != 0:
            return False

        return json_data[0]["value"]["Status"]["Persent"]

    async def get_snapshot(self, channel: int) -> Optional[list]:
        """Get the still image."""
        if channel not in self._channels:
            return None

        param = {"cmd": "Snap", "channel": channel}

        response = await self.send(None, param, expected_content_type="image/jpeg")
        if response is None or response == b"":
            _LOGGER.error(
                "Host: %s:%s: error obtaining still image response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self.expire_session()
            return None

        return response

    def get_flv_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        if channel not in self._channels:
            return None

        if stream is None:
            stream = self._stream

        if self._use_https:
            http_s = "https"
        else:
            http_s = "http"

        password = parse.quote(self._password)
        return f"{http_s}://{self._host}:{self._port}/flv?port={self._rtmp_port}&app=bcs&stream=channel{channel}_{stream}.bcs&user={self._username}&password={password}"

    def get_rtmp_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        if channel not in self._channels:
            return None

        if stream is None:
            stream = self._stream

        stream_type = None
        if stream == "sub":
            stream_type = 1
        else:
            stream_type = 0
        if self._rtmp_auth_method == DEFAULT_RTMP_AUTH_METHOD:
            password = parse.quote(self._password)
            return f"rtmp://{self._host}:{self._rtmp_port}/bcs/channel{channel}_{stream}.bcs?channel={channel}&stream={stream_type}&user={self._username}&password={password}"

        return f"rtmp://{self._host}:{self._rtmp_port}/bcs/channel{channel}_{stream}.bcs?channel={channel}&stream={stream_type}&token={self._token}"

    async def get_rtsp_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        if stream == "main" and channel in self._rtsp_mainStream:
            return self._rtsp_mainStream[channel]

        if stream == "sub" and channel in self._rtsp_subStream:
            return self._rtsp_subStream[channel]

        if not self._enc_settings:
            try:
                await self.get_state(cmd="GetEnc")
            except ReolinkError:
                return None

        if channel not in self._channels:
            return None

        if stream is None:
            stream = self._stream

        stream_enc = self._enc_settings[channel]["Enc"].get(f"{stream}Stream")
        if stream_enc is None:
            return None
        encoding = stream_enc.get("vType", "")

        password = parse.quote(self._password)
        channel = f"{channel + 1:02d}"

        return f"rtsp://{self._username}:{password}@{self._host}:{self._rtsp_port}/{encoding}Preview_{channel}_{stream}"

    async def get_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        """Return the stream source url."""
        if not await self.login():
            return None

        if stream is None:
            stream = self._stream

        if stream not in ["main", "sub", "ext"]:
            return None
        if self.protocol == "rtmp":
            return self.get_rtmp_stream_source(channel, stream)
        if self.protocol == "rtsp":
            return await self.get_rtsp_stream_source(channel, stream)
        if self.protocol == "flv":
            return self.get_flv_stream_source(channel, stream)
        return None

    async def get_vod_source(
        self,
        channel: int,
        filename: str,
        external_url: bool = False,
        stream: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Return the VOD source url."""
        if channel not in self._channels:
            return None, None
        if not await self.login():
            return None, None

        host_url: str = None
        if external_url and self._external_host:
            host_url = self._external_host
        else:
            host_url = self._host

        host_port: str = None
        if external_url and self._external_port:
            host_port = self._external_port
        else:
            host_port = self._port

        if self._use_https:
            http_s = "https"
        else:
            http_s = "http"

        if self._is_nvr:
            # NVR VoDs "type=0": Adobe flv
            # NVR VoDs "type=1": mp4
            return (
                "application/x-mpegURL",
                f"{http_s}://{host_url}:{host_port}/flv?port=1935&app=bcs&stream=playback.bcs&channel={channel}"
                f"&type=1&start={filename}&seek=0&user={self._username}&password={self._password}",
            )

        if external_url:
            return (
                "application/x-mpegURL",
                f"{http_s}://{host_url}:{host_port}/cgi-bin/api.cgi?&cmd=Playback&channel={channel}&source={filename}&user={self._username}&password={self._password}",
            )

        if stream is None:
            stream = self._stream

        stream_type = None
        if stream == "sub":
            stream_type = 1
        else:
            stream_type = 0
        # Reolink uses an odd encoding, if the camera provides a / in the filename it needs to be encoded with %20
        # Camera VoDs are only available over rtmp, rtsp is not an option
        file = filename.replace("/", "%20")
        # Looks like it only works with login/password method
        # return f"rtmp://{self._host}:{self._rtmp_port}/vod/{file}?channel={channel}&stream={stream_type}&token={self._token}"
        return (
            "application/x-mpegURL",
            f"rtmp://{self._host}:{self._rtmp_port}/vod/{file}?channel={channel}&stream={stream_type}&user={self._username}&password={self._password}",
        )

    def map_host_json_response(self, json_data):
        """Map the JSON objects to internal cache-objects."""
        for data in json_data:
            try:
                if data["code"] == 1:  # Error, like "ability error"
                    continue

                if data["cmd"] == "GetChannelstatus":
                    # Maybe later add a support of dynamic cameras' connect/disconnect, without API consumer re-init.
                    # A callback from here to the API consumer would be needed I think if changes are seen.
                    if not self._GetChannelStatus_present and (self._nvr_num_channels == 0 or len(self._channels) == 0):
                        self._channels.clear()
                        self._channel_models.clear()
                        self._is_doorbell_enabled.clear()

                        cur_value = data["value"]
                        self._nvr_num_channels = cur_value["count"]

                        if self._nvr_num_channels > 0:
                            cur_status = cur_value["status"]

                            # Not all Reolink devices respond with "name" attribute.
                            if "name" in cur_status[0]:
                                self._GetChannelStatus_has_name = True
                                self._channel_names.clear()
                            else:
                                self._GetChannelStatus_has_name = False

                            for ch_info in cur_status:
                                if ch_info["online"] == 1:
                                    cur_channel = ch_info["channel"]

                                    if self._GetChannelStatus_has_name:
                                        self._channel_names[cur_channel] = ch_info["name"]

                                    self._channel_models[cur_channel] = ch_info.get("typeInfo", "Unknown")  # Not all Reolink devices respond with "typeInfo" attribute.
                                    self._is_doorbell_enabled[cur_channel] = "Doorbell" in self._channel_models[cur_channel]
                                    self._channels.append(cur_channel)
                        else:
                            self._channel_names.clear()
                    elif self._GetChannelStatus_has_name:
                        cur_status = data["value"]["status"]
                        for ch_info in cur_status:
                            if ch_info["online"] == 1:
                                # Just a dynamic name change is OK for the current "non dynamic" behavior.
                                self._channel_names[ch_info["channel"]] = ch_info["name"]

                    if not self._GetChannelStatus_present:
                        self._GetChannelStatus_present = True

                    break

            except Exception as e:  # pylint: disable=bare-except
                _LOGGER.error(
                    "Host %s:%s failed mapping JSON data: %s, traceback:\n%s\n",
                    self._host,
                    self._port,
                    e,
                    traceback.format_exc(),
                )
                continue

        for data in json_data:
            try:
                if data["code"] == 1:  # Error, like "ability error"
                    continue

                if data["cmd"] == "GetDevInfo":
                    dev_info = data["value"]["DevInfo"]
                    self._is_nvr = dev_info.get("exactType", "CAM") == "NVR"
                    self._nvr_serial = dev_info["serial"]
                    self._nvr_name = dev_info["name"]
                    self._nvr_model: str = dev_info["model"]
                    self._nvr_hw_version = dev_info["hardVer"]
                    self._nvr_sw_version = dev_info["firmVer"]
                    self._nvr_sw_version_object = SoftwareVersion(self._nvr_sw_version)

                    # In case the "GetChannelStatus" command not supported by the device.
                    if not self._GetChannelStatus_present and self._nvr_num_channels == 0:
                        self._channels.clear()
                        self._channel_models.clear()
                        self._is_doorbell_enabled.clear()

                        self._nvr_num_channels = dev_info["channelNum"]

                        if self._is_nvr:
                            _LOGGER.warning(
                                "Your %s NVR doesn't support the 'Getchannelstatus' command. "
                                "Probably you need to update your firmware.\n"
                                "No way to recognize active channels, all %s channels will be considered 'active' as a result",
                                self._nvr_name,
                                self._nvr_num_channels,
                            )

                        if self._nvr_num_channels > 0:
                            is_doorbell = "Doorbell" in self._nvr_model
                            for i in range(self._nvr_num_channels):
                                self._channel_models[i] = self._nvr_model
                                self._is_doorbell_enabled[i] = is_doorbell
                                self._channels.append(i)
                        else:
                            self._channel_names.clear()

                elif data["cmd"] == "GetHddInfo":
                    self._hdd_info = data["value"]["HddInfo"]

                elif data["cmd"] == "GetLocalLink":
                    self._local_link = data["value"]
                    self._mac_address = data["value"]["LocalLink"]["mac"]

                elif data["cmd"] == "GetNetPort":
                    self._netport_settings = data["value"]
                    net_port = self._netport_settings["NetPort"]
                    self._rtsp_port = net_port["rtspPort"]
                    self._rtmp_port = net_port["rtmpPort"]
                    self._onvif_port = net_port["onvifPort"]
                    self._rtsp_enabled = net_port.get("rtspEnable", 1) == 1
                    self._rtmp_enabled = net_port.get("rtmpEnable", 1) == 1
                    self._onvif_enabled = net_port.get("onvifEnable", 1) == 1
                    self._subscribe_url = f"http://{self._host}:{self._onvif_port}/onvif/event_service"

                elif data["cmd"] == "GetUser":
                    self._users = data["value"]["User"]

                elif data["cmd"] == "GetNtp":
                    self._ntp_settings = data["value"]

                elif data["cmd"] == "GetTime":
                    self._time_settings = data["value"]

                elif data["cmd"] == "GetAbility":
                    host_abilities: dict[str, Any] = data["value"]["Ability"]
                    for ability, details in host_abilities.items():
                        if ability == "email":
                            self._api_version_getemail = details["ver"]
                        elif ability == "push":
                            self._api_version_getpush = details["ver"]
                        elif ability == "supportFtpEnable":
                            self._api_version_getftp = details["ver"]
                        elif ability == "supportRecordEnable":
                            self._api_version_getrec = details["ver"]
                        elif ability == "supportAudioAlarm":
                            self._api_version_getalarm = details["ver"]

                    channel_abilities: list = host_abilities["abilityChn"]
                    for channel in self._channels:
                        self._ptz_support[channel] = channel_abilities[channel]["ptzType"]["ver"]
                        self._api_version_getftp = channel_abilities[channel].get("ftp", {"ver": 1})["ver"]
                        self._api_version_getrec = channel_abilities[channel].get("recCfg", {"ver": 1})["ver"]
                        self._api_version_getalarm = channel_abilities[channel].get("supportAudioAlarm", {"ver": 1})["ver"]

            except Exception as e:  # pylint: disable=bare-except
                _LOGGER.error(
                    "Host %s:%s failed mapping JSON data: %s, traceback:\n%s\n",
                    self._host,
                    self._port,
                    e,
                    traceback.format_exc(),
                )
                continue

    def map_channels_json_response(self, json_data, channels: list[int]):
        if len(json_data) != len(channels):
            _LOGGER.error(
                "Host %s:%s error mapping response to channels, received %i responses while requesting %i responses",
                self._host,
                self._port,
                len(json_data),
                len(channels),
            )
            return

        for data, channel in zip(json_data, channels):
            self.map_channel_json_response([data], channel)

    def map_channel_json_response(self, json_data, channel: int):
        """Map the JSON objects to internal cache-objects."""
        response_channel = channel
        for data in json_data:
            try:
                if data["code"] == 1:  # -->Error, like "ability error"
                    _LOGGER.debug("Host %s:%s received response error code: %s", self._host, self._port, data)
                    continue

                if data["cmd"] == "GetEvents":
                    response_channel = data["value"]["channel"]
                    if response_channel != channel:
                        _LOGGER.error("Host %s:%s: GetEvents response channel %s does not equal requested channel %s", self._host, self._port, response_channel, channel)
                        continue
                    if "ai" in data["value"]:
                        self._ai_detection_states[channel] = {}
                        self._ai_detection_support[channel] = {}
                        for key, value in data["value"]["ai"].items():
                            supported: bool = value.get("support", 0) == 1
                            self._ai_detection_states[channel][key] = supported and value.get("alarm_state", 0) == 1
                            self._ai_detection_support[channel][key] = supported
                    if "md" in data["value"]:
                        self._motion_detection_states[channel] = data["value"]["md"]["alarm_state"] == 1
                    if "visitor" in data["value"]:
                        value = data["value"]["visitor"]
                        supported: bool = value.get("support", 0) == 1
                        self._visitor_states[channel] = supported and value.get("alarm_state", 0) == 1
                        self._is_doorbell_enabled[channel] = supported

                elif data["cmd"] == "GetMdState":
                    self._motion_detection_states[channel] = data["value"]["state"] == 1

                elif data["cmd"] == "GetAlarm":
                    self._alarm_settings[channel] = data["value"]
                    self._motion_detection_states[channel] = data["value"]["Alarm"]["enable"] == 1
                    self._sensitivity_presets[channel] = data["value"]["Alarm"]["sens"]

                elif data["cmd"] == "GetAiState":
                    self._ai_detection_states[channel] = {}
                    self._ai_detection_support[channel] = {}
                    response_channel = data["value"].get("channel", channel)
                    if response_channel != channel:
                        _LOGGER.error("Host %s:%s: GetAiState response channel %s does not equal requested channel %s", self._host, self._port, response_channel, channel)
                        continue

                    for key, value in data["value"].items():
                        if key == "channel":
                            continue

                        if isinstance(value, int):  # compatibility with firmware < 3.0.0-494
                            self._ai_detection_states[channel][key] = value == 1
                            self._ai_detection_support[channel][key] = True
                        else:
                            # from firmware 3.0.0.0-494 there is a new json structure:
                            # [
                            #     {
                            #         "cmd" : "GetAiState",
                            #         "code" : 0,
                            #         "value" : {
                            #             "channel" : 0,
                            #             "face" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 0
                            #             },
                            #             "people" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 1
                            #             },
                            #             "vehicle" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 1
                            #             }
                            #         }
                            #     }
                            # ]
                            supported: bool = value.get("support", 0) == 1
                            self._ai_detection_states[channel][key] = supported and value.get("alarm_state", 0) == 1
                            self._ai_detection_support[channel][key] = supported

                elif data["cmd"] == "GetOsd":
                    response_channel = data["value"]["Osd"]["channel"]
                    self._osd_settings[channel] = data["value"]
                    if not self._GetChannelStatus_present or not self._GetChannelStatus_has_name:
                        self._channel_names[channel] = data["value"]["Osd"]["osdChannel"]["name"]

                elif data["cmd"] == "GetFtp":
                    self._ftp_settings[channel] = data["value"]
                    self._ftp_enabled[channel] = data["value"]["Ftp"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetFtpV20":
                    self._ftp_settings[channel] = data["value"]
                    self._ftp_enabled[channel] = data["value"]["Ftp"]["enable"] == 1

                elif data["cmd"] == "GetPush":
                    self._push_settings[channel] = data["value"]
                    self._push_enabled[channel] = data["value"]["Push"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetPushV20":
                    self._push_settings[channel] = data["value"]
                    self._push_enabled[channel] = data["value"]["Push"]["enable"] == 1

                elif data["cmd"] == "GetEnc":
                    response_channel = data["value"]["Enc"]["channel"]
                    self._enc_settings[channel] = data["value"]
                    self._audio_enabled[channel] = data["value"]["Enc"]["audio"] == 1

                elif data["cmd"] == "GetRtspUrl":
                    response_channel = data["value"]["rtspUrl"]["channel"]
                    password = parse.quote(self._password)
                    mainStream = data["value"]["rtspUrl"]["mainStream"]
                    subStream = data["value"]["rtspUrl"]["subStream"]
                    self._rtsp_mainStream[channel] = mainStream.replace("rtsp://", f"rtsp://{self._username}:{password}@")
                    self._rtsp_subStream[channel] = subStream.replace("rtsp://", f"rtsp://{self._username}:{password}@")

                elif data["cmd"] == "GetEmail":
                    self._email_settings[channel] = data["value"]
                    self._email_enabled[channel] = data["value"]["Email"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetEmailV20":
                    self._email_settings[channel] = data["value"]
                    self._email_enabled[channel] = data["value"]["Email"]["enable"] == 1

                elif data["cmd"] == "GetIsp":
                    response_channel = data["value"]["Isp"]["channel"]
                    self._isp_settings[channel] = data["value"]
                    self._daynight_state[channel] = data["value"]["Isp"]["dayNight"]
                    self._backlight_state[channel] = data["value"]["Isp"]["backLight"]

                elif data["cmd"] == "GetIrLights":
                    self._ir_settings[channel] = data["value"]
                    self._ir_enabled[channel] = data["value"]["IrLights"]["state"] == "Auto"

                elif data["cmd"] == "GetPowerLed":
                    # GetPowerLed returns incorrect channel
                    # response_channel = data["value"]["PowerLed"]["channel"]
                    self._power_led_settings[channel] = data["value"]
                    val = data["value"]["PowerLed"].get("state", None)
                    if val is not None:
                        self._power_led_enabled[channel] = val == "On"
                    val = data["value"]["PowerLed"].get("eDoorbellLightState", None)
                    if val is not None and val != "NotSupport":
                        self._doorbell_light_enabled[channel] = val == "On"

                elif data["cmd"] == "GetWhiteLed":
                    response_channel = data["value"]["WhiteLed"]["channel"]
                    self._whiteled_settings[channel] = data["value"]
                    self._whiteled_enabled[channel] = data["value"]["WhiteLed"]["state"] == 1
                    self._whiteled_modes[channel] = data["value"]["WhiteLed"].get["mode"]

                elif data["cmd"] == "GetRec":
                    self._recording_settings[channel] = data["value"]
                    self._recording_enabled[channel] = data["value"]["Rec"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetRecV20":
                    self._recording_settings[channel] = data["value"]
                    self._recording_enabled[channel] = data["value"]["Rec"]["enable"] == 1

                elif data["cmd"] == "GetPtzPreset":
                    self._ptz_presets_settings[channel] = data["value"]
                    self._ptz_presets[channel] = {}
                    for preset in data["value"]["PtzPreset"]:
                        if int(preset["enable"]) == 1:
                            preset_name = preset["name"]
                            preset_id = int(preset["id"])
                            self._ptz_presets[channel][preset_name] = preset_id

                elif data["cmd"] == "GetAudioAlarm":
                    self._audio_alarm_settings[channel] = data["value"]
                    self._audio_alarm_enabled[channel] = data["value"]["Audio"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetAudioAlarmV20":
                    self._audio_alarm_settings[channel] = data["value"]
                    self._audio_alarm_enabled[channel] = data["value"]["Audio"]["enable"] == 1

                elif data["cmd"] == "GetAutoFocus":
                    self._auto_focus_settings[channel] = data["value"]

                elif data["cmd"] == "GetZoomFocus":
                    self._zoom_focus_settings[channel] = data["value"]

            except Exception as e:
                _LOGGER.error(
                    "Host %s:%s (channel %s) failed mapping JSON data: %s, traceback:\n%s\n",
                    self._host,
                    self._port,
                    channel,
                    e,
                    traceback.format_exc(),
                )
                continue
            if response_channel != channel:
                _LOGGER.error("Host %s:%s: command %s response channel %s does not equal requested channel %s", self._host, self._port, data["cmd"], response_channel, channel)

    async def set_net_port(
        self,
        enable_onvif: bool = None,
        enable_rtmp: bool = None,
        enable_rtsp: bool = None,
    ) -> None:
        """Set Network Port parameters on the host (NVR or camera)."""
        if self._netport_settings is None:
            await self.get_host_data()

        if self._netport_settings is None:
            raise NotSupportedError(f"set_net_port: failed to retrieve current NetPort settings from {self._host}:{self._port}")

        body = [{"cmd": "SetNetPort", "param": self._netport_settings}]

        if enable_onvif is not None:
            body[0]["param"]["NetPort"]["onvifEnable"] = 1 if enable_onvif else 0
        if enable_rtmp is not None:
            body[0]["param"]["NetPort"]["rtmpEnable"] = 1 if enable_rtmp else 0
        if enable_rtsp is not None:
            body[0]["param"]["NetPort"]["rtspEnable"] = 1 if enable_rtsp else 0

        await self.send_setting(body)
        self.expire_session()  # When changing network port settings, tokens are invalidated.

    async def set_time(self, dateFmt=None, hours24=None, tzOffset=None) -> None:
        """Set time on the host (NVR or camera).
        Arguments:
        dateFmt (string) Format of the date in the OSD timestamp
        hours24 (boolean) True selects 24h format, False selects 12h format
        tzoffset (int) Timezone offset versus UTC in seconds

        Always get current time first"""
        await self.get_host_data()
        if self._time_settings is None:
            raise NotSupportedError(f"set_time: failed to retrieve current time settings from {self._host}:{self._port}")

        body: list[dict] = [{"cmd": "SetTime", "action": 0, "param": self._time_settings}]

        if dateFmt is not None:
            if dateFmt in ["DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"]:
                body[0]["param"]["Time"]["timeFmt"] = dateFmt
            else:
                raise InvalidParameterError(f"set_time: date format {dateFmt} not in ['DD/MM/YYYY', 'MM/DD/YYYY', 'YYYY/MM/DD']")

        if hours24 is not None:
            if hours24:
                body[0]["param"]["Time"]["hourFmt"] = 0
            else:
                body[0]["param"]["Time"]["hourFmt"] = 1

        if tzOffset is not None:
            if not isinstance(tzOffset, int):
                raise InvalidParameterError(f"set_time: time zone offset {tzOffset} is not integer")
            if tzOffset < -43200 or tzOffset > 50400:
                raise InvalidParameterError(f"set_time: time zone offset {tzOffset} not in range -43200..50400")
            body[0]["param"]["Time"]["timeZone"] = tzOffset

        await self.send_setting(body)

    async def set_ntp(self, enable: bool = None, server: str = None, port: int = None, interval: int = None) -> None:
        """
        Set NTP parameters on the host (NVR or camera).
        Arguments:
        enable (boolean) Enable synchronization
        server (string) Name or IP-Address of time server (or pool)
        port (int) Port number in range of (1..65535)
        interval (int) Interval of synchronization in minutes in range of (60-65535)
        """
        if self._ntp_settings is None:
            await self.get_host_data()

        if self._ntp_settings is None:
            raise NotSupportedError(f"set_ntp: failed to retrieve current NTP settings from {self._host}:{self._port}")

        body: list[dict] = [{"cmd": "SetNtp", "action": 0, "param": self._ntp_settings}]

        if enable is not None:
            if enable:
                body[0]["param"]["Ntp"]["enable"] = 1
            else:
                body[0]["param"]["Ntp"]["enable"] = 0

        if server is not None:
            body[0]["param"]["Ntp"]["server"] = server

        if port is not None:
            if not isinstance(port, int):
                raise InvalidParameterError(f"set_ntp: Invalid NTP port {port} specified, type is not integer")
            if port < 1 or port > 65535:
                raise InvalidParameterError(f"set_ntp: Invalid NTP port {port} specified, out of valid range 1...65535")
            body[0]["param"]["Ntp"]["port"] = port

        if interval is not None:
            if not isinstance(interval, int):
                raise InvalidParameterError(f"set_ntp: Invalid NTP interval {interval} specified, type is not integer")
            if port < 60 or port > 65535:
                raise InvalidParameterError(f"set_ntp: Invalid NTP interval {interval} specified, out of valid range 60...65535")
            body[0]["param"]["Ntp"]["interval"] = interval

        await self.send_setting(body)

    async def sync_ntp(self) -> None:
        """Sync date and time on the host via NTP now."""
        if self._ntp_settings is None:
            await self.get_host_data()

        if self._ntp_settings is None:
            raise NotSupportedError(f"set_ntp: failed to retrieve current NTP settings from {self._host}:{self._port}")

        body: list[dict] = [{"cmd": "SetNtp", "action": 0, "param": self._ntp_settings}]
        body[0]["param"]["Ntp"]["interval"] = 0

        await self.send_setting(body)

    async def set_autofocus(self, channel: int, enable: bool) -> None:
        """Enable/Disable AutoFocus on a camera.
        Parameters:
        enable (boolean) enables/disables AutoFocus if supported"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_autofocus: no camera connected to channel '{channel}'")
        if self._auto_focus_settings is None or channel not in self._auto_focus_settings or not self._auto_focus_settings[channel]:
            raise NotSupportedError(f"set_autofocus: AutoFocus on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [
            {
                "cmd": "SetAutoFocus",
                "action": 0,
                "param": self._auto_focus_settings[channel],
            }
        ]
        body[0]["param"]["AutoFocus"]["disable"] = 0 if enable else 1

        await self.send_setting(body)

    def get_focus(self, channel: int) -> None:
        """Get absolute focus value."""
        if channel not in self._channels:
            raise InvalidParameterError(f"get_focus: no camera connected to channel '{channel}'")
        if self._zoom_focus_settings is None or channel not in self._zoom_focus_settings or not self._zoom_focus_settings[channel]:
            raise NotSupportedError(f"get_focus: ZoomFocus on camera {self.camera_name(channel)} is not available")

        return self._zoom_focus_settings[channel]["ZoomFocus"]["focus"]["pos"]

    async def set_focus(self, channel: int, focus) -> None:
        """Set absolute focus value.
        Parameters:
        focus (int) 0..223"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_focus: no camera connected to channel '{channel}'")
        if focus not in range(0, 223):
            raise InvalidParameterError(f"set_focus: focus value {focus} not in range 0..223")

        body = [
            {
                "cmd": "StartZoomFocus",
                "action": 0,
                "param": {"ZoomFocus": {"channel": channel, "op": "FocusPos", "pos": focus}},
            }
        ]

        await self.send_setting(body)

    def get_zoom(self, channel: int):
        """Get absolute zoom value."""
        if channel not in self._channels:
            raise InvalidParameterError(f"get_zoom: no camera connected to channel '{channel}'")
        if self._zoom_focus_settings is None or channel not in self._zoom_focus_settings or not self._zoom_focus_settings[channel]:
            raise NotSupportedError(f"get_zoom: ZoomFocus on camera {self.camera_name(channel)} is not available")

        return self._zoom_focus_settings[channel]["ZoomFocus"]["zoom"]["pos"]

    async def set_zoom(self, channel: int, zoom) -> None:
        """Set absolute zoom value.
        Parameters:
        zoom (int) 0..33"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_zoom: no camera connected to channel '{channel}'")
        if zoom not in range(0, 33):
            raise InvalidParameterError(f"set_zoom: zoom value {zoom} not in range 0..33")

        body = [
            {
                "cmd": "StartZoomFocus",
                "action": 0,
                "param": {"ZoomFocus": {"channel": channel, "op": "ZoomPos", "pos": zoom}},
            }
        ]

        await self.send_setting(body)

    def validate_osd_pos(self, pos) -> bool:
        """Helper function for validating an OSD position
        Returns True, if a valid position is specified"""
        return pos in [
            "Upper Left",
            "Upper Right",
            "Top Center",
            "Bottom Center",
            "Lower Left",
            "Lower Right",
        ]

    async def set_osd(self, channel: int, namePos=None, datePos=None, enableWaterMark=None) -> None:
        """Set OSD parameters.
        Parameters:
        namePos (string) specifies the position of the camera name - "Off" disables this OSD
        datePos (string) specifies the position of the date - "Off" disables this OSD
        enableWaterMark (boolean) enables/disables the Logo (WaterMark) if supported"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_osd: no camera connected to channel '{channel}'")
        if self._osd_settings is None or channel not in self._osd_settings or not self._osd_settings[channel]:
            raise NotSupportedError(f"set_osd: OSD on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [{"cmd": "SetOsd", "action": 0, "param": self._osd_settings[channel]}]

        if namePos is not None:
            if namePos == "Off":
                body[0]["param"]["Osd"]["osdChannel"]["enable"] = 0
            else:
                if not self.validate_osd_pos(namePos):
                    raise InvalidParameterError(f"set_osd: Invalid name OSD position specified '{namePos}'")
                body[0]["param"]["Osd"]["osdChannel"]["enable"] = 1
                body[0]["param"]["Osd"]["osdChannel"]["pos"] = namePos

        if datePos is not None:
            if datePos == "Off":
                body[0]["param"]["Osd"]["osdTime"]["enable"] = 0
            else:
                if not self.validate_osd_pos(datePos):
                    raise InvalidParameterError(f"set_osd: Invalid date OSD position specified '{datePos}'")
                body[0]["param"]["Osd"]["osdTime"]["enable"] = 1
                body[0]["param"]["Osd"]["osdTime"]["pos"] = datePos

        if enableWaterMark is not None:
            if "watermark" in body[0]["param"]["Osd"]:
                if enableWaterMark:
                    body[0]["param"]["Osd"]["watermark"] = 1
                else:
                    body[0]["param"]["Osd"]["watermark"] = 0
            else:
                _LOGGER.debug(
                    'Ignoring "enable watermark" request. Not supported by camera %s.',
                    self.camera_name(channel),
                )

        await self.send_setting(body)

    async def set_push(self, channel: Optional[int], enable: bool) -> None:
        """Set the PUSH-notifications parameter."""

        body: list[dict]
        if channel is None:
            if self._api_version_getpush == 0:
                for c in self._channels:
                    if self._push_settings is not None and c in self._push_settings and self._push_settings[c] is not None:
                        body = [
                            {
                                "cmd": "SetPush",
                                "action": 0,
                                "param": self._push_settings[c],
                            }
                        ]
                        body[0]["param"]["Push"]["schedule"]["enable"] = 1 if enable else 0
                        await self.send_setting(body)
                return

            body = [{"cmd": "SetPushV20", "action": 0, "param": self._push_settings[0]}]
            body[0]["param"]["Push"]["enable"] = 1 if enable else 0
            await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_push: no camera connected to channel '{channel}'")
        if self._push_settings is None or channel not in self._push_settings or not self._push_settings[channel]:
            raise NotSupportedError(f"set_push: push-notifications on camera {self.camera_name(channel)} are not available")

        if self._api_version_getpush == 0:
            body = [
                {
                    "cmd": "SetPush",
                    "action": 0,
                    "param": self._push_settings[channel],
                }
            ]
            body[0]["param"]["Push"]["schedule"]["enable"] = 1 if enable else 0
        else:
            body = [
                {
                    "cmd": "SetPushV20",
                    "action": 0,
                    "param": self._push_settings[channel],
                }
            ]
            body[0]["param"]["Push"]["enable"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_ftp(self, channel: Optional[int], enable: bool) -> None:
        """Set the FTP-notifications parameter."""

        body: list[dict]
        if channel is None:
            if self._api_version_getftp == 0:
                for c in self._channels:
                    if self._ftp_settings is not None and c in self._ftp_settings and self._ftp_settings[c] is not None:
                        body = [
                            {
                                "cmd": "SetFtp",
                                "action": 0,
                                "param": self._ftp_settings[c],
                            }
                        ]
                        body[0]["param"]["Ftp"]["schedule"]["enable"] = 1 if enable else 0
                        await self.send_setting(body)
                return

            body = [{"cmd": "SetFtpV20", "action": 0, "param": self._ftp_settings[0]}]
            body[0]["param"]["Ftp"]["enable"] = 1 if enable else 0
            await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_ftp: no camera connected to channel '{channel}'")
        if self._ftp_settings is None or channel not in self._ftp_settings or not self._ftp_settings[channel]:
            raise NotSupportedError(f"set_ftp: FTP on camera {self.camera_name(channel)} is not available")

        if self._api_version_getftp == 0:
            body = [{"cmd": "SetFtp", "action": 0, "param": self._ftp_settings[channel]}]
            body[0]["param"]["Ftp"]["schedule"]["enable"] = 1 if enable else 0
        else:
            body = [
                {
                    "cmd": "SetFtpV20",
                    "action": 0,
                    "param": self._ftp_settings[channel],
                }
            ]
            body[0]["param"]["Ftp"]["enable"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_email(self, channel: Optional[int], enable: bool) -> None:
        body: list[dict]
        if channel is None:
            if self._api_version_getemail == 0:
                for c in self._channels:
                    if self._email_settings is not None and c in self._email_settings and self._email_settings[c] is not None:
                        body = [
                            {
                                "cmd": "SetEmail",
                                "action": 0,
                                "param": self._email_settings[c],
                            }
                        ]
                        body[0]["param"]["Email"]["schedule"]["enable"] = 1 if enable else 0
                        await self.send_setting(body)
                return

            body = [
                {
                    "cmd": "SetEmailV20",
                    "action": 0,
                    "param": self._email_settings[0],
                }
            ]
            body[0]["param"]["Email"]["enable"] = 1 if enable else 0
            await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_email: no camera connected to channel '{channel}'")
        if self._email_settings is None or channel not in self._email_settings or not self._email_settings[channel]:
            raise NotSupportedError(f"set_email: Email on camera {self.camera_name(channel)} is not available")

        if self._api_version_getemail == 0:
            body = [
                {
                    "cmd": "SetEmail",
                    "action": 0,
                    "param": self._email_settings[channel],
                }
            ]
            body[0]["param"]["Email"]["schedule"]["enable"] = 1 if enable else 0
        else:
            body = [
                {
                    "cmd": "SetEmailV20",
                    "action": 0,
                    "param": self._email_settings[channel],
                }
            ]
            body[0]["param"]["Email"]["enable"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_audio(self, channel: int, enable: bool) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_audio: no camera connected to channel '{channel}'")
        if self._enc_settings is None or channel not in self._enc_settings or not self._enc_settings[channel]:
            raise NotSupportedError(f"set_audio: Audio on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [{"cmd": "SetEnc", "action": 0, "param": self._enc_settings[channel]}]
        body[0]["param"]["Enc"]["audio"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_ir_lights(self, channel: int, enable: bool) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_ir_lights: no camera connected to channel '{channel}'")
        if self._ir_settings is None or channel not in self._ir_settings or not self._ir_settings[channel]:
            raise NotSupportedError(f"set_ir_lights: IR light on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [
            {
                "cmd": "SetIrLights",
                "action": 0,
                "param": {"IrLights": {"channel": channel, "state": "dummy"}},
            }
        ]
        body[0]["param"]["IrLights"]["state"] = "Auto" if enable else "Off"

        await self.send_setting(body)

    async def set_power_led(self, channel: int, state: bool, doorbellLightState: bool) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_power_led: no camera connected to channel '{channel}'")
        if self._power_led_settings is None or channel not in self._power_led_settings or not self._power_led_settings[channel]:
            raise NotSupportedError(f"set_whiteled: Power led on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [
            {
                "cmd": "SetPowerLed",
                "action": 0,
                "param": {
                    "PowerLed": {
                        "channel": channel,
                        "state": "dummy",
                        "eDoorbellLightState": "dummy",
                    }
                },
            }
        ]
        body[0]["param"]["PowerLed"]["state"] = "On" if state else "Off"
        body[0]["param"]["PowerLed"]["eDoorbellLightState"] = "On" if doorbellLightState else "Off"

        await self.send_setting(body)

    async def set_whiteled(self, channel: int, enable: bool, brightness, mode=None) -> None:
        """
        Set the WhiteLed parameter.
        with Reolink Duo GetWhiteLed returns an error state
        SetWhiteLed appears to require 4 parameters
          state - two values 0/1 possibly OFF/ON
          channel - appears to default to 0
          mode - three values I think
            0  Night Mode Off
            1  Night Mode On , AUTO on
            3  Night Mode On, Set Time On
          brightness - brigtness level range 0 to 100

          TO BE CONFIRMED
          There may be an extra set of parameters with Duo - dont know with others
          LightingSchedule : { EndHour , EndMin, StartHour,StartMin  }
        """
        if channel not in self._channels:
            raise InvalidParameterError(f"set_whiteled: no camera connected to channel '{channel}'")
        if self._whiteled_settings is None or channel not in self._whiteled_settings or not self._whiteled_settings[channel]:
            raise NotSupportedError(f"set_whiteled: White Led on camera {self.camera_name(channel)} is not available")

        if mode is None:
            mode = 1
        if brightness < 0 or brightness > 100 or mode not in [0, 1, 3]:
            raise InvalidParameterError(f"set_whiteled: brightness {brightness} not in range 0..100 or mode {mode} not in [0,1,3]")

        body = [
            {
                "cmd": "SetWhiteLed",
                "param": {
                    "WhiteLed": {
                        "state": 1 if enable else 0,
                        "channel": channel,
                        "mode": mode,
                        "bright": brightness,
                    }
                },
            }
        ]

        await self.send_setting(body)

    async def set_spotlight_lighting_schedule(self, channel: int, endhour=6, endmin=0, starthour=18, startmin=0) -> None:
        """Stub to handle setting the time period where spotlight (WhiteLed) will be on when NightMode set and AUTO is off.
        Time in 24-hours format"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_spotlight_lighting_schedule: no camera connected to channel '{channel}'")
        if self._whiteled_settings is None or channel not in self._whiteled_settings or not self._whiteled_settings[channel]:
            raise NotSupportedError(f"set_spotlight_lighting_schedule: White Led on camera {self.camera_name(channel)} is not available")

        if (
            endhour < 0
            or endhour > 23
            or endmin < 0
            or endmin > 59
            or starthour < 0
            or starthour > 23
            or startmin < 0
            or startmin > 59
            or (endhour == starthour and endmin < startmin)
            or (not (endhour < 12 and starthour > 16) and (endhour < starthour))
        ):
            raise InvalidParameterError(
                f"set_spotlight_lighting_schedule: Parameter error on camera {self.camera_name(channel)} start time: {starthour}:{startmin}, end time: {endhour}:{endmin}"
            )

        body = [
            {
                "cmd": "SetWhiteLed",
                "param": {
                    "WhiteLed": {
                        "LightingSchedule": {
                            "EndHour": endhour,
                            "EndMin": endmin,
                            "StartHour": starthour,
                            "StartMin": startmin,
                        },
                        "channel": channel,
                        "mode": 3,
                    }
                },
            }
        ]

        await self.send_setting(body)

    async def set_spotlight(self, channel: int, enable: bool) -> None:
        """Simply calls set_whiteled with brightness 100, mode 3 after setting lightning schedule to on all the time 0000 to 2359."""
        if enable:
            await self.set_spotlight_lighting_schedule(channel, 23, 59, 0, 0)
            await self.set_whiteled(channel, enable, 100, 3)
            return

        await self.set_spotlight_lighting_schedule(channel, 0, 0, 0, 0)
        await self.set_whiteled(channel, enable, 100, 1)

    async def set_audio_alarm(self, channel: int, enable: bool) -> None:
        # fairly basic only either turns it off or on
        # called in its simple form by set_siren

        if channel not in self._channels:
            raise InvalidParameterError(f"set_audio_alarm: no camera connected to channel '{channel}'")
        if self._audio_alarm_settings is None or channel not in self._audio_alarm_settings or not self._audio_alarm_settings[channel]:
            raise NotSupportedError(f"set_audio_alarm: AudioAlarm on camera {self.camera_name(channel)} is not available")

        if self._api_version_getalarm == 0:
            body = [
                {
                    "cmd": "SetAudioAlarm",
                    "param": {
                        "Audio": {
                            "schedule": {
                                "enable": 1 if enable else 0,
                                "channel": channel,
                            }
                        }
                    },
                }
            ]
        else:
            body = [
                {
                    "cmd": "SetAudioAlarmV20",
                    "param": {"Audio": {"enable": 1 if enable else 0, "channel": channel}},
                }
            ]

        await self.send_setting(body)

    async def set_siren(self, channel: int, enable: bool) -> None:
        # Uses API AudioAlarmPlay with manual switch
        # uncertain if there may be a glitch - dont know if there is API I have yet to find
        # which sets AudioLevel
        if channel not in self._channels:
            raise InvalidParameterError(f"set_siren: no camera connected to channel '{channel}'")

        # This is overkill but to get state set right necessary to call set_audio_alarm.
        await self.set_audio_alarm(channel, enable)

        body = [
            {
                "cmd": "AudioAlarmPlay",
                "action": 0,
                "param": {
                    "alarm_mode": "manul",
                    "manual_switch": 1 if enable else 0,
                    "times": 2,
                    "channel": channel,
                },
            }
        ]

        await self.send_setting(body)

    async def set_daynight(self, channel: int, value: str) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_daynight: no camera connected to channel '{channel}'")
        if self._isp_settings is None or channel not in self._isp_settings or not self._isp_settings[channel]:
            raise NotSupportedError(f"set_daynight: ISP on camera {self.camera_name(channel)} is not available")

        if value not in ["Auto", "Color", "Black&White"]:
            raise InvalidParameterError(f"set_daynight: value {value} not in ['Auto', 'Color', 'Black&White']")

        body: list[dict] = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["dayNight"] = value

        await self.send_setting(body)

    async def set_backlight(self, channel: int, value: str) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_backlight: no camera connected to channel '{channel}'")
        if self._isp_settings is None or channel not in self._isp_settings or not self._isp_settings[channel]:
            raise NotSupportedError(f"set_backlight: ISP on camera {self.camera_name(channel)} is not available")

        if value not in ["BackLightControl", "DynamicRangeControl", "Off"]:
            raise InvalidParameterError(f"set_backlight: value {value} not in ['BackLightControl', 'DynamicRangeControl', 'Off']")

        body: list[dict] = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["backLight"] = value

        await self.send_setting(body)

    async def set_recording(self, channel: Optional[int], enable: bool) -> None:
        """Set the recording parameter."""

        body: list[dict]
        if channel is None:
            if self._api_version_getrec == 0:
                for c in self._channels:
                    if self._recording_settings is not None and c in self._recording_settings and self._recording_settings[c] is not None:
                        body = [
                            {
                                "cmd": "SetRec",
                                "action": 0,
                                "param": self._recording_settings[c],
                            }
                        ]
                        body[0]["param"]["Rec"]["schedule"]["enable"] = 1 if enable else 0
                        await self.send_setting(body)
                return

            body = [
                {
                    "cmd": "SetRecV20",
                    "action": 0,
                    "param": self._recording_settings[0],
                }
            ]
            body[0]["param"]["Rec"]["enable"] = 1 if enable else 0
            await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_recording: no camera connected to channel '{channel}'")
        if self._recording_settings is None or channel not in self._recording_settings or not self._recording_settings[channel]:
            raise NotSupportedError(f"set_recording: recording on camera {self.camera_name(channel)} is not available")

        if self._api_version_getrec == 0:
            body = [
                {
                    "cmd": "SetRec",
                    "action": 0,
                    "param": self._recording_settings[channel],
                }
            ]
            body[0]["param"]["Rec"]["schedule"]["enable"] = 1 if enable else 0
        else:
            body = [
                {
                    "cmd": "SetRecV20",
                    "action": 0,
                    "param": self._recording_settings[channel],
                }
            ]
            body[0]["param"]["Rec"]["enable"] = 1 if enable else 0

            await self.send_setting(body)

    async def set_motion_detection(self, channel: int, enable: bool) -> None:
        """Set the motion detection parameter."""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_motion_detection: no camera connected to channel '{channel}'")
        if self._alarm_settings is None or channel not in self._alarm_settings or not self._alarm_settings[channel]:
            raise NotSupportedError(f"set_motion_detection: alarm on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [{"cmd": "SetAlarm", "action": 0, "param": self._alarm_settings[channel]}]
        body[0]["param"]["Alarm"]["enable"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_sensitivity(self, channel: int, value: int, preset=None) -> None:
        """Set motion detection sensitivity.
        Here the camera web and windows application show a completely different value than set.
        So the calculation <51 - value> makes the "real" value.
        """
        if channel not in self._channels:
            raise InvalidParameterError(f"set_sensitivity: no camera connected to channel '{channel}'")
        if self._alarm_settings is None or channel not in self._alarm_settings or not self._alarm_settings[channel]:
            raise NotSupportedError(f"set_sensitivity: alarm on camera {self.camera_name(channel)} is not available")

        body: list[dict] = [
            {
                "cmd": "SetAlarm",
                "action": 0,
                "param": {
                    "Alarm": {
                        "channel": channel,
                        "type": "md",
                        "sens": self._alarm_settings[channel]["Alarm"]["sens"],
                    }
                },
            }
        ]
        for setting in body[0]["param"]["Alarm"]["sens"]:
            if preset is None or preset == setting["id"]:
                setting["sensitivity"] = int(51 - value)

        await self.send_setting(body)

    async def set_ptz_command(self, channel: int, command, preset=None, speed=None) -> None:
        """Send PTZ command to the camera.

        List of possible commands
        --------------------------
        Command     Speed   Preset
        --------------------------
        Right       X
        RightUp     X
        RightDown   X
        Left        X
        LeftUp      X
        LeftDown    X
        Up          X
        Down        X
        ZoomInc     X
        ZoomDec     X
        FocusInc    X
        FocusDec    X
        ToPos       X       X
        Auto
        Stop
        """

        if channel not in self._channels:
            raise InvalidParameterError(f"set_ptz_command: no camera connected to channel '{channel}'")
        body: list[dict] = [
            {
                "cmd": "PtzCtrl",
                "action": 0,
                "param": {"channel": channel, "op": command},
            }
        ]

        if speed:
            body[0]["param"]["speed"] = speed
        if preset:
            body[0]["param"]["id"] = preset

        await self.send_setting(body)

    async def request_vod_files(
        self,
        channel: int,
        start: datetime,
        end: datetime,
        status_only: bool = False,
        stream: Optional[str] = None,
    ) -> tuple[list[typings.SearchStatus], Optional[list[typings.SearchFile]]]:
        """Send search VOD-files command."""
        if channel not in self._channels:
            return None, None

        if stream is None:
            stream = self._stream

        body = [
            {
                "cmd": "Search",
                "action": 0,
                "param": {
                    "Search": {
                        "channel": channel,
                        "onlyStatus": 1 if status_only else 0,
                        "streamType": stream,
                        "StartTime": {
                            "year": start.year,
                            "mon": start.month,
                            "day": start.day,
                            "hour": start.hour,
                            "min": start.minute,
                            "sec": start.second,
                        },
                        "EndTime": {
                            "year": end.year,
                            "mon": end.month,
                            "day": end.day,
                            "hour": end.hour,
                            "min": end.minute,
                            "sec": end.second,
                        },
                    }
                },
            }
        ]

        try:
            # json_data = await self.send(body, {"cmd": "Search", "rs": "000000"}, expected_content_type = 'json')
            json_data = await self.send(body, {"cmd": "Search"}, expected_content_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                'Host %s:%s: error translating of "Search" command response.',
                self._host,
                self._port,
            )
            return None, None
        if json_data is None:
            _LOGGER.error(
                'Host %s:%s: error receiving response for "Search" command.',
                self._host,
                self._port,
            )
            return None, None

        try:
            if json_data[0]["code"] == 0:
                search_result = json_data[0]["value"]["SearchResult"]
                if status_only or "File" not in search_result:
                    if "Status" in search_result:
                        return search_result["Status"], None
                    _LOGGER.info(
                        'Host: %s:%s: no "Status" in the result of "Search" command:\n%s\n',
                        self._host,
                        self._port,
                        json_data,
                    )
                else:
                    return search_result["Status"], search_result["File"]
            else:
                _LOGGER.info(
                    'Host: %s:%s: the "Search" command returned error code %s:\n%s\n',
                    self._host,
                    self._port,
                    json_data[0]["code"],
                    json_data,
                )
        except KeyError as e:
            _LOGGER.error(
                'Host %s:%s: received an unexpected response from "Search" command: %s',
                self._host,
                self._port,
                e,
            )

        return None, None

    async def send_setting(self, body: list[dict[str, Any]]) -> None:
        command = body[0]["cmd"]
        _LOGGER.debug(
            'Sending command: "%s" to: %s:%s with body: %s',
            command,
            self._host,
            self._port,
            body,
        )

        try:
            json_data = await self.send(body, {"cmd": command}, expected_content_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Command '{command}': {str(err)}") from err
        if json_data is None:
            raise NoDataError(f"Host: {self._host}:{self._port}: error receiving response for command '{command}'")

        _LOGGER.debug("Response from cmd '%s' from %s:%s: %s", command, self._host, self._port, json_data)

        try:
            if json_data[0]["code"] != 0 or json_data[0]["value"]["rspCode"] != 200:
                _LOGGER.debug("ApiError for command '%s', response: %s", command, json_data)
                raise ApiError(
                    f"cmd '{command}': API returned error code {json_data[0]['code']}, "
                    f"response code {json_data[0]['value']['rspCode']}/{json_data[0]['value'].get('detail', '')}"
                )
        except KeyError as err:
            raise UnexpectedDataError(f"Host {self._host}:{self._port}: received an unexpected response from command '{command}': {json_data}") from err

        if command[:3] == "Set":
            getcmd = command.replace("Set", "Get")
            await self.get_state(cmd=getcmd)

    async def send(
        self,
        body: Optional[list],
        param: Optional[dict] = None,
        expected_content_type: Optional[str] = None,
        retry: bool = False,
    ) -> Optional[list]:
        """Generic send method."""

        if self._aiohttp_session is not None and self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession(timeout=self._timeout, connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT))

        if body is None:
            cur_command = "" if param is None else param.get("cmd", "")
            is_login_logout = False
        else:
            cur_command = "" if len(body) == 0 else body[0].get("cmd", "")
            is_login_logout = cur_command in ["Login", "Logout"]

        if not is_login_logout:
            if not await self.login():
                return None

        if not param:
            param = {}
        if cur_command == "Login":
            param["token"] = "null"
        elif self._token is not None:
            param["token"] = self._token

        try:
            session = self._aiohttp_session
            if session is None:
                session = self._aiohttp_get_session_callback()

            _LOGGER.debug("%s/%s:%s::send() HTTP Request params =\n%s\n", self.nvr_name, self._host, self._port, str(param).replace(self._password, "<password>"))

            if body is None:
                async with self._send_mutex:
                    response = await session.get(url=self._url, params=param, allow_redirects=False)

                data = await response.read()
            else:
                _LOGGER.debug("%s/%s:%s::send() HTTP Request body =\n%s\n", self.nvr_name, self._host, self._port, str(body).replace(self._password, "<password>"))

                async with self._send_mutex:
                    response = await session.post(url=self._url, json=body, params=param, allow_redirects=False)

                data = await response.text()

            _LOGGER.debug("%s/%s:%s::send() HTTP Response status = %s, content-type = (%s).", self.nvr_name, self._host, self._port, response.status, response.content_type)
            if cur_command == "Search" and len(data) > 500:
                _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response (VOD search) data scrapped because it's too large.", self.nvr_name, self._host, self._port)
            elif cur_command == "Snap":
                _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response (snapshot) data scrapped because it's too large.", self.nvr_name, self._host, self._port)
            else:
                _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response data:\n%s\n", self.nvr_name, self._host, self._port, data)

            if len(data) < 500 and response.content_type == "text/html":
                if body is None:
                    login_err = b'"detail" : "invalid user"' in data or b'"detail" : "login failed"' in data or b'detail" : "please login first' in data
                else:
                    login_err = (
                        '"detail" : "invalid user"' in data or '"detail" : "login failed"' in data or 'detail" : "please login first' in data
                    ) and cur_command != "Logout"
                if login_err:
                    if is_login_logout:
                        raise CredentialsInvalidError()

                    if retry:
                        raise CredentialsInvalidError()
                    _LOGGER.debug(
                        'Host %s:%s: "invalid login" response, trying to login again and retry the command.',
                        self._host,
                        self._port,
                    )
                    self.expire_session()
                    return await self.send(body, param, expected_content_type, True)

            if expected_content_type not in [None, "json"] and response.content_type != expected_content_type:
                raise InvalidContentTypeError(f"Expected type '{expected_content_type}' but received '{response.content_type}'.")

            if response.status == 502 and not retry:
                _LOGGER.debug("Host %s:%s: 502/Bad Gateway response, trying to login again and retry the command.", self._host, self._port)
                self.expire_session()
                return await self.send(body, param, expected_content_type, True)

            if response.status >= 400 or (is_login_logout and response.status != 200):
                raise ApiError(f"API returned HTTP status ERROR code {response.status}/{response.reason}")

            if expected_content_type == "json":
                try:
                    json_data = json.loads(data)
                except (TypeError, json.JSONDecodeError) as err:
                    if not retry:
                        _LOGGER.debug("Error translating JSON response: %s, data:\n%s\n", err, response)
                        self.expire_session()
                        return await self.send(body, param, expected_content_type, True)
                    raise InvalidContentTypeError(f"Error translating JSON response: {str(err)},  content type '{response.content_type}', data:\n{data}\n") from err
                if json_data is None:
                    self.expire_session()
                return json_data

            return data
        except aiohttp.ClientConnectorError as err:
            self.expire_session()
            _LOGGER.error("Host %s:%s: connection error: %s", self._host, self._port, str(err))
            raise err
        except asyncio.TimeoutError as err:
            self.expire_session()
            _LOGGER.error(
                "Host %s:%s: connection timeout exception. Please check the connection to this host.",
                self._host,
                self._port,
            )
            raise err
        except ApiError as err:
            self.expire_session()
            _LOGGER.error("Host %s:%s: API error: %s.", self._host, self._port, str(err))
            raise err
        except CredentialsInvalidError as err:
            self.expire_session()
            _LOGGER.error("Host %s:%s: login attempt failed.", self._host, self._port)
            raise err
        except InvalidContentTypeError as err:
            self.expire_session()
            _LOGGER.debug("Host %s:%s: content type error: %s.", self._host, self._port, str(err))
            raise err
        except Exception as err:
            self.expire_session()
            _LOGGER.error('Host %s:%s: unknown exception "%s" occurred, traceback:\n%s\n', self._host, self._port, str(err), traceback.format_exc())
            raise err

    ##############################################################################
    # SUBSCRIPTION managing
    @property
    def renewtimer(self) -> int:
        """Return the renew time in seconds. Negative if expired."""
        if self._subscription_time_difference is None or self._subscription_termination_time is None:
            return -1

        diff = self._subscription_termination_time - datetime.utcnow()
        diff_s = int(diff.total_seconds())
        _LOGGER.debug("Host %s:%s should renew in: %i seconds...", self._host, self._port, diff_s)

        return diff_s

    @property
    def subscribed(self) -> bool:
        return self._subscription_manager_url is not None and self.renewtimer > 0

    async def convert_time(self, time) -> Optional[datetime]:
        """Convert time object to printable."""
        try:
            return datetime.strptime(time, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None

    async def calc_time_difference(self, local_time, remote_time) -> float:
        """Calculate the time difference between local and remote."""
        return remote_time.timestamp() - local_time.timestamp()

    async def get_digest(self) -> dict:
        """Get the authorisation digest."""
        time_created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

        raw_nonce = uuid.uuid4().bytes
        nonce = base64.b64encode(raw_nonce)

        sha1 = hashlib.sha1()
        sha1.update(raw_nonce + time_created.encode("utf8") + self._password.encode("utf8"))
        raw_digest = sha1.digest()
        digest_pwd = base64.b64encode(raw_digest)

        return {
            "UsernameToken": str(uuid.uuid4()),
            "Username": self._username,
            "PasswordDigest": digest_pwd.decode("utf8"),
            "Nonce": nonce.decode("utf8"),
            "Created": time_created,
        }

    async def subscription_send(self, headers, data, logger=True) -> Optional[str]:
        """Send subscription data to the camera."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout, connector=aiohttp.TCPConnector(verify_ssl=False)) as session:
                _LOGGER.debug(
                    "Host %s:%s: subscription request data:\n%s\n",
                    self._host,
                    self._port,
                    data,
                )

                async with self._send_mutex:
                    response = await session.post(
                        url=self._subscribe_url,
                        data=data,
                        headers=headers,
                        allow_redirects=False,
                    )

                response_text = await response.text()
                _LOGGER.debug(
                    "Host %s:%s: subscription got response status: %s. Payload:\n%s\n",
                    self._host,
                    self._port,
                    response.status,
                    response_text,
                )

                if response.status != 200:
                    if logger:
                        _LOGGER.warning(
                            "Host %s:%s: subscription request got a response with wrong HTTP status %s: %s",
                            self._host,
                            self._port,
                            response.status,
                            response.reason,
                        )
                    else:
                        _LOGGER.debug(
                            "Host %s:%s: unsubscribe request for unsubscribing all got a response with wrong HTTP status %s: %s, this is expected.",
                            self._host,
                            self._port,
                            response.status,
                            response.reason,
                        )
                    return

                return response_text

        except aiohttp.ClientConnectorError as e:
            _LOGGER.error("Host %s:%s: connection error: %s.", self._host, self._port, str(e))
        except asyncio.TimeoutError:
            _LOGGER.error("Host %s:%s: connection timeout exception.", self._host, self._port)

    async def subscribe(self, webhook_url: str, retry: bool = False) -> bool:
        """Subscribe to ONVIF events."""
        headers = templates.HEADERS
        headers.update(templates.SUBSCRIBE_ACTION)
        template = templates.SUBSCRIBE_XML

        parameters = {
            "Address": webhook_url,
            "InitialTerminationTime": f"PT{SUBSCRIPTION_TERMINATION_TIME}M",
        }

        parameters.update(await self.get_digest())
        local_time = datetime.utcnow()

        xml = template.format(**parameters)

        response = await self.subscription_send(headers, xml)
        if response is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error("Host %s:%s: failed to subscribe, None response.", self._host, self._port)
            return False
        root = XML.fromstring(response)

        address_element = root.find(".//{http://www.w3.org/2005/08/addressing}Address")
        if address_element is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        self._subscription_manager_url = address_element.text

        if self._subscription_manager_url is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error(
                "Host %s:%s: failed to subscribe. Required response parameters not available.",
                self._host,
                self._port,
            )
            return False

        current_time_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}CurrentTime")
        if current_time_element is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        remote_time = await self.convert_time(current_time_element.text)

        if remote_time is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error(
                "Host %s:%s: failed to subscribe. Required response parameters not available.",
                self._host,
                self._port,
            )
            return False

        self._subscription_time_difference = await self.calc_time_difference(local_time, remote_time)

        termination_time_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}TerminationTime")
        if termination_time_element is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        self._subscription_termination_time = await self.convert_time(termination_time_element.text) - timedelta(seconds=self._subscription_time_difference)

        if self._subscription_termination_time is None:
            if not retry:
                await self.unsubscribe_all()
                return await self.subscribe(webhook_url, retry=True)
            _LOGGER.error(
                "Host %s:%s: failed to subscribe. Required response parameters not available.",
                self._host,
                self._port,
            )
            return False

        _LOGGER.debug(
            "Local time: %s, camera time: %s (difference: %s), termination time: %s",
            local_time.strftime("%Y-%m-%d %H:%M"),
            remote_time.strftime("%Y-%m-%d %H:%M"),
            self._subscription_time_difference,
            self._subscription_termination_time.strftime("%Y-%m-%d %H:%M"),
        )

        return True

    async def renew(self) -> bool:
        """Renew the ONVIF event subscription."""

        if not self.subscribed:
            _LOGGER.error("Host %s:%s: failed to renew, not previously subscribed.", self._host, self._port)
            return False

        headers = templates.HEADERS
        headers.update(templates.RENEW_ACTION)
        template = templates.RENEW_XML

        parameters = {
            "To": self._subscription_manager_url,
            "TerminationTime": f"PT{SUBSCRIPTION_TERMINATION_TIME}M",
        }

        parameters.update(await self.get_digest())
        local_time = datetime.utcnow()

        xml = template.format(**parameters)

        response = await self.subscription_send(headers, xml)
        if response is None:
            await self.unsubscribe_all()
            return False
        root = XML.fromstring(response)

        current_time_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}CurrentTime")
        if current_time_element is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        remote_time = await self.convert_time(current_time_element.text)

        if remote_time is None:
            await self.unsubscribe_all()
            _LOGGER.error(
                "Host %s:%s: failed to renew subscription. Unexpected response.",
                self._host,
                self._port,
            )
            return False

        self._subscription_time_difference = await self.calc_time_difference(local_time, remote_time)

        # The Reolink renew functionality has a bug: it always returns the INITIAL TerminationTime.
        # By adding the duration to the CurrentTime parameter, the new termination time can be calculated.
        # This will not work before the Reolink bug gets fixed on all devices
        # termination_time_element = root.find('.//{http://docs.oasis-open.org/wsn/b-2}TerminationTime')
        # if termination_time_element is None:
        #     await self.unsubscribe_all()
        #     _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
        #     return False
        # self._subscription_termination_time = await self.convert_time(termination_time_element.text) - timedelta(seconds = self._subscription_time_difference)
        self._subscription_termination_time = local_time + timedelta(minutes=SUBSCRIPTION_TERMINATION_TIME)

        if self._subscription_termination_time is None:
            await self.unsubscribe_all()
            _LOGGER.error(
                "Host %s:%s: failed to renew subscription. Unexpected response.",
                self._host,
                self._port,
            )
            return False

        _LOGGER.debug(
            "Local time: %s, camera time: %s (difference: %s), termination time: %s",
            local_time.strftime("%Y-%m-%d %H:%M"),
            remote_time.strftime("%Y-%m-%d %H:%M"),
            self._subscription_time_difference,
            self._subscription_termination_time.strftime("%Y-%m-%d %H:%M"),
        )

        return True

    async def unsubscribe(self):
        """Unsubscribe from ONVIF events."""
        if self._subscription_manager_url is not None:
            headers = templates.HEADERS
            headers.update(templates.UNSUBSCRIBE_ACTION)
            template = templates.UNSUBSCRIBE_XML

            parameters = {"To": self._subscription_manager_url}
            parameters.update(await self.get_digest())

            xml = template.format(**parameters)

            await self.subscription_send(headers, xml)

            self._subscription_manager_url = None

        self._subscription_termination_time = None
        self._subscription_time_difference = None

        return True

    async def unsubscribe_all(self):
        """Unsubscribe from ONVIF events. Normally only needed during entry initialization/setup, to free possibly dangling subscriptions."""
        headers = templates.HEADERS
        headers.update(templates.UNSUBSCRIBE_ACTION)
        template = templates.UNSUBSCRIBE_XML

        await self.unsubscribe()

        if self._nvr_model in ["RLN8-410", "RLN16-410"]:
            _LOGGER.debug("Attempting to unsubscribe previous (dead) sessions notifications...")

            # These work for RLN8-410 NVR, so up to 3 maximum subscriptions on it
            parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_0"}
            parameters.update(await self.get_digest())
            xml = template.format(**parameters)
            await self.subscription_send(headers, xml, logger=False)

            parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_1"}
            parameters.update(await self.get_digest())
            xml = template.format(**parameters)
            await self.subscription_send(headers, xml, logger=False)

            parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_2"}
            parameters.update(await self.get_digest())
            xml = template.format(**parameters)
            await self.subscription_send(headers, xml, logger=False)

        return True

    async def ONVIF_event_callback(self, data: str) -> int | None:
        """Handle incoming ONVIF event from the webhook called by the Reolink device."""
        _LOGGER_DATA.debug("ONVIF event callback received payload:\n%s", data)

        found_event = False

        root = XML.fromstring(data)
        for message in root.iter("{http://docs.oasis-open.org/wsn/b-2}NotificationMessage"):
            channel = None

            # find NotificationMessage Rule (type of event)
            topic_element = message.find("{http://docs.oasis-open.org/wsn/b-2}Topic[@Dialect='http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet']")
            if topic_element is None:
                continue
            rule = basename(topic_element.text)
            if not rule:
                continue

            # find camera channel
            if self.num_cameras == 1:
                channel = self.channels[0]
            else:
                source_element = message.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='Source']")
                if source_element is None:
                    source_element = message.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='VideoSourceConfigurationToken']")
                if source_element is not None and "Value" in source_element.attrib:
                    try:
                        channel = int(source_element.attrib["Value"])
                    except ValueError:
                        if f"ONVIF_{rule}_invalid_channel" not in self._log_once:
                            self._log_once.append(f"ONVIF_{rule}_invalid_channel")
                            _LOGGER.warning("Reolink ONVIF event '%s' data contained invalid channel '%s', issuing poll instead", rule, source_element.attrib["Value"])

            if channel is None:
                # Unknown which channel caused the event, poll all channels
                if f"ONVIF_{rule}_no_channel" not in self._log_once:
                    self._log_once.append(f"ONVIF_{rule}_no_channel")
                    _LOGGER.warning("Reolink ONVIF event '%s' does not contain channel", rule)
                if not await self.get_motion_state_all_ch():
                    _LOGGER.error("Could not poll motion state after receiving ONVIF event with unknown channel")
                return None

            if channel not in self.channels:
                # Channel has no camera connected, ignoring this notification
                continue

            key = "State"
            if rule == "Motion":
                key = "IsMotion"
            data_element = message.find(f".//\u007bhttp://www.onvif.org/ver10/schema\u007dSimpleItem[@Name='{key}']")
            if data_element is None or "Value" not in data_element.attrib:
                if f"ONVIF_{rule}_no_data" not in self._log_once:
                    self._log_once.append(f"ONVIF_{rule}_no_data")
                    _LOGGER.warning("ONVIF event '%s' did not contain data:\n%s", rule, data)
                continue

            if rule not in ["Motion", "MotionAlarm", "FaceDetect", "PeopleDetect", "VehicleDetect", "DogCatDetect", "Visitor"]:
                if f"ONVIF_unknown_{rule}" not in self._log_once:
                    self._log_once.append(f"ONVIF_unknown_{rule}")
                    _LOGGER.warning("ONVIF event with unknown rule: '%s'", rule)
                continue

            found_event = True
            if rule in ["FaceDetect", "PeopleDetect", "VehicleDetect", "DogCatDetect", "Visitor"]:
                self._onvif_only_motion = False

            state = data_element.attrib["Value"] == "true"
            _LOGGER.info("Reolink ONVIF event %s: %s", rule, state)

            if rule == "Motion":
                self._motion_detection_states[channel] = state
            elif rule == "MotionAlarm":
                self._motion_detection_states[channel] = state
            elif rule == "FaceDetect":
                self._ai_detection_states[channel]["face"] = state
            elif rule == "PeopleDetect":
                self._ai_detection_states[channel]["people"] = state
            elif rule == "VehicleDetect":
                self._ai_detection_states[channel]["vehicle"] = state
            elif rule == "DogCatDetect":
                self._ai_detection_states[channel]["dog_cat"] = state
            elif rule == "Visitor":
                self._visitor_states[channel] = state

        if not found_event:
            # ONVIF notification withouth known events
            if "ONVIF_no_known" not in self._log_once:
                self._log_once.append("ONVIF_no_known")
                _LOGGER.warning("Reolink ONVIF notification received withouth any known events:\n%s", data)
            if not await self.get_motion_state_all_ch():
                _LOGGER.error("Could not poll motion state after receiving ONVIF event without any known events")
            return None

        if self._onvif_only_motion:
            # Poll all other states since not all cameras have rich notifications including the specific events
            if "ONVIF_only_motion" not in self._log_once:
                self._log_once.append("ONVIF_only_motion")
                _LOGGER.debug("Reolink model '%s' appears to not support rich notifications", self.model)
            if not await self.get_motion_state_all_ch():
                _LOGGER.error("Could not poll event state after receiving ONVIF event with only motion event")
            return None

        return channel
