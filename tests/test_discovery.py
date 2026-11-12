"""GX discovery: ID 0, gaps, short area status, manual IDs, 1.x migration."""

from __future__ import annotations

import struct

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ict_automation.const import (
    CONF_DISCOVERY_GAP,
    CONF_LEGACY_KEYS,
    CONF_MANUAL_RECORDS,
    CONF_PIN,
    DOMAIN,
)
from custom_components.ict_automation.hub import parse_id_list
from custom_components.ict_automation.protege import AreaState, ProtegeClient, RecordType
from custom_components.ict_automation.protege.models import decode_status
from custom_components.ict_automation.protege.protocol import GROUP_AREA, ICTDataBlock

from .fake_panel import FakePanel


def _area_block(index: int, body_tail: bytes) -> ICTDataBlock:
    return ICTDataBlock(0x00, GROUP_AREA, struct.pack("<I", index) + body_tail)


def test_short_area_status_decodes_armed_and_disarmed():
    """GX replies omit the tamper byte. 1.7.0 threw on body[6]; 2.2.0 required 7 bytes."""
    missing = decode_status(_area_block(1, b""))
    assert missing is None

    disarmed = decode_status(_area_block(1, bytes((0x00, 0x40))))
    assert disarmed is not None
    assert disarmed.index == 1
    assert disarmed.status.state is AreaState.DISARMED
    assert disarmed.status.partial_armed
    assert not disarmed.status.armed

    armed = decode_status(_area_block(1, bytes((0x80,))))
    assert armed is not None and armed.status.armed

    # The 2014 layout still decodes: state, tamper, flags, spare.
    full = decode_status(_area_block(1, bytes((0x80, 0x00, 0x01, 0x00))))
    assert full is not None
    assert full.status.armed and full.status.alarm


def test_parse_id_list_accepts_ranges_and_names():
    assert parse_id_list("0, 12-14, 19=Garage light") == [
        (0, None),
        (12, None),
        (13, None),
        (14, None),
        (19, "Garage light"),
    ]
    assert parse_id_list("5=Second, 5=First") == [(5, "First")]
    with pytest.raises(ValueError):
        parse_id_list("nope")


def _gx_panel(panel: FakePanel) -> None:
    """Door 0, area 1, inputs 1-7 and 12-20 (8-11 empty), outputs from 19."""
    panel.doors = {0: [0, 0]}
    panel.areas = {1: [0x00, 0x00, 0x00]}
    panel.inputs = {
        i: [f"CP001:{i + 1:02d}", 0, 0] for i in range(1, 8)
    }
    panel.inputs.update({i: [f"EX001:{i - 11:02d}", 0, 0] for i in range(12, 21)})
    panel.outputs = {19: ["CP001:01", 0], 20: ["CP001:02", 0]}
    panel.troubles = {}


async def test_discovery_finds_id_zero_and_walks_gaps(panel: FakePanel):
    _gx_panel(panel)
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    keys = {r.key for r in records}
    assert (RecordType.DOOR, 0) in keys
    assert (RecordType.AREA, 1) in keys
    assert {(RecordType.INPUT, i) for i in range(1, 8)} <= keys
    assert {(RecordType.INPUT, i) for i in range(12, 21)} <= keys
    assert {(RecordType.OUTPUT, 19), (RecordType.OUTPUT, 20)} <= keys
    assert not keys & {(RecordType.INPUT, i) for i in (0, 8, 9, 10, 11)}
    assert (RecordType.OUTPUT, 1) not in keys
    door = next(r for r in records if r.key == (RecordType.DOOR, 0))
    assert door.default_name == "Door 0"
    zone = next(r for r in records if r.key == (RecordType.INPUT, 1))
    assert zone.reference == "CP001:02"
    assert zone.default_name == "Input CP001:02"
    assert client.status[(RecordType.AREA, 1)].armed is False
    await client.disconnect()


async def test_small_gap_still_probes_pinned_ids(panel: FakePanel):
    _gx_panel(panel)
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover(
        gap_tolerance=3,
        extra=[(RecordType.OUTPUT, 19), (RecordType.INPUT, 15)],
    )
    keys = {r.key for r in records}
    assert (RecordType.DOOR, 0) in keys
    assert (RecordType.INPUT, 7) in keys
    assert (RecordType.INPUT, 12) not in keys  # past the 3-miss hole, and not pinned
    assert (RecordType.INPUT, 15) in keys
    assert (RecordType.OUTPUT, 19) in keys
    assert (RecordType.OUTPUT, 20) not in keys
    await client.disconnect()


async def test_area_status_from_monitor_when_status_request_is_refused(panel: FakePanel):
    panel.doors = {0: [0, 0]}
    panel.areas = {1: [0x00, 0x00, 0x00]}
    panel.inputs = {}
    panel.outputs = {}
    panel.troubles = {}
    panel.area_status = "reject"
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover(gap_tolerance=4)
    assert (RecordType.AREA, 1) in {r.key for r in records}
    assert client.status[(RecordType.AREA, 1)].armed is False
    panel.areas[1] = [0x80, 0x00, 0x00]
    await client.start(records)
    # Monitoring pushes the armed state even though status requests are refused.
    panel.push(GROUP_AREA, 1)
    from .test_client import _wait_until

    await _wait_until(lambda: client.status[(RecordType.AREA, 1)].armed)
    await client.stop()


async def test_short_area_status_reports_disarmed_then_armed(panel: FakePanel):
    panel.doors = {}
    panel.areas = {1: [0x00, 0x00, 0x00]}
    panel.inputs = {}
    panel.outputs = {}
    panel.troubles = {}
    panel.area_status = "short"
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover(gap_tolerance=4)
    assert client.status[(RecordType.AREA, 1)].state is AreaState.DISARMED
    await client.start(records)
    panel.areas[1] = [0x80, 0x00, 0x01]
    panel.push(GROUP_AREA, 1)
    from .test_client import _wait_until

    await _wait_until(lambda: client.status[(RecordType.AREA, 1)].alarm)
    assert client.status[(RecordType.AREA, 1)].armed
    await client.stop()


async def test_names_and_alarm_state_on_a_gapped_panel(hass: HomeAssistant, panel: FakePanel) -> None:
    _gx_panel(panel)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="00A1B2C3",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, CONF_PIN: "1234"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("lock.door_0").state == "locked"
    assert hass.states.get("binary_sensor.input_cp001_02").state == "off"
    alarm = hass.states.get("alarm_control_panel.area_1")
    assert alarm is not None
    assert alarm.state == "disarmed"

    panel.areas[1] = [0x80, 0x00, 0x00]
    panel.push(GROUP_AREA, 1)
    from .test_init import _settle

    await _settle(hass, lambda: hass.states.get("alarm_control_panel.area_1").state == "armed_away")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_v1_migration_keeps_entities_and_pins_records(hass: HomeAssistant, panel: FakePanel) -> None:
    _gx_panel(panel)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="House",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, "password": "1234"},
        options={
            "doors": {"0": "Front Door"},
            "areas": {"1": "House"},
            "inputs": {"2": "Hall PIR"},
            "outputs": {"19": "Garage light"},
            "enable_arm_away": True,
        },
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "lock", DOMAIN, "ict_door_0", suggested_object_id="front_door", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "alarm_control_panel", DOMAIN, "ict_area_1", suggested_object_id="house", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "binary_sensor", DOMAIN, "ict_input_2", suggested_object_id="hall_pir", config_entry=entry
    )
    ent_reg.async_get_or_create(
        "switch", DOMAIN, "ict_output_19", suggested_object_id="garage_light", config_entry=entry
    )
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "door_0")}, name="Front Door"
    )
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ict_controller")}, name="ICT"
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert "enable_arm_away" not in entry.options
    pinned = {(item["type"], item["index"]) for item in entry.options[CONF_MANUAL_RECORDS]}
    assert pinned == {("door", 0), ("area", 1), ("input", 2), ("output", 19)}
    assert set(entry.data[CONF_LEGACY_KEYS]) >= {"door:0", "area:1", "input:2", "output:19"}

    assert hass.states.get("lock.front_door").state == "locked"
    assert ent_reg.async_get("lock.front_door").unique_id == "ict_door_0"
    assert hass.states.get("alarm_control_panel.house").state == "disarmed"
    assert ent_reg.async_get("alarm_control_panel.house").unique_id == "ict_area_1"
    assert hass.states.get("binary_sensor.hall_pir").state == "off"
    assert ent_reg.async_get("binary_sensor.hall_pir").unique_id == "ict_input_2"
    assert hass.states.get("switch.garage_light").state == "off"
    assert ent_reg.async_get("switch.garage_light").unique_id == "ict_output_19"
    assert dev_reg.async_get_device(identifiers={(DOMAIN, "door_0")}) is not None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, "ict_controller")}) is not None

    # A pinned ID the panel does not have is still not deleted when discovery runs again.
    # Here the panel does have them, and the 1.x names were kept.
    names = {item["index"]: item.get("name") for item in entry.data["records"] if item["type"] == "door"}
    assert names[0] == "Front Door"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_flow_pins_a_record_the_scan_did_not_find(hass: HomeAssistant, panel: FakePanel) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, CONF_PIN: "1234", "records": []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "records"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DISCOVERY_GAP: 8,
            "doors": "40=Spare",
            "areas": "",
            "inputs": "12-13",
            "outputs": "",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    pinned = {(item["type"], item["index"], item.get("name")) for item in entry.options[CONF_MANUAL_RECORDS]}
    assert ("door", 40, "Spare") in pinned
    assert ("input", 12, None) in pinned
    assert ("input", 13, None) in pinned
    # The panel has no door 40; it stays because it was pinned.
    assert any(item["type"] == "door" and item["index"] == 40 for item in entry.data["records"])
    assert entry.options[CONF_DISCOVERY_GAP] == 8

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
