"""Local LAN client for Clearlight (Gizwits GAgent) sauna controllers.

Clearlight Sanctuary saunas on the 2.4 GHz "Clearlight Sauna Connect" app use an
ESP8266 running Gizwits GAgent 4.x. The module exposes a LAN control port
(TCP 12416) and answers discovery broadcasts on UDP 12414. No cloud is needed.

This module has no Home Assistant dependencies so it can be tested standalone.
Kept compatible with Python 3.9 for bench testing on macOS's system Python.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

TCP_PORT = 12416
UDP_DISCOVERY_PORT = 12414
CLEARLIGHT_PRODUCT_KEY = "4bde5ccdff4a48c0840e055204a71e1f"

PREFIX = b"\x00\x00\x00\x03"

# LAN commands
CMD_DISCOVER = 0x0003
CMD_DISCOVER_REPLY = 0x0004
CMD_PASSCODE = 0x0006
CMD_PASSCODE_REPLY = 0x0007
CMD_LOGIN = 0x0008
CMD_LOGIN_REPLY = 0x0009
CMD_MODULE_INFO = 0x0013
CMD_MODULE_INFO_REPLY = 0x0014
CMD_PING = 0x0015
CMD_PONG = 0x0016
CMD_TRANSMIT = 0x0090
CMD_TRANSMIT_REPLY = 0x0091
CMD_CONTROL = 0x0093
CMD_CONTROL_REPLY = 0x0094

# P0 actions
P0_WRITE = 0x01
P0_READ = 0x02
P0_READ_REPLY = 0x03
P0_REPORT = 0x04

PING_INTERVAL = 4.0
RESPONSE_TIMEOUT = 5.0
STATUS_LEN = 12  # bytes of packed datapoints after the action byte
WRITABLE_LEN = 10  # writable datapoints occupy status bytes 0..9
FLAG_BYTES = 2  # 16 writable datapoints -> 2 flag bytes

# Datapoint ids from the product's Gizwits datapoint definition
# (usapi.gizwits.com/app/datapoint?product_key=4bde5ccd...). Bools live in
# byte 0 at bit_offset == id; uint8 values at their own byte offsets.
DP_EXTERNAL_LIGHT = 0
DP_INTERNAL_LIGHT = 1
DP_PRE_TIME_FLAG = 2
DP_POWER = 3
DP_CF = 4
DP_UVB = 5
DP_N = 6
DP_LED = 7
DP_RIGHT = 8
DP_LEFT = 9
DP_SET_TEMP = 10
DP_SET_HOUR = 11
DP_SET_MINUTE = 12
DP_PRE_TIME_HOUR = 13
DP_PRE_TIME_MINUTE = 14
DP_SN = 15

BOOL_BITS = {
    DP_EXTERNAL_LIGHT: 0,
    DP_INTERNAL_LIGHT: 1,
    DP_PRE_TIME_FLAG: 2,
    DP_POWER: 3,
    DP_CF: 4,
    DP_UVB: 5,
    DP_N: 6,
}
UINT8_BYTES = {
    DP_LED: 1,
    DP_RIGHT: 2,
    DP_LEFT: 3,
    DP_SET_TEMP: 4,
    DP_SET_HOUR: 5,
    DP_SET_MINUTE: 6,
    DP_PRE_TIME_HOUR: 7,
    DP_PRE_TIME_MINUTE: 8,
    DP_SN: 9,
}


class GizwitsError(Exception):
    """Base error."""


class GizwitsConnectionError(GizwitsError):
    """Connection failed or dropped."""


class GizwitsAuthError(GizwitsError):
    """The module refused the passcode."""


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    body = b"\x00" + struct.pack(">H", cmd) + payload
    return PREFIX + encode_varint(len(body)) + body


def parse_frames(buf: bytes) -> Tuple[List[Tuple[int, bytes]], bytes]:
    """Split a byte stream into (cmd, payload) frames; return leftover bytes."""
    frames: List[Tuple[int, bytes]] = []
    while len(buf) >= 4:
        if buf[:4] != PREFIX:
            # Resync on the next prefix; drop garbage.
            idx = buf.find(PREFIX, 1)
            if idx < 0:
                return frames, b""
            buf = buf[idx:]
            continue
        length = 0
        shift = 0
        i = 4
        while True:
            if i >= len(buf):
                return frames, buf
            byte = buf[i]
            length |= (byte & 0x7F) << shift
            shift += 7
            i += 1
            if not byte & 0x80:
                break
            if shift > 28:
                raise GizwitsError("bad length varint")
        if len(buf) < i + length:
            return frames, buf
        body = buf[i : i + length]
        buf = buf[i + length :]
        if len(body) < 3:
            continue
        frames.append((struct.unpack(">H", body[1:3])[0], bytes(body[3:])))
    return frames, buf


def _lp_string(data: bytes, pos: int) -> Tuple[bytes, int]:
    (length,) = struct.unpack(">H", data[pos : pos + 2])
    pos += 2
    return data[pos : pos + length], pos + length


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SaunaState:
    """Decoded datapoints. Field semantics marked (?) are not yet verified."""

    raw: bytes
    external_light: bool
    internal_light: bool
    reservation_active: bool
    power: bool
    celsius: bool  # cf_flag (display unit); reported values stay °F either way
    uvb: bool  # (?)
    n_flag: bool  # (?)
    led: int  # (?) chromotherapy color
    right: int  # (?) observed 100; likely full-spectrum intensity
    left: int  # (?)
    set_temp: int
    set_hour: int
    set_minute: int
    pre_time_hour: int
    pre_time_minute: int
    sn: int
    current_temp: int
    heart_pulse: int

    @property
    def session_minutes(self) -> int:
        return self.set_hour * 60 + self.set_minute

    @classmethod
    def from_bytes(cls, data: bytes) -> "SaunaState":
        if len(data) < STATUS_LEN:
            raise GizwitsError("status too short: %s" % data.hex())
        data = bytes(data[:STATUS_LEN])
        b0 = data[0]

        def bit(dp: int) -> bool:
            return bool(b0 >> BOOL_BITS[dp] & 1)

        return cls(
            raw=data,
            external_light=bit(DP_EXTERNAL_LIGHT),
            internal_light=bit(DP_INTERNAL_LIGHT),
            reservation_active=bit(DP_PRE_TIME_FLAG),
            power=bit(DP_POWER),
            celsius=bit(DP_CF),
            uvb=bit(DP_UVB),
            n_flag=bit(DP_N),
            led=data[1],
            right=data[2],
            left=data[3],
            set_temp=data[4],
            set_hour=data[5],
            set_minute=data[6],
            pre_time_hour=data[7],
            pre_time_minute=data[8],
            sn=data[9],
            current_temp=data[10],
            heart_pulse=data[11],
        )


def build_write_p0(current: SaunaState, changes: Dict[int, int]) -> bytes:
    """Build a P0 write: action, attr flags, then all writable values.

    Every unflagged value is copied from the current state, so a controller
    that ignores the flag mask still sees no change for those fields.
    """
    vals = bytearray(current.raw[:WRITABLE_LEN])
    flags = 0
    for dp, value in changes.items():
        if dp in BOOL_BITS:
            mask = 1 << BOOL_BITS[dp]
            vals[0] = (vals[0] | mask) if value else (vals[0] & ~mask & 0xFF)
        elif dp in UINT8_BYTES:
            if not 0 <= int(value) <= 255:
                raise ValueError("datapoint %d out of range: %r" % (dp, value))
            vals[UINT8_BYTES[dp]] = int(value)
        else:
            raise ValueError("datapoint %d is not writable" % dp)
        flags |= 1 << dp
    return bytes([P0_WRITE]) + flags.to_bytes(FLAG_BYTES, "big") + bytes(vals)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredDevice:
    host: str
    did: str
    mac: str
    product_key: str
    api_server: str
    gagent_version: str


def parse_discovery_reply(payload: bytes, host: str) -> DiscoveredDevice:
    pos = 0
    did, pos = _lp_string(payload, pos)
    mac_raw, pos = _lp_string(payload, pos)
    _fw, pos = _lp_string(payload, pos)
    product_key, pos = _lp_string(payload, pos)
    pos += 8  # MCU attributes
    rest = payload[pos:].split(b"\x00")
    api_server = rest[0].decode(errors="replace") if rest else ""
    version = rest[1].decode(errors="replace") if len(rest) > 1 else ""
    if len(mac_raw) == 6:
        mac = ":".join("%02x" % b for b in mac_raw)
    else:
        mac = mac_raw.decode(errors="replace").lower()
    return DiscoveredDevice(
        host=host,
        did=did.decode(errors="replace"),
        mac=mac,
        product_key=product_key.decode(errors="replace"),
        api_server=api_server,
        gagent_version=version,
    )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: Dict[str, DiscoveredDevice] = {}

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            frames, _ = parse_frames(data)
            for cmd, payload in frames:
                if cmd in (CMD_DISCOVER_REPLY, 0x0005):
                    dev = parse_discovery_reply(payload, addr[0])
                    self.found[dev.mac] = dev
        except Exception:  # noqa: BLE001 - ignore foreign datagrams
            _LOGGER.debug("ignoring datagram from %s: %s", addr, data.hex())


async def async_discover(
    timeout: float = 3.0,
    targets: Tuple[str, ...] = ("255.255.255.255",),
    product_key: Optional[str] = CLEARLIGHT_PRODUCT_KEY,
) -> List[DiscoveredDevice]:
    """Broadcast a GAgent discovery request and collect replies."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    transport, proto = await loop.create_datagram_endpoint(_DiscoveryProtocol, sock=sock)
    try:
        packet = build_frame(CMD_DISCOVER)
        for _ in range(2):
            for target in targets:
                transport.sendto(packet, (target, UDP_DISCOVERY_PORT))
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()
    devices = list(proto.found.values())
    if product_key:
        devices = [d for d in devices if d.product_key == product_key]
    return devices


# --------------------------------------------------------------------------
# TCP client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleInfo:
    wifi_hw: str
    wifi_sw: str
    mcu_hw: str
    mcu_sw: str
    p0_version: str
    product_key: str


def parse_module_info(payload: bytes) -> ModuleInfo:
    fields = [payload[i : i + 8].decode(errors="replace") for i in range(0, 40, 8)]
    pos = 48  # five 8-byte strings + 8-byte firmware id
    _fw, pos = _lp_string(payload, pos)
    pk, pos = _lp_string(payload, pos)
    return ModuleInfo(*fields, product_key=pk.decode(errors="replace"))


class ClearlightClient:
    """Persistent connection with login, heartbeat, push updates and writes."""

    def __init__(
        self,
        host: str,
        port: int = TCP_PORT,
        on_state: Optional[Callable[[SaunaState], None]] = None,
        on_connection: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self._on_state = on_state
        self._on_connection = on_connection
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._buf = b""
        self._waiters: Dict[int, List[asyncio.Future]] = {}
        self._control_waiters: Dict[int, asyncio.Future] = {}
        self._tasks: List[asyncio.Task] = []
        self._run_task: Optional[asyncio.Task] = None
        self._seq = 0
        self._write_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self.state: Optional[SaunaState] = None
        self.module_info: Optional[ModuleInfo] = None
        self.connected = False

    def set_callbacks(
        self,
        on_state: Optional[Callable[[SaunaState], None]],
        on_connection: Optional[Callable[[bool], None]],
    ) -> None:
        self._on_state = on_state
        self._on_connection = on_connection

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        """Open the socket, log in and request a first status."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), RESPONSE_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise GizwitsConnectionError(
                "cannot reach %s:%s: %s" % (self.host, self.port, err)
            ) from err
        self._buf = b""
        self._tasks = [asyncio.ensure_future(self._read_loop())]
        try:
            payload = await self._request(CMD_PASSCODE, b"", CMD_PASSCODE_REPLY)
            passcode, _ = _lp_string(payload, 0)
            if not passcode:
                raise GizwitsAuthError("module returned an empty passcode")
            reply = await self._request(
                CMD_LOGIN, struct.pack(">H", len(passcode)) + passcode, CMD_LOGIN_REPLY
            )
            if not reply or reply[0] != 0:
                raise GizwitsAuthError("login rejected")
            info = await self._request(CMD_MODULE_INFO, b"", CMD_MODULE_INFO_REPLY)
            self.module_info = parse_module_info(info)
            self.connected = True
            self._connected_event.set()
            self._tasks.append(asyncio.ensure_future(self._ping_loop()))
            try:
                await self.async_request_status(wait=True)
            except asyncio.TimeoutError:
                _LOGGER.debug("Clearlight %s: no initial status yet", self.host)
        except BaseException:
            await self._teardown()
            raise
        if self._on_connection:
            self._on_connection(True)

    async def disconnect(self) -> None:
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._run_task = None
        await self._teardown()

    def start(self) -> None:
        """Keep a connection up in the background, reconnecting with backoff."""
        if self._run_task is None:
            self._run_task = asyncio.ensure_future(self._run_forever())

    async def _run_forever(self) -> None:
        delay = 2.0
        while True:
            try:
                if not self.connected:
                    await self.connect()
                    delay = 2.0
                await self._wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Clearlight %s connection error: %s", self.host, err)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _wait_closed(self) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)

    async def _teardown(self) -> None:
        was_connected = self.connected
        self.connected = False
        self._connected_event.clear()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        self._tasks = []
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None
        err = GizwitsConnectionError("connection closed")
        for futs in self._waiters.values():
            for fut in futs:
                if not fut.done():
                    fut.set_exception(err)
        self._waiters.clear()
        for fut in self._control_waiters.values():
            if not fut.done():
                fut.set_exception(err)
        self._control_waiters.clear()
        if was_connected and self._on_connection:
            self._on_connection(False)

    # ---- I/O -------------------------------------------------------------

    def _send(self, cmd: int, payload: bytes = b"") -> None:
        if self._writer is None:
            raise GizwitsConnectionError("not connected")
        self._writer.write(build_frame(cmd, payload))

    async def _request(self, cmd: int, payload: bytes, reply_cmd: int) -> bytes:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(reply_cmd, []).append(fut)
        self._send(cmd, payload)
        await self._writer.drain()  # type: ignore[union-attr]
        try:
            return await asyncio.wait_for(fut, RESPONSE_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise GizwitsConnectionError("no reply to 0x%04x" % cmd) from err
        finally:
            futs = self._waiters.get(reply_cmd, [])
            if fut in futs:
                futs.remove(fut)

    async def _read_loop(self) -> None:
        try:
            while True:
                data = await self._reader.read(4096)  # type: ignore[union-attr]
                if not data:
                    raise GizwitsConnectionError("closed by device")
                self._buf += data
                frames, self._buf = parse_frames(self._buf)
                for cmd, payload in frames:
                    self._dispatch(cmd, payload)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Clearlight %s read loop ended: %s", self.host, err)
        finally:
            await self._teardown()

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await self._request(CMD_PING, b"", CMD_PONG)
            except GizwitsError:
                _LOGGER.debug("Clearlight %s heartbeat failed", self.host)
                await self._teardown()
                return

    def _dispatch(self, cmd: int, payload: bytes) -> None:
        if cmd in (CMD_TRANSMIT_REPLY, CMD_CONTROL_REPLY):
            p0 = payload
            if cmd == CMD_CONTROL_REPLY:
                if len(payload) < 4:
                    return
                (seq,) = struct.unpack(">I", payload[:4])
                fut = self._control_waiters.pop(seq, None)
                if fut is not None and not fut.done():
                    fut.set_result(payload[4:])
                p0 = payload[4:]
            if p0 and p0[0] in (P0_READ_REPLY, P0_REPORT) and len(p0) > STATUS_LEN:
                self._handle_status(p0[1 : 1 + STATUS_LEN])
            return
        futs = self._waiters.get(cmd)
        if futs:
            fut = futs.pop(0)
            if not fut.done():
                fut.set_result(payload)

    def _handle_status(self, data: bytes) -> None:
        try:
            state = SaunaState.from_bytes(data)
        except GizwitsError:
            return
        changed = self.state is None or state.raw != self.state.raw
        self.state = state
        for fut in self._waiters.pop(-1, []):
            if not fut.done():
                fut.set_result(data)
        if changed and self._on_state:
            self._on_state(state)

    # ---- public API ------------------------------------------------------

    async def async_request_status(self, wait: bool = False) -> Optional[SaunaState]:
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.setdefault(-1, []).append(fut)
        self._send(CMD_TRANSMIT, bytes([P0_READ]))
        await self._writer.drain()  # type: ignore[union-attr]
        if wait:
            try:
                await asyncio.wait_for(fut, RESPONSE_TIMEOUT)
            finally:
                waiters = self._waiters.get(-1, [])
                if fut in waiters:
                    waiters.remove(fut)
        return self.state

    async def async_write(self, changes: Dict[int, int]) -> None:
        """Write datapoints. Values not in `changes` keep their current value."""
        if not self.connected:
            raise GizwitsConnectionError("not connected")
        if self.state is None:
            # Never write blind: unflagged fields are copied from current state.
            try:
                await self.async_request_status(wait=True)
            except asyncio.TimeoutError as err:
                raise GizwitsConnectionError("no status; refusing to write") from err
            if self.state is None:
                raise GizwitsConnectionError("no status; refusing to write")
        async with self._write_lock:
            p0 = build_write_p0(self.state, changes)
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
            fut = asyncio.get_running_loop().create_future()
            self._control_waiters[seq] = fut
            self._send(CMD_CONTROL, struct.pack(">I", seq) + p0)
            await self._writer.drain()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(fut, RESPONSE_TIMEOUT)
            except asyncio.TimeoutError:
                # Some firmwares don't ack 0x93; fall back to a status read.
                self._control_waiters.pop(seq, None)
                _LOGGER.debug("Clearlight %s: no ack for write %d", self.host, seq)
            # Optimistically reflect the write until the device reports back.
            vals = build_write_p0(self.state, changes)[1 + FLAG_BYTES :]
            optimistic = SaunaState.from_bytes(vals + self.state.raw[WRITABLE_LEN:])
            self.state = optimistic
            if self._on_state:
                self._on_state(optimistic)
            await self.async_request_status()

