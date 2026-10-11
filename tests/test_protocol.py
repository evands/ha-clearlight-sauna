"""Protocol tests against bytes captured from the real sauna and a fake GAgent."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.clearlight_sauna import protocol as p

from .fake_gagent import (
    DISCOVERY_REPLY,
    FAKE_DID,
    FAKE_MAC,
    INITIAL_STATUS,
    MODULE_INFO,
    FakeGAgent,
)

# Tests talk to a fake GAgent on 127.0.0.1.
pytestmark = pytest.mark.usefixtures("socket_enabled")


def test_varint_roundtrip() -> None:
    for n in (0, 3, 0x7A, 127, 128, 300, 16383, 16384, 2_000_000):
        frame = p.PREFIX + p.encode_varint(n + 3) + b"\x00\x00\x15" + b"x" * n
        frames, rest = p.parse_frames(frame)
        assert rest == b""
        assert frames == [(0x15, b"x" * n)]


def test_build_frame_matches_capture() -> None:
    assert p.build_frame(p.CMD_DISCOVER).hex() == "0000000303000003"
    assert p.build_frame(p.CMD_TRANSMIT, b"\x02").hex() == "000000030400009002"


def test_parse_handles_partial_and_concatenated_frames() -> None:
    a = p.build_frame(p.CMD_PONG)
    b = p.build_frame(p.CMD_TRANSMIT_REPLY, b"\x04" + INITIAL_STATUS)
    stream = a + b
    frames, rest = p.parse_frames(stream[:5])
    assert frames == [] and rest == stream[:5]
    frames, rest = p.parse_frames(b"junk" + stream)
    assert [c for c, _ in frames] == [p.CMD_PONG, p.CMD_TRANSMIT_REPLY]
    assert rest == b""


def test_decode_captured_status() -> None:
    s = p.SaunaState.from_bytes(INITIAL_STATUS)
    assert not s.power and not s.internal_light and not s.external_light
    assert not s.celsius
    assert s.set_temp == 150
    assert s.current_temp == 76
    assert (s.right, s.left) == (100, 100)
    assert s.session_minutes == 10
    assert not s.aux
    assert p.SaunaState.from_bytes(bytes([0x28]) + INITIAL_STATUS[1:]).aux


def test_write_payload_flags_and_padding() -> None:
    s = p.SaunaState.from_bytes(INITIAL_STATUS)
    p0 = p.build_write_p0(s, {p.DP_INTERNAL_LIGHT: 1})
    assert p0[0] == p.P0_WRITE
    assert p0[1:3] == b"\x00\x02"  # bit for datapoint id 1, big-endian
    assert p0[3] == 0b0000_0010
    assert p0[4:13] == INITIAL_STATUS[1:10]  # everything else copied verbatim

    p0 = p.build_write_p0(s, {p.DP_POWER: 1, p.DP_SET_TEMP: 140})
    assert int.from_bytes(p0[1:3], "big") == (1 << 3) | (1 << 10)
    assert p0[3] & 0x08 and p0[3 + 4] == 140

    with pytest.raises(ValueError):
        p.build_write_p0(s, {p.DP_SET_TEMP: 300})
    with pytest.raises(ValueError):
        p.build_write_p0(s, {16: 1})  # CURRENT_TEMP is read-only


def test_parse_discovery_capture() -> None:
    d = p.parse_discovery_reply(DISCOVERY_REPLY, "192.0.2.10")
    assert d.did == FAKE_DID
    assert d.mac == FAKE_MAC
    assert d.product_key == p.CLEARLIGHT_PRODUCT_KEY
    assert d.api_server == "usapi.gizwits.com:80"
    assert d.gagent_version == "4.1.2"


def test_parse_module_info_capture() -> None:
    info = p.parse_module_info(MODULE_INFO)
    assert info.wifi_hw == "00ESP826"
    assert info.p0_version == "00000004"
    assert info.product_key == p.CLEARLIGHT_PRODUCT_KEY


async def test_client_connect_read_write() -> None:
    dev = FakeGAgent()
    await dev.start()
    states: list[p.SaunaState] = []
    client = p.ClearlightClient("127.0.0.1", dev.port, on_state=states.append)
    try:
        await client.connect()
        assert dev.logged_in
        await client.async_request_status(wait=True)
        assert client.state is not None and client.state.set_temp == 150

        await client.async_write({p.DP_INTERNAL_LIGHT: 1})
        await client.async_request_status(wait=True)
        assert client.state.internal_light
        assert not client.state.power
        assert dev.status[1:10] == INITIAL_STATUS[1:10]

        await client.async_write({p.DP_SET_TEMP: 140})
        await client.async_request_status(wait=True)
        assert client.state.set_temp == 140 and client.state.internal_light
    finally:
        await client.disconnect()
        await dev.stop()


async def test_client_tolerates_missing_write_ack(monkeypatch) -> None:
    monkeypatch.setattr(p, "RESPONSE_TIMEOUT", 0.3)
    dev = FakeGAgent(ack_writes=False)
    await dev.start()
    client = p.ClearlightClient("127.0.0.1", dev.port)
    try:
        await client.connect()
        await client.async_write({p.DP_EXTERNAL_LIGHT: 1})
        await client.async_request_status(wait=True)
        assert client.state.external_light
    finally:
        await client.disconnect()
        await dev.stop()


async def test_background_reconnect(monkeypatch) -> None:
    dev = FakeGAgent()
    await dev.start()
    events: list[bool] = []
    client = p.ClearlightClient("127.0.0.1", dev.port, on_connection=events.append)
    client.start()
    try:
        for _ in range(50):
            if client.connected:
                break
            await asyncio.sleep(0.05)
        assert client.connected
        await dev.drop_clients()
        for _ in range(50):
            if not client.connected:
                break
            await asyncio.sleep(0.05)
        assert not client.connected
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.05)
        assert client.connected
        assert events[:3] == [True, False, True]
    finally:
        await client.disconnect()
        await dev.stop()


async def test_connect_refused() -> None:
    client = p.ClearlightClient("127.0.0.1", 1)
    with pytest.raises(p.GizwitsConnectionError):
        await client.connect()
