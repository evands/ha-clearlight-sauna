"""Phase 3: time to target, ready at, ready-by plans and calibration, against the fake device."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import CONF_HOST, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.clearlight_sauna import heatup as h
from custom_components.clearlight_sauna import protocol
from custom_components.clearlight_sauna.const import CONF_MAC, DOMAIN
from custom_components.clearlight_sauna.estimate import ROOM_IDLE, STORAGE_VERSION, storage_key

from .fake_gagent import FAKE_MAC, FakeGAgent

pytestmark = pytest.mark.usefixtures("socket_enabled")

ENTRY_ID = "heatup_test_entry"
CLIMATE = "climate.clearlight_sauna"
TTT = "sensor.clearlight_sauna_time_to_target"
READY_AT = "sensor.clearlight_sauna_ready_at"
START = "sensor.clearlight_sauna_scheduled_start"
RESET = "button.clearlight_sauna_reset_heat_up_calibration"
POWER = 0x08

TAU = h.DEFAULT_TAU
CEILING = 76 + h.DEFAULT_DELTA_T  # 161 °F: the default model from a 76 °F room


@pytest.fixture(autouse=True)
def _no_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests jump the clock by minutes or hours. A heartbeat sent just before
    a jump would see its 5 s reply timeout expire first and drop the connection."""
    monkeypatch.setattr(protocol, "PING_INTERVAL", 1e7)


def _curve(t: float) -> float:
    return CEILING - (CEILING - 76) * math.exp(-t / TAU)


def _stored(hass_storage, **data) -> None:
    hass_storage[storage_key(ENTRY_ID)] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": storage_key(ENTRY_ID),
        "data": data,
    }


def _cold_room(hass_storage, **extra) -> None:
    """A cabin that has sat at a 76 °F room temperature for long enough."""
    now = dt_util.utcnow().timestamp()
    _stored(hass_storage, room=76.0, room_at=now, idle_since=now - ROOM_IDLE - 60, **extra)


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.config.units = US_CUSTOMARY_SYSTEM  # the device's own °F throughout
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="Clearlight Sauna",
        unique_id=FAKE_MAC,
        data={CONF_HOST: "127.0.0.1", CONF_MAC: FAKE_MAC, "did": "x"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _report(hass: HomeAssistant, fake: FakeGAgent, raw: int) -> None:
    client = hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.client
    fake.status[10] = raw
    for _ in range(3):  # a reply already in flight can answer first
        await client.async_request_status(wait=True)
        if client.state.current_temp == raw:
            break
    await hass.async_block_till_done()


async def _advance(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _heat(hass, fake, freezer, start: float, end: float) -> None:
    """Follow the default model's curve from `start` to `end` seconds, every 15 s."""
    t = start
    while t < end:
        t += h.SAMPLE_INTERVAL
        await _advance(hass, freezer, h.SAMPLE_INTERVAL)
        await _report(hass, fake, round(_curve(t)))


async def _call(hass, service: str, **data) -> None:
    await hass.services.async_call(
        DOMAIN, service, {"entity_id": CLIMATE, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def test_off_shows_planning_estimate(hass: HomeAssistant, patch_client) -> None:
    await _setup(hass)
    # The fake starts off at 76 °F with a 150 °F setpoint; no room measured yet.
    state = hass.states.get(TTT)
    expected = math.ceil(h.eta_seconds(76, 150, CEILING, TAU) / 60)
    assert int(state.state) == expected
    assert state.attributes["approximate"] is True
    assert state.attributes["ceiling_f"] == CEILING
    assert state.attributes["calibration_sessions"] == 0
    assert hass.states.get(READY_AT).state == STATE_UNKNOWN
    assert hass.states.get(START).state == STATE_UNKNOWN


async def test_cold_session_estimates_and_calibrates(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent, freezer, hass_storage
) -> None:
    _cold_room(hass_storage)
    await _setup(hass)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 120}, blocking=True
    )
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    reach = h.eta_seconds(76, 120, CEILING, TAU)  # ~23 min

    await _heat(hass, fake_device, freezer, 0, 240)
    assert hass.states.get(TTT).attributes["approximate"] is True  # under 5 minutes of data

    await _heat(hass, fake_device, freezer, 240, 600)
    state = hass.states.get(TTT)
    assert state.attributes["approximate"] is False
    assert int(state.state) == pytest.approx((reach - 600) / 60, abs=2)
    ready = dt_util.parse_datetime(hass.states.get(READY_AT).state)
    assert ready.timestamp() == pytest.approx(dt_util.utcnow().timestamp() + reach - 600, abs=150)

    await _heat(hass, fake_device, freezer, 600, reach + 30)
    assert hass.states.get(TTT).state == "0"
    reached = hass.states.get(READY_AT).state
    await _heat(hass, fake_device, freezer, reach + 30, reach + 120)
    assert hass.states.get(READY_AT).state == reached  # fixed once reached

    # The session ends: a cold start that ran long enough calibrates.
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(TTT).attributes["calibration_sessions"] == 1
    await _advance(hass, freezer, 1)
    stored = hass_storage[storage_key(ENTRY_ID)]["data"]["calibration"]
    assert stored["sessions"] == 1
    assert stored["delta_t"] == pytest.approx(h.DEFAULT_DELTA_T, abs=1.5)


async def test_warm_start_does_not_calibrate(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent, freezer, hass_storage
) -> None:
    now = dt_util.utcnow().timestamp()
    _stored(hass_storage, room=76.0, room_at=now - 7200, idle_since=now - 600)  # off 10 min
    await _setup(hass)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 120}, blocking=True
    )
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    await _heat(hass, fake_device, freezer, 0, 900)
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(TTT).attributes["calibration_sessions"] == 0


async def test_ready_by_starts_with_heatup_plus_hold_and_restores_length(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent, freezer, hass_storage
) -> None:
    _cold_room(hass_storage)
    await _setup(hass)
    ready_by = (dt_util.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
    await _call(
        hass, "schedule_ready", ready_by=ready_by.strftime("%H:%M:%S"),
        temperature=120, minutes_at_temperature=30,
    )
    heatup = h.eta_seconds(76, 120, CEILING, TAU)
    session = math.ceil(heatup / 60) + 30
    start = dt_util.parse_datetime(hass.states.get(START).state)
    assert start.timestamp() == pytest.approx(ready_by.timestamp() - heatup, abs=60)
    attrs = hass.states.get(START).attributes
    assert attrs["session_minutes"] == session
    assert attrs["target_temperature_f"] == 120
    assert attrs["running"] is False
    assert not fake_device.status[0] & POWER

    # Nothing happens before the start time...
    await _advance(hass, freezer, start.timestamp() - dt_util.utcnow().timestamp() - 30)
    assert not fake_device.status[0] & POWER
    # ...then it starts at the target with heat-up + time at temperature.
    await _advance(hass, freezer, 60)
    assert fake_device.status[0] & POWER
    assert fake_device.status[4] == 120
    assert (fake_device.status[5], fake_device.status[6]) == (0, session)
    assert hass.states.get(START).state == STATE_UNKNOWN
    attrs = hass.states.get(START).attributes
    assert attrs["running"] is True
    # The request stays on offer as the next form's defaults.
    assert attrs["last_ready_by"] == ready_by.strftime("%H:%M:%S")
    assert attrs["last_target_temperature_f"] == 120
    assert attrs["last_minutes_at_temperature"] == 30

    # Stopping puts back the length the controller had (10 min), not the long one.
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 1)
    assert not fake_device.status[0] & POWER
    assert fake_device.status[6] == 10
    assert fake_device.session_length == 10
    assert hass.states.get(START).attributes["running"] is False


async def test_ready_session_timing_out_restores_length(
    hass: HomeAssistant, patch_client, fake_device: FakeGAgent, freezer, hass_storage
) -> None:
    _cold_room(hass_storage)
    await _setup(hass)
    soon = (dt_util.now() + timedelta(minutes=5)).replace(second=0, microsecond=0)
    await _call(
        hass, "schedule_ready", ready_by=soon.strftime("%H:%M:%S"),
        temperature=100, minutes_at_temperature=5,
    )
    # Ready in 5 minutes but the heat-up takes longer: it starts right away.
    await _advance(hass, freezer, h.SAMPLE_INTERVAL)
    assert fake_device.status[0] & POWER
    client = hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.client
    await client.async_request_status(wait=True)
    await hass.async_block_till_done()

    # The timer reaches 0:00; the controller keeps its power flag set.
    fake_device.status[6] = 0
    await _report(hass, fake_device, 100)
    await _advance(hass, freezer, 1)
    assert not fake_device.status[0] & POWER  # cleared, heaters were already off
    assert fake_device.status[6] == 10


async def test_unreachable_target_is_refused(
    hass: HomeAssistant, patch_client, hass_storage
) -> None:
    _cold_room(hass_storage)
    await _setup(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(  # the default model levels off at 161 °F from a 76 °F room
            hass, "schedule_ready", ready_by="07:00:00", temperature=175,
            minutes_at_temperature=30,
        )
    assert err.value.translation_key == "target_out_of_reach"
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "schedule_ready", ready_by="07:00:00", temperature=200,
            minutes_at_temperature=30,
        )
    assert err.value.translation_key == "temperature_out_of_range"
    assert hass.states.get(START).state == STATE_UNKNOWN


async def test_plan_survives_reload_and_can_be_cancelled(
    hass: HomeAssistant, patch_client, hass_storage
) -> None:
    _cold_room(hass_storage)
    entry = await _setup(hass)
    await _call(
        hass, "schedule_ready", ready_by="06:30:00", temperature=130, minutes_at_temperature=45
    )
    before = hass.states.get(START).state
    assert before != STATE_UNKNOWN

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(START).state == before
    assert hass.states.get(START).attributes["last_minutes_at_temperature"] == 45

    await _call(hass, "cancel_ready")
    assert hass.states.get(START).state == STATE_UNKNOWN


async def test_reset_button_restores_default_model(
    hass: HomeAssistant, patch_client, hass_storage
) -> None:
    _cold_room(
        hass_storage,
        calibration={"tau": 1500.0, "delta_t": 70.0, "sessions": 3, "tau_sessions": 1},
    )
    await _setup(hass)
    attrs = hass.states.get(TTT).attributes
    assert attrs["calibration_sessions"] == 3 and attrs["delta_t_f"] == 70.0

    await hass.services.async_call("button", "press", {"entity_id": RESET}, blocking=True)
    await hass.async_block_till_done()
    attrs = hass.states.get(TTT).attributes
    assert attrs["calibration_sessions"] == 0
    assert attrs["delta_t_f"] == h.DEFAULT_DELTA_T
    assert attrs["time_constant_min"] == round(h.DEFAULT_TAU / 60, 1)


async def test_schedule_takes_the_users_temperature_unit(
    hass: HomeAssistant, patch_client, hass_storage
) -> None:
    _cold_room(hass_storage)
    await _setup(hass)
    hass.config.units = METRIC_SYSTEM
    await _call(hass, "schedule_ready", ready_by="06:30:00", temperature=49, minutes_at_temperature=30)
    assert hass.states.get(START).attributes["target_temperature_f"] == 120  # 49 °C
