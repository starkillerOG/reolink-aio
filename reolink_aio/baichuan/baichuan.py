""" Reolink Baichuan API """

import logging
import asyncio
from xml.etree import ElementTree as XML
from Cryptodome.Cipher import AES

from . import xmls
from .tcp_protocol import BaichuanTcpClientProtocol
from ..exceptions import (
    InvalidContentTypeError,
    InvalidParameterError,
    ReolinkError,
    UnexpectedDataError,
    ReolinkConnectionError,
    ReolinkTimeoutError,
)
from .util import BC_PORT, HEADER_MAGIC, AES_IV, EncType, PortType, decrypt_baichuan, encrypt_baichuan, md5_str_modern

_LOGGER = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3


class Baichuan:
    """Reolink Baichuan API class."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = BC_PORT,
    ) -> None:
        self._host: str = host
        self._port: int = port
        self._username: str = username
        self._password: str = password
        self._nonce: str | None = None
        self._user_hash: str | None = None
        self._password_hash: str | None = None
        self._aes_key: bytes | None = None

        # TCP connection
        self._mutex = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._transport: asyncio.Transport | None = None
        self._protocol: BaichuanTcpClientProtocol | None = None
        self._logged_in: bool = False

        # states
        self._ports: dict[str, dict[str, int | bool]] = {}
        self._dev_info: dict[str, str] = {}

    async def send(
        self, cmd_id: int, body: str = "", extension: str = "", enc_type: EncType = EncType.AES, message_class: str = "1464", enc_offset: int = 0, retry: int = RETRY_ATTEMPTS
    ) -> str:
        """Generic baichuan send method."""
        retry = retry - 1

        if not self._logged_in and cmd_id > 2:
            # not logged in and requesting a non login/logout cmd, first login
            await self.login()

        mess_len = len(extension) + len(body)
        payload_offset = len(extension)

        cmd_id_bytes = (cmd_id).to_bytes(4, byteorder="little")
        mess_len_bytes = (mess_len).to_bytes(4, byteorder="little")
        enc_offset_bytes = (enc_offset).to_bytes(4, byteorder="little")
        payload_offset_bytes = (payload_offset).to_bytes(4, byteorder="little")

        if message_class == "1465":
            encrypt = "12dc"
            header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + mess_len_bytes + enc_offset_bytes + bytes.fromhex(encrypt + message_class)
        elif message_class == "1464":
            status_code = "0000"
            header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + mess_len_bytes + enc_offset_bytes + bytes.fromhex(status_code + message_class) + payload_offset_bytes
        else:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param message_class '{message_class}'")

        enc_body_bytes = b""
        if mess_len > 0:
            if enc_type == EncType.BC:
                enc_body_bytes = encrypt_baichuan(extension + body, enc_offset)
            elif enc_type == EncType.AES:
                enc_body_bytes = self._aes_encrypt(extension + body)
            else:
                raise InvalidParameterError(f"Baichuan host {self._host}: invalid param enc_type '{enc_type}'")

        # send message
        async with self._mutex:
            if self._transport is None or self._protocol is None or self._transport.is_closing():
                try:
                    async with asyncio.timeout(15):
                        self._transport, self._protocol = await self._loop.create_connection(
                            lambda: BaichuanTcpClientProtocol(self._loop, self._host, self._push_callback), self._host, self._port
                        )
                except asyncio.TimeoutError as err:
                    raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error") from err
                except (ConnectionResetError, OSError) as err:
                    raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error: {str(err)}") from err

            if _LOGGER.isEnabledFor(logging.DEBUG):
                if mess_len > 0:
                    _LOGGER.debug("Baichuan host %s: writing cmd_id %s, body:\n%s", self._host, cmd_id, self._hide_password(extension + body))
                else:
                    _LOGGER.debug("Baichuan host %s: writing cmd_id %s, without body", self._host, cmd_id)

            if self._protocol.expected_cmd_id is not None or self._protocol.receive_future is not None:
                raise ReolinkError(f"Baichuan host {self._host}: receive future is already set, cannot receive multiple requests simultaneously")

            self._protocol.expected_cmd_id = cmd_id
            self._protocol.receive_future = self._loop.create_future()

            try:
                async with asyncio.timeout(15):
                    self._transport.write(header + enc_body_bytes)
                    data, len_header = await self._protocol.receive_future
            except asyncio.TimeoutError as err:
                raise ReolinkTimeoutError(f"Baichuan host {self._host}: Timeout error") from err
            except (ConnectionResetError, OSError) as err:
                if retry <= 0 or cmd_id == 2:
                    raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error during read/write: {str(err)}") from err
                _LOGGER.debug("Baichuan host %s: Connection error during read/write: %s, trying again", self._host, str(err))
                return await self.send(cmd_id, body, extension, enc_type, message_class, enc_offset, retry)
            finally:
                self._protocol.expected_cmd_id = None
                self._protocol.receive_future.cancel()
                self._protocol.receive_future = None

        # decryption
        rec_body = self._decrypt(data, len_header, enc_type)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            if len(rec_body) > 0:
                _LOGGER.debug("Baichuan host %s: received:\n%s", self._host, self._hide_password(rec_body))
            else:
                _LOGGER.debug("Baichuan host %s: received status 200:OK without body", self._host)

        return rec_body

    def _aes_encrypt(self, body: str) -> bytes:
        """Encrypt a message using AES encryption"""
        if self._aes_key is None:
            raise InvalidParameterError("Baichuan host {self._host}: first login before using AES encryption")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.encrypt(body.encode("utf8"))

    def _aes_decrypt(self, data: bytes) -> str:
        """Decrypt a message using AES decryption"""
        if self._aes_key is None:
            raise InvalidParameterError("Baichuan host {self._host}: first login before using AES decryption")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.decrypt(data).decode("utf8")

    def _decrypt(self, data: bytes, len_header: int, enc_type: EncType = EncType.AES) -> str:
        """Figure out the encryption method and decrypt the message"""
        rec_enc_offset = int.from_bytes(data[12:16], byteorder="little")
        rec_enc_type = data[16:18].hex()
        enc_body = data[len_header::]

        # decryption
        if (len_header == 20 and rec_enc_type in ["01dd", "12dd"]) or (len_header == 24 and enc_type == EncType.BC):
            # Baichuan Encryption
            rec_body = decrypt_baichuan(enc_body, rec_enc_offset)
        elif (len_header == 20 and rec_enc_type in ["02dd", "03dd"]) or (len_header == 24 and enc_type == EncType.AES):
            # AES Encryption
            rec_body = self._aes_decrypt(enc_body)
        elif rec_enc_type == "00dd":  # Unencrypted
            rec_body = enc_body.decode("utf8")
        else:
            raise InvalidContentTypeError(f"Baichuan host {self._host}: received unknown encryption type '{rec_enc_type}', data: {data.hex()}")

        return rec_body

    def _hide_password(self, content: str | bytes | dict | list) -> str:
        """Redact sensitive informtation from the logs"""
        redacted = str(content)
        if self._password:
            redacted = redacted.replace(self._password, "<password>")
        if self._nonce:
            redacted = redacted.replace(self._nonce, "<nonce>")
        if self._user_hash:
            redacted = redacted.replace(self._user_hash, "<user_md5_hash>")
        if self._password_hash:
            redacted = redacted.replace(self._password_hash, "<password_md5_hash>")
        return redacted

    def _push_callback(self, cmd_id: int, data: bytes, len_header: int) -> None:
        """Callback to parse a received message that was pushed"""
        # decryption
        rec_body = self._decrypt(data, len_header)

        if len(rec_body) == 0:
            _LOGGER.debug("Baichuan host %s: received push cmd_id %s withouth body", self._host, cmd_id)
            return

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Baichuan host %s: received push cmd_id %s:\n%s", self._host, cmd_id, self._hide_password(rec_body))

    def _get_keys_from_xml(self, xml: str, keys: list[str]) -> dict[str, str]:
        """Get multiple keys from a xml and return as a dict"""
        root = XML.fromstring(xml)
        result = {}
        for key in keys:
            xml_value = root.find(f".//{key}")
            if xml_value is None:
                continue
            value = xml_value.text
            if value is None:
                continue
            result[key] = value

        return result

    def _get_value_from_xml(self, xml: str, key: str) -> str | None:
        """Get the value of a single key in a xml"""
        return self._get_keys_from_xml(xml, [key]).get(key)

    async def _get_nonce(self) -> str:
        """Get the nonce needed for the modern login"""
        # send only a header to receive the nonce (alternatively use legacy login)
        mess = await self.send(cmd_id=1, enc_type=EncType.BC, message_class="1465")
        self._nonce = self._get_value_from_xml(mess, "nonce")
        if self._nonce is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: could not find nonce in response:\n{mess}")

        aes_key_str = md5_str_modern(f"{self._nonce}-{self._password}")[0:16]
        self._aes_key = aes_key_str.encode("utf8")

        return self._nonce

    async def login(self) -> None:
        """Login using the Baichuan protocol"""
        nonce = await self._get_nonce()

        # modern login
        self._user_hash = md5_str_modern(f"{self._username}{nonce}")
        self._password_hash = md5_str_modern(f"{self._password}{nonce}")
        xml = xmls.LOGIN_XML.format(userName=self._user_hash, password=self._password_hash)

        await self.send(cmd_id=1, enc_type=EncType.BC, body=xml)
        self._logged_in = True

    async def logout(self) -> None:
        """Close the TCP session and cleanup"""
        if self._transport is not None and self._protocol is not None:
            try:
                xml = xmls.LOGOUT_XML.format(userName=self._username, password=self._password)
                await self.send(cmd_id=2, body=xml)
            except ReolinkError as err:
                _LOGGER.error("Baichuan host %s: failed to logout: %s", self._host, err)

            try:
                self._transport.close()
                await self._protocol.close_future
            except ConnectionResetError as err:
                _LOGGER.debug("Baichuan host %s: connection already reset when trying to close: %s", self._host, err)

        self._logged_in = False
        self._transport = None
        self._protocol = None
        self._nonce = None
        self._aes_key = None
        self._user_hash = None
        self._password_hash = None

    async def get_ports(self) -> dict[str, dict[str, int | bool]]:
        """Get the HTTP(S)/RTSP/RTMP/ONVIF port state"""
        mess = await self.send(cmd_id=37)

        self._ports = {}
        root = XML.fromstring(mess)
        for protocol in root:
            for key in protocol:
                proto_key = protocol.tag.replace("Port", "").lower()
                sub_key = key.tag.replace(proto_key, "").lower()
                if key.text is None:
                    continue
                self._ports.setdefault(proto_key, {})
                self._ports[proto_key][sub_key] = int(key.text)

        return self._ports

    async def set_port_enabled(self, port: PortType, enable: bool) -> None:
        """set the HTTP(S)/RTSP/RTMP/ONVIF port"""
        xml_body = XML.Element("body")
        main = XML.SubElement(xml_body, port.value.capitalize() + "Port", version="1.1")
        sub = XML.SubElement(main, "enable")
        sub.text = "1" if enable else "0"
        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml

        await self.send(cmd_id=36, body=xml)

    async def get_info(self) -> dict[str, str]:
        """Get the device info of the host"""
        mess = await self.send(cmd_id=80)
        self._dev_info = self._get_keys_from_xml(mess, ["type", "hardwareVersion", "firmwareVersion", "itemNo"])
        return self._dev_info

    async def get_channel_uids(self) -> None:
        """Get a channel list containing the UIDs"""
        # the NVR sends a message with cmd_id 145 when connecting, but it seems to not allow requesting that id.
        await self.send(cmd_id=145)

    async def get_wifi_signal(self) -> None:
        """Get the wifi signal of the host"""
        await self.send(cmd_id=115)

    @property
    def http_port(self) -> int | None:
        return self._ports.get("http", {}).get("port")

    @property
    def https_port(self) -> int | None:
        return self._ports.get("https", {}).get("port")

    @property
    def rtmp_port(self) -> int | None:
        return self._ports.get("rtmp", {}).get("port")

    @property
    def rtsp_port(self) -> int | None:
        return self._ports.get("rtsp", {}).get("port")

    @property
    def onvif_port(self) -> int | None:
        return self._ports.get("onvif", {}).get("port")

    @property
    def http_enabled(self) -> bool | None:
        enabled = self._ports.get("http", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def https_enabled(self) -> bool | None:
        enabled = self._ports.get("https", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def rtmp_enabled(self) -> bool | None:
        enabled = self._ports.get("rtmp", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def rtsp_enabled(self) -> bool | None:
        enabled = self._ports.get("rtsp", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def onvif_enabled(self) -> bool | None:
        enabled = self._ports.get("onvif", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def model(self) -> str | None:
        return self._dev_info.get("type")

    @property
    def hardware_version(self) -> str | None:
        return self._dev_info.get("hardwareVersion")

    @property
    def item_number(self) -> str | None:
        return self._dev_info.get("itemNo")

    @property
    def sw_version(self) -> str | None:
        return self._dev_info.get("firmwareVersion")
