"""A minimal fake Gizwits GAgent for tests, seeded with bytes captured from a real sauna."""

from __future__ import annotations

import asyncio
import struct

from custom_components.clearlight_sauna import protocol as p

# Frame layouts and status bytes match a real controller (GAgent 4.1.2, P0 v4).
# Device identity (DID, MAC, passcode) is synthetic: the real values, combined,
# are enough to bind the sauna to a Gizwits cloud account.
FAKE_DID = "TestDevice000000000000"
FAKE_MAC = "bc:ff:4d:00:00:01"
PASSCODE = b"TESTPASSWD"


def _lp(data: bytes) -> bytes:
    return struct.pack(">H", len(data)) + data


MODULE_INFO = bytes.fromhex(
    "3030455350383236" "3034303230303341" "3030303030303031" "3030303030303031"
    "3030303030303034" "0000000000000000" "0000"
    "0020" + "4bde5ccdff4a48c0840e055204a71e1f".encode().hex()
)
INITIAL_STATUS = bytes.fromhex("0000646496000a0000004c00")
DISCOVERY_REPLY = (
    _lp(FAKE_DID.encode())
    + _lp(bytes.fromhex(FAKE_MAC.replace(":", "")))
    + _lp(b"0402003A")
    + _lp(p.CLEARLIGHT_PRODUCT_KEY.encode())
    + b"\x00" * 8
    + b"usapi.gizwits.com:80\x00"
    + b"4.1.2\x00"
    + b"00000001"
)


class FakeGAgent:
    def __init__(self, ack_writes: bool = True) -> None:
        self.status = bytearray(INITIAL_STATUS)
        self.session_length = INITIAL_STATUS[6]
        self.writes: list[bytes] = []
        self.logged_in = False
        self.ack_writes = ack_writes
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.clients: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for w in self.clients:
            w.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def drop_clients(self) -> None:
        for w in self.clients:
            w.close()
        self.clients.clear()

    def _apply_write(self, p0: bytes) -> None:
        assert p0[0] == p.P0_WRITE
        flags = int.from_bytes(p0[1:3], "big")
        vals = p0[3:13]
        for dp, bit in p.BOOL_BITS.items():
            if flags >> dp & 1:
                mask = 1 << bit
                self.status[0] = (self.status[0] & ~mask & 0xFF) | (vals[0] & mask)
        for dp, off in p.UINT8_BYTES.items():
            if flags >> dp & 1:
                self.status[off] = vals[off]
        if flags >> p.DP_SET_MINUTE & 1 and not self.status[0] & 0x08:
            # Set while off, the session time becomes the configured length.
            self.session_length = vals[6]
        if flags >> p.DP_POWER & 1 and not vals[0] & 0x08:
            # Real controller: power off resets the timer to the configured length.
            self.status[5], self.status[6] = 0, self.session_length

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.append(writer)
        buf = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                buf += data
                frames, buf = p.parse_frames(buf)
                for cmd, payload in frames:
                    if cmd == p.CMD_PASSCODE:
                        out = p.build_frame(p.CMD_PASSCODE_REPLY, struct.pack(">H", len(PASSCODE)) + PASSCODE)
                    elif cmd == p.CMD_LOGIN:
                        ok = payload[2:] == PASSCODE
                        self.logged_in = ok
                        out = p.build_frame(p.CMD_LOGIN_REPLY, b"\x00" if ok else b"\x01")
                    elif cmd == p.CMD_MODULE_INFO:
                        out = p.build_frame(p.CMD_MODULE_INFO_REPLY, MODULE_INFO)
                    elif cmd == p.CMD_PING:
                        out = p.build_frame(p.CMD_PONG)
                    elif cmd == p.CMD_TRANSMIT and payload == b"\x02":
                        out = p.build_frame(p.CMD_TRANSMIT_REPLY, b"\x04" + bytes(self.status))
                    elif cmd == p.CMD_CONTROL:
                        seq, p0 = payload[:4], payload[4:]
                        self.writes.append(p0)
                        self._apply_write(p0)
                        out = b""
                        if self.ack_writes:
                            out += p.build_frame(p.CMD_CONTROL_REPLY, seq)
                        out += p.build_frame(p.CMD_TRANSMIT_REPLY, b"\x04" + bytes(self.status))
                    else:
                        continue
                    writer.write(out)
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            return
