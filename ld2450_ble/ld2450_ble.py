from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Callable
from typing import Any, TypeVar

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
    retry_bluetooth_connection_error,
)

#CONSTANTS FROM CONST FILE
from .const import (
    CHARACTERISTIC_NOTIFY,
    CHARACTERISTIC_WRITE,
    CMD_DISABLE_CONFIG,
    CMD_ENABLE_CONFIG,
    frame_regex
    )
from .exceptions import CharacteristicMissingError
from .models import LD2450BLEState

BLEAK_BACKOFF_TIME = 0.25

__version__ = "0.0.0"


WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

RETRY_BACKOFF_EXCEPTIONS = (BleakDBusError,)

_LOGGER = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = sys.maxsize


class LD2450BLE:
    def __init__(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
        #password: bytes = CMD_BT_PASS_DEFAULT,
    ) -> None:
        """Init the LD2450BLE."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        #self._password = password
        self._operation_lock = asyncio.Lock()
        self._state = LD2450BLEState()
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._expected_disconnect = False
        self.loop = asyncio.get_running_loop()
        self._callbacks: list[Callable[[LD2450BLEState], None]] = []
        self._disconnected_callbacks: list[Callable[[], None]] = []
        self._buf = b""

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def _address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the name of the device."""
        return self._ble_device.name or self._ble_device.address

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    @property
    def state(self) -> LD2450BLEState:
        """Return the state."""
        return self._state

    @property
    def target_one_x(self) -> int:
        return self._state.target_one_x
    @property
    def target_one_y(self) -> int:
        return self._state.target_one_y
    @property
    def target_one_speed(self) -> int:
        return self._state.target_one_speed
    @property
    def target_one_resolution(self) -> int:
        return self._state.target_one_resolution

    @property
    def target_two_x(self) -> int:
        return self._state.target_two_x
    @property
    def target_two_y(self) -> int:
        return self._state.target_two_y
    @property
    def target_two_speed(self) -> int:
        return self._state.target_two_speed
    @property
    def target_two_resolution(self) -> int:
        return self._state.target_two_resolution

    @property
    def target_three_x(self) -> int:
        return self._state.target_three_x
    @property
    def target_three_y(self) -> int:
        return self._state.target_three_y
    @property
    def target_three_speed(self) -> int:
        return self._state.target_three_speed
    @property
    def target_three_resolution(self) -> int:
        return self._state.target_three_resolution


    async def stop(self) -> None:
        """Stop the LD2410BLE."""
        _LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    def _fire_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(self._state)

    def register_callback(
        self, callback: Callable[[LD2450BLEState], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def _fire_disconnected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._disconnected_callbacks:
            callback()

    def register_disconnected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._disconnected_callbacks.remove(callback)

        self._disconnected_callbacks.append(callback)
        return unregister_callback

    async def initialise(self) -> None:
        await self._ensure_connected()

        _LOGGER.debug("%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi)
        if self._client is not None:
            _LOGGER.debug(self._client)
            await self._client.start_notify(
                CHARACTERISTIC_NOTIFY, self._notification_handler
            )
        else:
            _LOGGER.debug("Client is unexpectedly None")

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            _LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete; RSSI: %s",
                self.name,
                self.rssi,
            )
        if self._client and self._client.is_connected:
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                return
            _LOGGER.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self.name,
                self._disconnected,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device,
            )
            _LOGGER.debug("%s: Connected; RSSI: %s", self.name, self.rssi)

            self._client = client

    async def _reconnect(self) -> None:
        """Attempt a reconnect"""
        _LOGGER.debug("ensuring connection")
        try:
            await self._ensure_connected()
            _LOGGER.debug("ensured connection - initialising")
            await self.initialise()
        except BleakNotFoundError:
            _LOGGER.debug("failed to ensure connection - backing off")
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug("reconnecting again")
            asyncio.create_task(self._reconnect())

    def intify(self, state: bytes) -> int:
        return int.from_bytes(state, byteorder="little")

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        _LOGGER.debug("%s: Notification received: %s", self.name, data.hex())

        self._buf += data
        msg = re.search(frame_regex, self._buf)
        if msg:
            self._buf = self._buf[msg.end() :]  # noqa: E203

            target_one_x = msg.group("target_one_x")[0] + msg.group("target_one_x")[1] * 256
            if target_one_x > 2**15:
                target_one_x = target_one_x - 2**15
            else:
                target_one_x = - target_one_x
            target_one_y = msg.group("target_one_y")[0] + msg.group("target_one_y")[1] * 256
            if target_one_y > 2**15:
                target_one_y = target_one_y - 2**15
            else:
                target_one_y = - target_one_y
            target_one_speed = msg.group("target_one_s")[0] + msg.group("target_one_s")[1] * 256
            if target_one_speed > 2**15:
                target_one_speed = target_one_speed - 2**15
            else:
                target_one_speed = - target_one_speed
            target_one_resolution = msg.group("target_one_r")[0] + msg.group("target_one_r")[1] * 256

            target_two_x = msg.group("target_two_x")[0] + msg.group("target_two_x")[1] * 256
            if target_two_x > 2**15:
                target_two_x = target_two_x - 2**15
            else:
                target_two_x = - target_two_x
            target_two_y = msg.group("target_two_y")[0] + msg.group("target_two_y")[1] * 256
            if target_two_y > 2**15:
                target_two_y = target_two_y - 2**15
            else:
                target_two_y = - target_two_y
            target_two_speed = msg.group("target_two_s")[0] + msg.group("target_two_s")[1] * 256
            if target_two_speed > 2**15:
                target_two_speed = target_two_speed - 2**15
            else:
                target_two_speed = - target_two_speed
            target_two_resolution = msg.group("target_two_r")[0] + msg.group("target_two_r")[1] * 256

            target_three_x = msg.group("target_three_x")[0] + msg.group("target_three_x")[1] * 256
            if target_three_x > 2**15:
                target_three_x = target_three_x - 2**15
            else:
                target_three_x = - target_three_x
            target_three_y = msg.group("target_three_y")[0] + msg.group("target_three_y")[1] * 256
            if target_three_y > 2**15:
                target_three_y = target_three_y - 2**15
            else:
                target_three_y = - target_three_y
            target_three_speed = msg.group("target_three_s")[0] + msg.group("target_three_s")[1] * 256
            if target_three_speed > 2**15:
                target_three_speed = target_three_speed - 2**15
            else:
                target_three_speed = - target_three_speed
            target_three_resolution = msg.group("target_three_r")[0] + msg.group("target_three_r")[1] * 256

            self._state = LD2450BLEState(
                target_one_x = target_one_x,
                target_one_y = target_one_y,
                target_one_speed = target_one_speed,
                target_one_resolution = target_one_resolution,

                target_two_x = target_two_x,
                target_two_y = target_two_y,
                target_two_speed = target_two_speed,
                target_two_resolution = target_two_resolution,

                target_three_x = target_three_x,
                target_three_y = target_three_y,
                target_three_speed = target_three_speed,
                target_three_resolution = target_three_resolution,
            )

            self._fire_callbacks()

        _LOGGER.debug(
            "%s: Notification received; RSSI: %s: %s %s",
            self.name,
            self.rssi,
            data.hex(),
            self._state,
        )

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        self._fire_disconnected_callbacks()
        if self._expected_disconnect:
            _LOGGER.debug(
                "%s: Disconnected from device; RSSI: %s", self.name, self.rssi
            )
            return
        _LOGGER.warning(
            "%s: Device unexpectedly disconnected; RSSI: %s",
            self.name,
            self.rssi,
        )
        asyncio.create_task(self._reconnect())

    def _disconnect(self) -> None:
        """Disconnect from device."""
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting",
            self.name,
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            self._expected_disconnect = True
            self._client = None
            if client and client.is_connected:
                await client.stop_notify(CHARACTERISTIC_NOTIFY)
                await client.disconnect()

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _send_command_locked(self, commands: list[bytes]) -> None:
        """Send command to device and read response."""
        try:
            await self._execute_command_locked(commands)
        except BleakDBusError as ex:
            # Disconnect so we can reset state and try again
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug(
                "%s: RSSI: %s; Backing off %ss; Disconnecting due to error: %s",
                self.name,
                self.rssi,
                BLEAK_BACKOFF_TIME,
                ex,
            )
            await self._execute_disconnect()
            raise
        except BleakError as ex:
            # Disconnect so we can reset state and try again
            _LOGGER.debug(
                "%s: RSSI: %s; Disconnecting due to error: %s", self.name, self.rssi, ex
            )
            await self._execute_disconnect()
            raise

    async def _send_command(
        self, commands: list[bytes] | bytes, retry: int | None = None
    ) -> None:
        """Send command to device and read response."""
        await self._ensure_connected()
        if not isinstance(commands, list):
            commands = [commands]
        await self._send_command_while_connected(commands, retry)

    async def _send_command_while_connected(
        self, commands: list[bytes], retry: int | None = None
    ) -> None:
        """Send command to device and read response."""
        _LOGGER.debug(
            "%s: Sending commands %s",
            self.name,
            [command.hex() for command in commands],
        )
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, waiting for it to complete; RSSI: %s",
                self.name,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                await self._send_command_locked(commands)
                return
            except BleakNotFoundError:
                _LOGGER.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.name,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except CharacteristicMissingError as ex:
                _LOGGER.debug(
                    "%s: characteristic missing: %s; RSSI: %s",
                    self.name,
                    ex,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                _LOGGER.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    async def _execute_command_locked(self, commands: list[bytes]) -> None:
        """Execute command and read response."""
        assert self._client is not None  # nosec
        for command in commands:
            await self._client.write_gatt_char(CHARACTERISTIC_WRITE, command, False)

    #self send command target single/multi