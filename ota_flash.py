#!/usr/bin/env python3
"""
ZZK Switch Assistant - Standalone BLE OTA Flasher
Automatically triggers DFU on ZZK-SA2, waits for XIAO-DFU, and transfers firmware.
"""

import sys
import os
import json
import zipfile
import struct
import asyncio
import argparse
from contextlib import AsyncExitStack, asynccontextmanager
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

UUID_SWITCH_SERVICE = "2d220000-516b-47bd-a33b-2c93889ac9b7"

# UUIDs for Nordic Legacy DFU (used by Adafruit / Seeed Bootloader)
UUID_DFU_SERVICE = "00001530-1212-efde-1523-785feabcd123"
UUID_DFU_CONTROL = "00001531-1212-efde-1523-785feabcd123"
UUID_DFU_PACKET  = "00001532-1212-efde-1523-785feabcd123"

# OpCodes
OP_START_DFU                   = 1
OP_INITIALIZE_DFU              = 2
OP_RECEIVE_FIRMWARE_IMAGE      = 3
OP_VALIDATE_FIRMWARE_IMAGE     = 4
OP_ACTIVATE_FIRMWARE_AND_RESET = 5
OP_REQ_PKT_RCPT_NOTIFICATION   = 8
OP_RESPONSE                    = 16

PKT_RECEIPT_NOTIF_INTERVAL = 10
DATA_PACKET_SIZE = 20


class ActivationUnconfirmed(RuntimeError):
    """The image was validated, but returning to application mode is unconfirmed."""


def is_windows_reset_cancellation(error):
    # ERROR_CANCELLED (1223), either as Win32 or signed/unsigned HRESULT.
    code = getattr(error, "winerror", None)
    return isinstance(error, OSError) and isinstance(code, int) and (
            code & 0xFFFFFFFF
    ) in (1223, 0x800704C7)


class LegacyDfuClient:
    def __init__(self, client: BleakClient):
        self.client = client
        self.response_future = None
        self.notification_event = asyncio.Event()
        self.activation_started = False

    def _notification_handler(self, sender, data: bytearray):
        opcode = data[0]
        if opcode == OP_RESPONSE:
            req_op = data[1]
            status = data[2]
            if self.response_future and not self.response_future.done():
                self.response_future.set_result((req_op, status))
        elif opcode == 17:  # PKT_RCPT_NOTIF
            self.notification_event.set()

    async def start(self):
        # Allow BlueZ GATT discovery and connection parameters to stabilize
        await asyncio.sleep(0.6)
        for attempt in range(3):
            try:
                await self.client.start_notify(UUID_DFU_CONTROL, self._notification_handler)
                return
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.6)

    async def wait_response(self, expected_op: int, timeout=15.0):
        try:
            res_op, status = await asyncio.wait_for(self.response_future, timeout=timeout)
            if res_op != expected_op:
                raise RuntimeError(f"Expected DFU response for 0x{expected_op:02X}, "
                                   f"received 0x{res_op:02X}")
            if status != 1:  # 1 = SUCCESS
                raise RuntimeError(f"DFU opcode 0x{expected_op:02X} failed with error status: {status}")
            return res_op, status
        except asyncio.TimeoutError:
            raise TimeoutError(f"DFU opcode 0x{expected_op:02X} timed out waiting for response")

    async def send_control(self, opcode: int, data: bytes = b"", wait_for_response=True, timeout=15.0):
        if wait_for_response:
            self.response_future = asyncio.get_running_loop().create_future()

        payload = bytes([opcode]) + data
        await self.client.write_gatt_char(UUID_DFU_CONTROL, payload, response=True)

        if wait_for_response:
            return await self.wait_response(opcode, timeout)

    async def send_packet(self, data: bytes):
        await self.client.write_gatt_char(UUID_DFU_PACKET, data, response=False)

    async def flash_package(self, dat_bytes: bytes, bin_bytes: bytes):
        self.activation_started = False
        print(f"📦 Preparing transfer (Init size: {len(dat_bytes)}B, App size: {len(bin_bytes)}B)...")

        # 1. Start DFU (Application mode = 4)
        size_packet = struct.pack("<III", 0, 0, len(bin_bytes))
        print("➡️  Sending START DFU command & image size...")
        self.response_future = asyncio.get_running_loop().create_future()
        await self.client.write_gatt_char(UUID_DFU_CONTROL, bytes([OP_START_DFU, 4]), response=True)
        await self.send_packet(size_packet)
        await self.wait_response(OP_START_DFU, timeout=15.0)

        # 2. Init Packet
        print("➡️  Sending Init Packet...")
        # 0x02 + 0x00: Init Packet Start (No notification expected)
        await self.send_control(OP_INITIALIZE_DFU, bytes([0]), wait_for_response=False)
        for i in range(0, len(dat_bytes), DATA_PACKET_SIZE):
            chunk = dat_bytes[i:i + DATA_PACKET_SIZE]
            await self.send_packet(chunk)
        # 0x02 + 0x01: Init Packet Complete (Wait for notification)
        await self.send_control(OP_INITIALIZE_DFU, bytes([1]), wait_for_response=True, timeout=15.0)

        # 3. Set Packet Receipt Notification Interval
        await self.send_control(OP_REQ_PKT_RCPT_NOTIFICATION, struct.pack("<H", PKT_RECEIPT_NOTIF_INTERVAL), wait_for_response=False)

        # 4. Receive Firmware Image
        print("➡️  Sending Firmware Image...")
        self.response_future = asyncio.get_running_loop().create_future()
        await self.client.write_gatt_char(UUID_DFU_CONTROL, bytes([OP_RECEIVE_FIRMWARE_IMAGE]), response=True)

        total_bytes = len(bin_bytes)
        bytes_sent = 0
        pkt_count = 0

        for i in range(0, total_bytes, DATA_PACKET_SIZE):
            chunk = bin_bytes[i:i + DATA_PACKET_SIZE]
            await self.send_packet(chunk)
            bytes_sent += len(chunk)
            pkt_count += 1

            if pkt_count % PKT_RECEIPT_NOTIF_INTERVAL == 0:
                self.notification_event.clear()
                try:
                    await asyncio.wait_for(self.notification_event.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"Lost notification ack around {bytes_sent}/{total_bytes} bytes")

            # Progress print
            percent = (bytes_sent / total_bytes) * 100
            sys.stdout.write(f"\r🚀 Progress: {percent:5.1f}% [{bytes_sent}/{total_bytes} bytes]")
            sys.stdout.flush()

        print("\n⏳ Waiting for image transfer confirmation...")
        await self.wait_response(OP_RECEIVE_FIRMWARE_IMAGE, timeout=25.0)

        # 5. Validate Firmware
        print("🔍 Validating firmware on device...")
        await self.send_control(OP_VALIDATE_FIRMWARE_IMAGE, wait_for_response=True, timeout=15.0)

        # 6. Activate and Reset
        print("🔄 Activating firmware & rebooting device...")
        self.activation_started = True
        await self.send_control(OP_ACTIVATE_FIRMWARE_AND_RESET, wait_for_response=False)


@asynccontextmanager
async def connect_dfu_device(device, timeout):
    """Resolve the DFU service with a separate connection/GATT timeout."""
    print(f"Connecting to {device.name or '<unnamed>'} ({device.address}); "
          f"connection/GATT timeout {timeout:g}s...", flush=True)
    async with AsyncExitStack() as stack:
        try:
            client = await stack.enter_async_context(BleakClient(
                device, timeout=timeout, services=[UUID_DFU_SERVICE],
                winrt={"use_cached_services": False},
            ))
        except TimeoutError as error:
            raise RuntimeError(
                f"Connection/GATT discovery timed out after {timeout:g}s. "
                "Keep the gateway connection released and move the PC/Bluetooth "
                "antenna closer. --connect-timeout controls this wait; "
                "--timeout only controls scanning."
            ) from error
        yield client


def require_dfu_control(client):
    for service in client.services:
        for characteristic in service.characteristics:
            if characteristic.uuid.lower() == UUID_DFU_CONTROL:
                return characteristic
    raise RuntimeError("DFU Control characteristic was not found on this device.")


async def check_connection(device, timeout):
    """Discover services only; never subscribe, reboot or transfer firmware."""
    async with connect_dfu_device(device, timeout) as client:
        require_dfu_control(client)
        print("Connection check passed: DFU service and Control characteristic "
              "are accessible. No DFU command or firmware was sent.")


async def application_is_advertising(address, timeout=30.0):
    def matches(device, advertisement):
        # Require the original address AND the application service, never a
        # nearby switch or a cached name left over from before the reboot.
        services = {uuid.lower() for uuid in (advertisement.service_uuids or [])}
        return (device.address.casefold() == address.casefold()
                and UUID_SWITCH_SERVICE in services)

    device = await BleakScanner.find_device_by_filter(matches, timeout=timeout)
    return device is not None


async def flash_and_handle_reset(device, timeout, dat_bytes, bin_bytes,
                                 application_address=None):
    dfu = None
    try:
        async with connect_dfu_device(device, timeout) as client:
            dfu = LegacyDfuClient(client)
            await dfu.start()
            await dfu.flash_package(dat_bytes, bin_bytes)
    except OSError as error:
        if (dfu is None or not dfu.activation_started
                or not is_windows_reset_cancellation(error)):
            raise
        print("Firmware transfer and device validation passed. Windows cancelled "
              "the final reset write/cleanup; checking application restart...")
        if application_address and await application_is_advertising(application_address):
            print(f"✅ Firmware transferred and validated; application returned "
                  f"at {application_address}.")
            return
        raise ActivationUnconfirmed(
            "Firmware was transferred and validated, but application restart "
            "could not be confirmed. Check whether the gateway has reconnected "
            "and shows a battery level before repeating OTA. "
            "The application may stop advertising once the gateway connects."
        ) from error
    print("✅ OTA Upgrade Complete successfully!")


async def trigger_dfu_from_app(target_device, connect_timeout=45.0):
    """Connect to ZZK-SA2, enable notify on DFU Control, and tell BLEDfu to reboot into DFU mode."""
    async with connect_dfu_device(target_device, connect_timeout) as client:
        require_dfu_control(client)

        def dummy_cb(sender, data):
            pass

        print("🔔 Enabling DFU Control notifications...")
        for attempt in range(3):
            try:
                await client.start_notify(UUID_DFU_CONTROL, dummy_cb)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)
        await asyncio.sleep(0.3)

        print("⚡ Sending reboot to DFU Bootloader command...")
        try:
            await client.write_gatt_char(UUID_DFU_CONTROL, bytes([1]), response=True)
            await asyncio.sleep(0.5)
        except Exception:
            pass
    print("⏳ Device rebooted into Bootloader.")



def is_xiao_dfu_name(name: str) -> bool:
    n = (name or "").strip().upper()
    return n.startswith(("XIAO_DFU", "XIAO-DFU", "ADA_DFU")) or n == "DFUTARG"


def classify_advertisement(device, advertisement):
    """The main advertisement can arrive without the name in the scan response."""
    names = (advertisement.local_name or "", device.name or "")
    services = {uuid.lower() for uuid in (advertisement.service_uuids or [])}
    if UUID_SWITCH_SERVICE in services or any(
            name.upper().startswith("ZZK-SA2") for name in names
    ):
        return "app"
    if UUID_DFU_SERVICE in services or any(is_xiao_dfu_name(name) for name in names):
        return "dfu"
    return None


async def scan_targets(timeout, address=None):
    # Preserve advertisement data and BLEDevice objects for the later connection.
    results = await BleakScanner.discover(
        timeout=timeout, return_adv=True, scanning_mode="active"
    )
    apps, bootloaders = [], []
    for device, advertisement in results.values():
        kind = classify_advertisement(device, advertisement)
        exact_target = address and device.address.casefold() == address.casefold()
        if kind is None and not exact_target:
            continue
        name = advertisement.local_name or device.name or "<unnamed>"
        print(f"  {kind or 'unidentified'}: {name} ({device.address}), "
              f"RSSI {advertisement.rssi} dBm", flush=True)
        if kind == "app":
            apps.append(device)
        elif kind == "dfu":
            bootloaders.append(device)
        else:
            print("    Address seen, but no switch/DFU identity received; "
                  "move closer and scan again.")
    if not apps and not bootloaders:
        print(f"  No switch or DFU identity found among {len(results)} advertisers.")
    return apps, bootloaders


def select_target(apps, bootloaders, address=None, dfu_address=None):
    """Never silently choose the last/strongest device when several are visible."""
    if address:
        matching_apps = [d for d in apps if d.address.casefold() == address.casefold()]
        matching_dfu = [d for d in bootloaders if d.address.casefold() == address.casefold()]
        return (matching_apps[0] if matching_apps else None,
                matching_dfu[0] if matching_dfu else None)
    if dfu_address:
        matches = [d for d in bootloaders if d.address.casefold() == dfu_address.casefold()]
        return None, matches[0] if matches else None
    candidates = [(d, None) for d in apps] + [(None, d) for d in bootloaders]
    if len(candidates) > 1:
        raise RuntimeError(
            "Multiple OTA candidates found. Use --address for the switch or "
            "--dfu-address for a bootloader shown by --scan-only."
        )
    return candidates[0] if candidates else (None, None)


def select_new_bootloader(bootloaders, previous_addresses):
    candidates = [d for d in bootloaders if d.address.casefold() not in previous_addresses]
    if len(candidates) > 1:
        raise RuntimeError(
            "Multiple new DFU devices found; automatic selection stopped. "
            "Use --scan-only, identify the correct bootloader, then --dfu-address."
        )
    return candidates[0] if candidates else None


async def main():
    parser = argparse.ArgumentParser(description="Standalone OTA Flasher for ZZK-SA2 / XIAO BLE")
    parser.add_argument("-z", "--zip", default=".pio/build/xiaoble_adafruit/firmware.zip", help="Path to firmware.zip")
    parser.add_argument("-t", "--timeout", type=float, default=20.0, help="Scan timeout")
    parser.add_argument("--connect-timeout", type=float, default=45.0,
                        help="Connection/GATT discovery timeout in seconds (default: 45)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--address", help="Application device address (MAC on Windows/Linux)")
    target.add_argument("--dfu-address", help="Address of a device already in DFU mode")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scan-only", action="store_true",
                      help="List OTA candidates without connecting or flashing")
    mode.add_argument("--check-only", action="store_true",
                      help="Connect and check DFU service without rebooting or flashing")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.connect_timeout <= 0:
        parser.error("--connect-timeout must be greater than zero")
    if args.scan_only:
        await scan_targets(args.timeout, args.address or args.dfu_address)
        return
    if args.check_only:
        apps, bootloaders = await scan_targets(args.timeout, args.address or args.dfu_address)
        device_app, device_dfu = select_target(apps, bootloaders, args.address, args.dfu_address)
        device = device_app or device_dfu
        if device is None:
            raise RuntimeError("Requested device was not discovered. Release the gateway "
                               "connection and move the PC/Bluetooth antenna closer.")
        await check_connection(device, args.connect_timeout)
        return

    zip_path = args.zip
    if not os.path.isfile(zip_path):
        print(f"❌ Error: Firmware file not found: {zip_path}")
        sys.exit(1)

    # Read zip
    print(f"📦 Extracting package: {zip_path}")
    with zipfile.ZipFile(zip_path, 'r') as zf:
        manifest = json.loads(zf.read("manifest.json"))
        app_info = manifest["manifest"]["application"]
        dat_bytes = zf.read(app_info["dat_file"])
        bin_bytes = zf.read(app_info["bin_file"])

    print("🔎 Scanning for target device (ZZK-SA2 or XIAO_DFU)...")
    apps, bootloaders = await scan_targets(args.timeout, args.address or args.dfu_address)
    device_app, device_dfu = select_target(apps, bootloaders, args.address, args.dfu_address)
    previous_dfu_addresses = {device.address.casefold() for device in bootloaders}

    if device_app is None and device_dfu is None:
        print("Error: The requested switch/bootloader was not discovered.")
        print("Release the gateway connection, move the PC/Bluetooth antenna closer, "
              "then retry with --scan-only --timeout 30.")
        print("If the switch is already in DFU mode, its address may have changed; "
              "select its scanned address with --dfu-address.")
        sys.exit(1)

    # Step 1: Trigger DFU if running application
    if not device_dfu and device_app:
        await trigger_dfu_from_app(device_app, args.connect_timeout)
        print("⏳ Waiting for XIAO_DFU to appear in advertising...")
        await asyncio.sleep(2.0)

        # Scan again for XIAO_DFU
        for _ in range(12):
            _, bootloaders = await scan_targets(2.0)
            device_dfu = select_new_bootloader(bootloaders, previous_dfu_addresses)
            if device_dfu:
                break
            await asyncio.sleep(0.5)

    if not device_dfu:
        print("❌ Could not find XIAO_DFU device! Please ensure the device is in DFU mode.")
        sys.exit(1)

    print(f"🎯 Found Bootloader: {device_dfu.name or '<unnamed>'} ({device_dfu.address})")
    print(f"🔗 Connecting to Bootloader...")

    await flash_and_handle_reset(
        device_dfu, args.connect_timeout, dat_bytes, bin_bytes,
        application_address=device_app.address if device_app else args.address,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ActivationUnconfirmed as error:
        print(f"⚠️ Activation unconfirmed: {error}")
        sys.exit(2)
    except (RuntimeError, BleakError, TimeoutError) as error:
        print(f"Error: {error or type(error).__name__}")
        sys.exit(1)
