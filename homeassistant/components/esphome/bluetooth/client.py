"""Bluetooth client for esphome."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from dataclasses import dataclass, field
from functools import partial
import logging
import sys
from typing import Any, TypeVar, cast
import uuid

if sys.version_info < (3, 12):
    from typing_extensions import Buffer
else:
    from collections.abc import Buffer

from aioesphomeapi import (
    ESP_CONNECTION_ERROR_DESCRIPTION,
    ESPHOME_GATT_ERRORS,
    APIClient,
    APIVersion,
    BLEConnectionError,
    BluetoothProxyFeature,
    DeviceInfo,
)
from aioesphomeapi.core import (
    APIConnectionError,
    BluetoothGATTAPIError,
    TimeoutAPIError,
)
from async_interrupt import interrupt
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient, NotifyCallback
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakError

from homeassistant.core import CALLBACK_TYPE

from .cache import ESPHomeBluetoothCache
from .characteristic import BleakGATTCharacteristicESPHome
from .descriptor import BleakGATTDescriptorESPHome
from .device import ESPHomeBluetoothDevice
from .scanner import ESPHomeScanner
from .service import BleakGATTServiceESPHome

DEFAULT_MTU = 23
GATT_HEADER_SIZE = 3
DISCONNECT_TIMEOUT = 5.0
CONNECT_FREE_SLOT_TIMEOUT = 2.0
GATT_READ_TIMEOUT = 30.0

# CCCD (Characteristic Client Config Descriptor)
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
CCCD_NOTIFY_BYTES = b"\x01\x00"
CCCD_INDICATE_BYTES = b"\x02\x00"

DEFAULT_MAX_WRITE_WITHOUT_RESPONSE = DEFAULT_MTU - GATT_HEADER_SIZE
_LOGGER = logging.getLogger(__name__)

_WrapFuncType = TypeVar("_WrapFuncType", bound=Callable[..., Any])


def mac_to_int(address: str) -> int:
    """Convert a mac address to an integer."""
    return int(address.replace(":", ""), 16)


def verify_connected(func: _WrapFuncType) -> _WrapFuncType:
    """Define a wrapper throw BleakError if not connected."""

    async def _async_wrap_bluetooth_connected_operation(
        self: ESPHomeClient, *args: Any, **kwargs: Any
    ) -> Any:
        # pylint: disable=protected-access
        loop = self._loop
        disconnected_futures = self._disconnected_futures
        disconnected_future = loop.create_future()
        disconnected_futures.add(disconnected_future)
        ble_device = self._ble_device
        disconnect_message = (
            f"{self._source_name }: {ble_device.name} - {ble_device.address}: "
            "Disconnected during operation"
        )
        try:
            async with interrupt(disconnected_future, BleakError, disconnect_message):
                return await func(self, *args, **kwargs)
        finally:
            disconnected_futures.discard(disconnected_future)

    return cast(_WrapFuncType, _async_wrap_bluetooth_connected_operation)


def api_error_as_bleak_error(func: _WrapFuncType) -> _WrapFuncType:
    """Define a wrapper throw esphome api errors as BleakErrors."""

    async def _async_wrap_bluetooth_operation(
        self: ESPHomeClient, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            return await func(self, *args, **kwargs)
        except TimeoutAPIError as err:
            raise asyncio.TimeoutError(str(err)) from err
        except BluetoothGATTAPIError as ex:
            # If the device disconnects in the middle of an operation
            # be sure to mark it as disconnected so any library using
            # the proxy knows to reconnect.
            #
            # Because callbacks are delivered asynchronously it's possible
            # that we find out about the disconnection during the operation
            # before the callback is delivered.

            if ex.error.error == -1:
                # pylint: disable=protected-access
                _LOGGER.debug(
                    "%s: %s - %s: BLE device disconnected during %s operation",
                    self._source_name,
                    self._ble_device.name,
                    self._ble_device.address,
                    func.__name__,
                )
                self._async_ble_device_disconnected()
            raise BleakError(str(ex)) from ex
        except APIConnectionError as err:
            raise BleakError(str(err)) from err

    return cast(_WrapFuncType, _async_wrap_bluetooth_operation)


@dataclass(slots=True)
class ESPHomeClientData:
    """Define a class that stores client data for an esphome client."""

    bluetooth_device: ESPHomeBluetoothDevice
    cache: ESPHomeBluetoothCache
    client: APIClient
    device_info: DeviceInfo
    api_version: APIVersion
    title: str
    scanner: ESPHomeScanner | None
    disconnect_callbacks: list[Callable[[], None]] = field(default_factory=list)


class ESPHomeClient(BaseBleakClient):
    """ESPHome Bleak Client."""

    def __init__(
        self,
        address_or_ble_device: BLEDevice | str,
        *args: Any,
        client_data: ESPHomeClientData,
        **kwargs: Any,
    ) -> None:
        """Initialize the ESPHomeClient."""
        device_info = client_data.device_info
        self._disconnect_callbacks = client_data.disconnect_callbacks
        assert isinstance(address_or_ble_device, BLEDevice)
        super().__init__(address_or_ble_device, *args, **kwargs)
        self._loop = asyncio.get_running_loop()
        self._ble_device = address_or_ble_device
        self._address_as_int = mac_to_int(self._ble_device.address)
        assert self._ble_device.details is not None
        self._source = self._ble_device.details["source"]
        self._cache = client_data.cache
        self._bluetooth_device = client_data.bluetooth_device
        self._client = client_data.client
        self._is_connected = False
        self._mtu: int | None = None
        self._cancel_connection_state: CALLBACK_TYPE | None = None
        self._notify_cancels: dict[
            int, tuple[Callable[[], Coroutine[Any, Any, None]], Callable[[], None]]
        ] = {}
        self._disconnected_futures: set[asyncio.Future[None]] = set()
        self._device_info = client_data.device_info
        self._feature_flags = device_info.bluetooth_proxy_feature_flags_compat(
            client_data.api_version
        )
        self._address_type = address_or_ble_device.details["address_type"]
        self._source_name = f"{client_data.title} [{self._source}]"
        scanner = client_data.scanner
        assert scanner is not None
        self._scanner = scanner

    def __str__(self) -> str:
        """Return the string representation of the client."""
        return f"ESPHomeClient ({self.address})"

    def _unsubscribe_connection_state(self) -> None:
        """Unsubscribe from connection state updates."""
        if not self._cancel_connection_state:
            return
        try:
            self._cancel_connection_state()
        except (AssertionError, ValueError) as ex:
            _LOGGER.debug(
                (
                    "%s: %s - %s: Failed to unsubscribe from connection state (likely"
                    " connection dropped): %s"
                ),
                self._source_name,
                self._ble_device.name,
                self._ble_device.address,
                ex,
            )
        self._cancel_connection_state = None

    def _async_disconnected_cleanup(self) -> None:
        """Clean up on disconnect."""
        self.services = BleakGATTServiceCollection()  # type: ignore[no-untyped-call]
        self._is_connected = False
        for _, notify_abort in self._notify_cancels.values():
            notify_abort()
        self._notify_cancels.clear()
        for future in self._disconnected_futures:
            if not future.done():
                future.set_result(None)
        self._disconnected_futures.clear()
        self._unsubscribe_connection_state()

    def _async_ble_device_disconnected(self) -> None:
        """Handle the BLE device disconnecting from the ESP."""
        was_connected = self._is_connected
        self._async_disconnected_cleanup()
        if was_connected:
            _LOGGER.debug(
                "%s: %s - %s: BLE device disconnected",
                self._source_name,
                self._ble_device.name,
                self._ble_device.address,
            )
            self._async_call_bleak_disconnected_callback()

    def _async_esp_disconnected(self) -> None:
        """Handle the esp32 client disconnecting from us."""
        _LOGGER.debug(
            "%s: %s - %s: ESP device disconnected",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
        )
        self._disconnect_callbacks.remove(self._async_esp_disconnected)
        self._async_ble_device_disconnected()

    def _async_call_bleak_disconnected_callback(self) -> None:
        """Call the disconnected callback to inform the bleak consumer."""
        if self._disconnected_callback:
            self._disconnected_callback()
            self._disconnected_callback = None

    def _on_bluetooth_connection_state(
        self,
        connected_future: asyncio.Future[bool],
        connected: bool,
        mtu: int,
        error: int,
    ) -> None:
        """Handle a connect or disconnect."""
        _LOGGER.debug(
            "%s: %s - %s: Connection state changed to connected=%s mtu=%s error=%s",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
            connected,
            mtu,
            error,
        )
        if connected:
            self._is_connected = True
            if not self._mtu:
                self._mtu = mtu
                self._cache.set_gatt_mtu_cache(self._address_as_int, mtu)
        else:
            self._async_ble_device_disconnected()

        if connected_future.done():
            return

        if error:
            try:
                ble_connection_error = BLEConnectionError(error)
                ble_connection_error_name = ble_connection_error.name
                human_error = ESP_CONNECTION_ERROR_DESCRIPTION[ble_connection_error]
            except (KeyError, ValueError):
                ble_connection_error_name = str(error)
                human_error = ESPHOME_GATT_ERRORS.get(
                    error, f"Unknown error code {error}"
                )
            connected_future.set_exception(
                BleakError(
                    f"Error {ble_connection_error_name} while connecting:"
                    f" {human_error}"
                )
            )
            return

        if not connected:
            connected_future.set_exception(BleakError("Disconnected"))
            return

        _LOGGER.debug(
            "%s: %s - %s: connected, registering for disconnected callbacks",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
        )
        self._disconnect_callbacks.append(self._async_esp_disconnected)
        connected_future.set_result(connected)

    @api_error_as_bleak_error
    async def connect(
        self, dangerous_use_bleak_cache: bool = False, **kwargs: Any
    ) -> bool:
        """Connect to a specified Peripheral.

        **kwargs:
            timeout (float): Timeout for required
                ``BleakScanner.find_device_by_address`` call. Defaults to 10.0.

        Returns:
            Boolean representing connection status.
        """
        await self._wait_for_free_connection_slot(CONNECT_FREE_SLOT_TIMEOUT)
        cache = self._cache

        self._mtu = cache.get_gatt_mtu_cache(self._address_as_int)
        has_cache = bool(
            dangerous_use_bleak_cache
            and self._feature_flags & BluetoothProxyFeature.REMOTE_CACHING
            and cache.get_gatt_services_cache(self._address_as_int)
            and self._mtu
        )
        connected_future: asyncio.Future[bool] = self._loop.create_future()

        timeout = kwargs.get("timeout", self._timeout)
        with self._scanner.connecting():
            try:
                self._cancel_connection_state = (
                    await self._client.bluetooth_device_connect(
                        self._address_as_int,
                        partial(self._on_bluetooth_connection_state, connected_future),
                        timeout=timeout,
                        has_cache=has_cache,
                        feature_flags=self._feature_flags,
                        address_type=self._address_type,
                    )
                )
            except asyncio.CancelledError:
                if connected_future.done():
                    with contextlib.suppress(BleakError):
                        # If we are cancelled while connecting,
                        # we need to make sure we await the future
                        # to avoid a warning about an un-retrieved
                        # exception.
                        await connected_future
                raise
            except Exception as ex:
                if connected_future.done():
                    with contextlib.suppress(BleakError):
                        # If the connect call throws an exception,
                        # we need to make sure we await the future
                        # to avoid a warning about an un-retrieved
                        # exception since we prefer to raise the
                        # exception from the connect call as it
                        # will be more descriptive.
                        await connected_future
                connected_future.cancel(f"Unhandled exception in connect call: {ex}")
                raise
            await connected_future

        try:
            await self._get_services(
                dangerous_use_bleak_cache=dangerous_use_bleak_cache
            )
        except asyncio.CancelledError:
            # On cancel we must still raise cancelled error
            # to avoid blocking the cancellation even if the
            # disconnect call fails.
            with contextlib.suppress(Exception):
                await self._disconnect()
            raise
        except Exception:
            await self._disconnect()
            raise

        return True

    @api_error_as_bleak_error
    async def disconnect(self) -> bool:
        """Disconnect from the peripheral device."""
        return await self._disconnect()

    async def _disconnect(self) -> bool:
        self._async_disconnected_cleanup()
        await self._client.bluetooth_device_disconnect(self._address_as_int)
        await self._wait_for_free_connection_slot(DISCONNECT_TIMEOUT)
        return True

    async def _wait_for_free_connection_slot(self, timeout: float) -> None:
        """Wait for a free connection slot."""
        bluetooth_device = self._bluetooth_device
        if bluetooth_device.ble_connections_free:
            return
        _LOGGER.debug(
            "%s: %s - %s: Out of connection slots, waiting for a free one",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
        )
        async with asyncio.timeout(timeout):
            await bluetooth_device.wait_for_ble_connections_free()

    @property
    def is_connected(self) -> bool:
        """Is Connected."""
        return self._is_connected

    @property
    def mtu_size(self) -> int:
        """Get ATT MTU size for active connection."""
        return self._mtu or DEFAULT_MTU

    @verify_connected
    @api_error_as_bleak_error
    async def pair(self, *args: Any, **kwargs: Any) -> bool:
        """Attempt to pair."""
        if not self._feature_flags & BluetoothProxyFeature.PAIRING:
            raise NotImplementedError(
                "Pairing is not available in this version ESPHome; "
                f"Upgrade the ESPHome version on the {self._device_info.name} device."
            )
        response = await self._client.bluetooth_device_pair(self._address_as_int)
        if response.paired:
            return True
        _LOGGER.error(
            "Pairing with %s failed due to error: %s", self.address, response.error
        )
        return False

    @verify_connected
    @api_error_as_bleak_error
    async def unpair(self) -> bool:
        """Attempt to unpair."""
        if not self._feature_flags & BluetoothProxyFeature.PAIRING:
            raise NotImplementedError(
                "Unpairing is not available in this version ESPHome; "
                f"Upgrade the ESPHome version on the {self._device_info.name} device."
            )
        response = await self._client.bluetooth_device_unpair(self._address_as_int)
        if response.success:
            return True
        _LOGGER.error(
            "Unpairing with %s failed due to error: %s", self.address, response.error
        )
        return False

    @api_error_as_bleak_error
    async def get_services(
        self, dangerous_use_bleak_cache: bool = False, **kwargs: Any
    ) -> BleakGATTServiceCollection:
        """Get all services registered for this GATT server.

        Returns:
           A :py:class:`bleak.backends.service.BleakGATTServiceCollection`
           with this device's services tree.
        """
        return await self._get_services(
            dangerous_use_bleak_cache=dangerous_use_bleak_cache, **kwargs
        )

    @verify_connected
    async def _get_services(
        self, dangerous_use_bleak_cache: bool = False, **kwargs: Any
    ) -> BleakGATTServiceCollection:
        """Get all services registered for this GATT server.

        Must only be called from get_services or connected
        """
        address_as_int = self._address_as_int
        cache = self._cache
        # If the connection version >= 3, we must use the cache
        # because the esp has already wiped the services list to
        # save memory.
        if (
            self._feature_flags & BluetoothProxyFeature.REMOTE_CACHING
            or dangerous_use_bleak_cache
        ) and (cached_services := cache.get_gatt_services_cache(address_as_int)):
            _LOGGER.debug(
                "%s: %s - %s: Cached services hit",
                self._source_name,
                self._ble_device.name,
                self._ble_device.address,
            )
            self.services = cached_services
            return self.services
        _LOGGER.debug(
            "%s: %s - %s: Cached services miss",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
        )
        esphome_services = await self._client.bluetooth_gatt_get_services(
            address_as_int
        )
        _LOGGER.debug(
            "%s: %s - %s: Got services: %s",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
            esphome_services,
        )
        max_write_without_response = self.mtu_size - GATT_HEADER_SIZE
        services = BleakGATTServiceCollection()  # type: ignore[no-untyped-call]
        for service in esphome_services.services:
            services.add_service(BleakGATTServiceESPHome(service))
            for characteristic in service.characteristics:
                services.add_characteristic(
                    BleakGATTCharacteristicESPHome(
                        characteristic,
                        max_write_without_response,
                        service.uuid,
                        service.handle,
                    )
                )
                for descriptor in characteristic.descriptors:
                    services.add_descriptor(
                        BleakGATTDescriptorESPHome(
                            descriptor,
                            characteristic.uuid,
                            characteristic.handle,
                        )
                    )

        if not esphome_services.services:
            # If we got no services, we must have disconnected
            # or something went wrong on the ESP32's BLE stack.
            raise BleakError("Failed to get services from remote esp")

        self.services = services
        _LOGGER.debug(
            "%s: %s - %s: Cached services saved",
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
        )
        cache.set_gatt_services_cache(address_as_int, services)
        return services

    def _resolve_characteristic(
        self, char_specifier: BleakGATTCharacteristic | int | str | uuid.UUID
    ) -> BleakGATTCharacteristic:
        """Resolve a characteristic specifier to a BleakGATTCharacteristic object."""
        if (services := self.services) is None:
            raise BleakError("Services have not been resolved")
        if not isinstance(char_specifier, BleakGATTCharacteristic):
            characteristic = services.get_characteristic(char_specifier)
        else:
            characteristic = char_specifier
        if not characteristic:
            raise BleakError(f"Characteristic {char_specifier} was not found!")
        return characteristic

    @verify_connected
    @api_error_as_bleak_error
    async def clear_cache(self) -> bool:
        """Clear the GATT cache."""
        cache = self._cache
        cache.clear_gatt_services_cache(self._address_as_int)
        cache.clear_gatt_mtu_cache(self._address_as_int)
        if not self._feature_flags & BluetoothProxyFeature.CACHE_CLEARING:
            _LOGGER.warning(
                "On device cache clear is not available with this ESPHome version; "
                "Upgrade the ESPHome version on the device %s; Only memory cache will be cleared",
                self._device_info.name,
            )
            return True
        response = await self._client.bluetooth_device_clear_cache(self._address_as_int)
        if response.success:
            return True
        _LOGGER.error(
            "Clear cache failed with %s failed due to error: %s",
            self.address,
            response.error,
        )
        return False

    @verify_connected
    @api_error_as_bleak_error
    async def read_gatt_char(
        self,
        char_specifier: BleakGATTCharacteristic | int | str | uuid.UUID,
        **kwargs: Any,
    ) -> bytearray:
        """Perform read operation on the specified GATT characteristic.

        Args:
            char_specifier (BleakGATTCharacteristic, int, str or UUID):
                The characteristic to read from, specified by either integer
                handle, UUID or directly by the BleakGATTCharacteristic
                object representing it.
            **kwargs: Unused

        Returns:
            (bytearray) The read data.
        """
        characteristic = self._resolve_characteristic(char_specifier)
        return await self._client.bluetooth_gatt_read(
            self._address_as_int, characteristic.handle, GATT_READ_TIMEOUT
        )

    @verify_connected
    @api_error_as_bleak_error
    async def read_gatt_descriptor(self, handle: int, **kwargs: Any) -> bytearray:
        """Perform read operation on the specified GATT descriptor.

        Args:
            handle (int): The handle of the descriptor to read from.
            **kwargs: Unused

        Returns:
            (bytearray) The read data.
        """
        return await self._client.bluetooth_gatt_read_descriptor(
            self._address_as_int, handle, GATT_READ_TIMEOUT
        )

    @verify_connected
    @api_error_as_bleak_error
    async def write_gatt_char(
        self,
        characteristic: BleakGATTCharacteristic | int | str | uuid.UUID,
        data: Buffer,
        response: bool = False,
    ) -> None:
        """Perform a write operation of the specified GATT characteristic.

        Args:
            characteristic (BleakGATTCharacteristic, int, str or UUID):
                The characteristic to write to, specified by either integer
                handle, UUID or directly by the BleakGATTCharacteristic object
                representing it.
            data (bytes or bytearray): The data to send.
            response (bool): If write-with-response operation should be done.
                Defaults to `False`.
        """
        characteristic = self._resolve_characteristic(characteristic)
        await self._client.bluetooth_gatt_write(
            self._address_as_int, characteristic.handle, bytes(data), response
        )

    @verify_connected
    @api_error_as_bleak_error
    async def write_gatt_descriptor(self, handle: int, data: Buffer) -> None:
        """Perform a write operation on the specified GATT descriptor.

        Args:
            handle (int): The handle of the descriptor to read from.
            data (bytes or bytearray): The data to send.
        """
        await self._client.bluetooth_gatt_write_descriptor(
            self._address_as_int, handle, bytes(data)
        )

    @verify_connected
    @api_error_as_bleak_error
    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: NotifyCallback,
        **kwargs: Any,
    ) -> None:
        """Activate notifications/indications on a characteristic.

        Callbacks must accept two inputs. The first will be a integer handle of the
        characteristic generating the data and the second will be a ``bytearray``
        containing the data sent from the connected server.

        .. code-block:: python
            def callback(sender: int, data: bytearray):
                print(f"{sender}: {data}")
            client.start_notify(char_uuid, callback)

        Args:
            characteristic (BleakGATTCharacteristic):
                The characteristic to activate notifications/indications on a
                characteristic, specified by either integer handle, UUID or
                directly by the BleakGATTCharacteristic object representing it.
            callback (function): The function to be called on notification.
            kwargs: Unused.
        """
        ble_handle = characteristic.handle
        if ble_handle in self._notify_cancels:
            raise BleakError(
                "Notifications are already enabled on "
                f"service:{characteristic.service_uuid} "
                f"characteristic:{characteristic.uuid} "
                f"handle:{ble_handle}"
            )
        if (
            "notify" not in characteristic.properties
            and "indicate" not in characteristic.properties
        ):
            raise BleakError(
                f"Characteristic {characteristic.uuid} does not have notify or indicate"
                " property set."
            )

        self._notify_cancels[
            ble_handle
        ] = await self._client.bluetooth_gatt_start_notify(
            self._address_as_int,
            ble_handle,
            lambda handle, data: callback(data),
        )

        if not self._feature_flags & BluetoothProxyFeature.REMOTE_CACHING:
            return

        # For connection v3 we are responsible for enabling notifications
        # on the cccd (characteristic client config descriptor) handle since
        # the esp32 will not have resolved the characteristic descriptors to
        # save memory since doing so can exhaust the memory and cause a soft
        # reset
        cccd_descriptor = characteristic.get_descriptor(CCCD_UUID)
        if not cccd_descriptor:
            raise BleakError(
                f"Characteristic {characteristic.uuid} does not have a "
                "characteristic client config descriptor."
            )

        _LOGGER.debug(
            (
                "%s: %s - %s: Writing to CCD descriptor %s for notifications with"
                " properties=%s"
            ),
            self._source_name,
            self._ble_device.name,
            self._ble_device.address,
            cccd_descriptor.handle,
            characteristic.properties,
        )
        supports_notify = "notify" in characteristic.properties
        await self._client.bluetooth_gatt_write_descriptor(
            self._address_as_int,
            cccd_descriptor.handle,
            CCCD_NOTIFY_BYTES if supports_notify else CCCD_INDICATE_BYTES,
            wait_for_response=False,
        )

    @verify_connected
    @api_error_as_bleak_error
    async def stop_notify(
        self,
        char_specifier: BleakGATTCharacteristic | int | str | uuid.UUID,
    ) -> None:
        """Deactivate notification/indication on a specified characteristic.

        Args:
            char_specifier (BleakGATTCharacteristic, int, str or UUID):
                The characteristic to deactivate notification/indication on,
                specified by either integer handle, UUID or directly by the
                BleakGATTCharacteristic object representing it.
        """
        characteristic = self._resolve_characteristic(char_specifier)
        # Do not raise KeyError if notifications are not enabled on this characteristic
        # to be consistent with the behavior of the BlueZ backend
        if notify_cancel := self._notify_cancels.pop(characteristic.handle, None):
            notify_stop, _ = notify_cancel
            await notify_stop()

    def __del__(self) -> None:
        """Destructor to make sure the connection state is unsubscribed."""
        if self._cancel_connection_state:
            _LOGGER.warning(
                (
                    "%s: %s - %s: ESPHomeClient bleak client was not properly"
                    " disconnected before destruction"
                ),
                self._source_name,
                self._ble_device.name,
                self._ble_device.address,
            )
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._async_disconnected_cleanup)
