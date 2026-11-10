"""Hub that owns the panel connections and routes updates to entities."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONNECTION,
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_NUMBERING,
    CONF_PIN,
    CONF_RECORDS,
    CONF_WX_PASSWORD,
    CONF_WX_PORT,
    CONF_WX_USERNAME,
    CONNECTION_WEB,
    DEFAULT_WX_PORT,
    DOMAIN,
    EVENT_DOOR,
    EVENT_EMERGENCY,
    EVENT_PANEL,
    MANUFACTURER,
)
from .protege import (
    AreaStatus,
    CommandRejected,
    DoorLockState,
    DoorState,
    DoorStatus,
    InputStatus,
    PanelEvent,
    PanelInfo,
    ProtegeClient,
    ProtegeError,
    ProtegeEventClient,
    Record,
    RecordType,
    StatusUpdate,
)
from .protege import protocol as proto
from .protege.client import NUMBERING_RECORD_ID
from .protege.wx import NO_RECORD, WXClient, WXError, WXSite, apply_names, apply_zone_settings

_LOGGER = logging.getLogger(__name__)

CONTROLLER_MODULE = "CP001"

# An input that opened this long before an alarm is taken as its cause.
ALARM_CAUSE_WINDOW = 90.0

# Whole words, so a name like "Panic Room" only trips it as part of a real alarm line.
_EMERGENCY = re.compile(r"\b(duress|panic|medical|hold[ -]?up)\b", re.IGNORECASE)

# Who the controller says unlocked the door, when this integration did not.
_LOCK_SOURCES = {
    DoorLockState.UNLOCKED_USER: "user",
    DoorLockState.UNLOCKED_SCHEDULE: "schedule",
    DoorLockState.UNLOCKED_USER_TIMED: "user",
    DoorLockState.UNLOCKED_LATCHED: "latched",
    DoorLockState.UNLOCKED_REX: "rex",
    DoorLockState.UNLOCKED_REN: "ren",
    DoorLockState.UNLOCKED_KEYPAD: "keypad",
    DoorLockState.UNLOCKED_AREA: "area",
    DoorLockState.UNLOCKED_FIRE: "fire",
}
SOURCE_HOME_ASSISTANT = "home_assistant"
RELOCK_RETRY = 15.0


def door_unlock_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    """Pending unlock_for timers. Times only: no PINs or passwords."""
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.door_unlocks")


@dataclass
class DoorUnlockPlan:
    """A timed unlock this integration started, kept until the controller reports locked."""

    source: str
    duration: int | None = None
    until: float | None = None  # unix time; None once a manual lock has cancelled it
    relock_requested: bool = False


def emergency_kind(text: str | None) -> str | None:
    """ "duress", "medical" or "panic" when an event reports one of those alarms."""
    match = _EMERGENCY.search(text or "")
    if match is None:
        return None
    word = match.group(1).lower()
    return word if word in ("duress", "medical") else "panic"


def records_to_data(records: list[Record]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        item: dict[str, Any] = {"type": r.record_type.value, "index": r.index, "reference": r.reference}
        if r.name:
            item["name"] = r.name
        if r.areas is not None:
            item["areas"] = list(r.areas)
        if r.arm_open is not None:
            item["arm_open"] = list(r.arm_open)
        out.append(item)
    return out


def records_from_data(data: list[dict[str, Any]]) -> list[Record]:
    return [
        Record(
            RecordType(d["type"]),
            int(d["index"]),
            d.get("reference"),
            d.get("name"),
            d.get("areas"),
            d.get("arm_open"),
        )
        for d in data
    ]


async def async_discover_records(
    hass: HomeAssistant, entry: ConfigEntry, client: ProtegeClient
) -> list[Record]:
    """Discover records over the control service, named and with zone settings.

    When an operator login is configured, the web interface is read first (one login):
    its record IDs tell discovery which records to read, and give names and zone settings.
    Names saved from the last successful read are used when it can't be reached.
    """
    site = await _async_read_site(hass, entry)
    saved = {r.key: r for r in records_from_data(entry.data.get(CONF_RECORDS, []))}
    listed: dict[RecordType, list[int]] = {}
    if site is not None:
        for record_type, items in site.lists.items():
            listed[record_type] = [rec_id for rec_id, _ in items if rec_id not in NO_RECORD]
    else:
        # Without the web interface, the records found last time still tell how the panel numbers them.
        for record_type, index in saved:
            listed.setdefault(record_type, []).append(index)
    records = await client.discover(listed=listed or None)
    for record in records:
        if (old := saved.get(record.key)) is not None:
            record.name = record.name or old.name
            record.areas = old.areas if record.areas is None else record.areas
            record.arm_open = old.arm_open if record.arm_open is None else record.arm_open
    if site is not None:
        by_id = client.numbering == NUMBERING_RECORD_ID
        named = apply_names(records, site.lists, by_id=by_id)
        zoned = apply_zone_settings(records, site, by_id=by_id)
        _LOGGER.debug(
            "Panel numbers records by %s; named %d records and read zone settings for %d inputs",
            client.numbering,
            named,
            zoned,
        )
    return records


async def _async_read_site(hass: HomeAssistant, entry: ConfigEntry) -> WXSite | None:
    username = entry.options.get(CONF_WX_USERNAME)
    password = entry.options.get(CONF_WX_PASSWORD)
    if not username or not password:
        return None
    client = WXClient(
        async_get_clientsession(hass, verify_ssl=False),
        entry.data[CONF_HOST],
        username,
        password,
        int(entry.options.get(CONF_WX_PORT, DEFAULT_WX_PORT)),
    )
    try:
        return await client.fetch_site()
    except WXError as err:
        _LOGGER.warning("Could not read names from the WX web interface: %s", err)
        return None


def record_id_prefix(serial: str, record_type: RecordType, index: int, by_record_id: bool) -> str:
    return f"{serial}_{record_type.value}_{'id' if by_record_id else ''}{index}"


class ProtegeHub:
    """Connection owner for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ProtegeClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.events: ProtegeEventClient | None = None
        self.records: list[Record] = records_from_data(entry.data.get(CONF_RECORDS, []))
        self._listeners: dict[tuple[RecordType, int], list[CALLBACK_TYPE]] = defaultdict(list)
        self._availability_listeners: list[CALLBACK_TYPE] = []
        self._event_listeners: list[Callable[[PanelEvent], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        # Arm modes turned off per area index, set by each area's arm mode switches.
        self.disabled_arm_modes: dict[int, set[str]] = defaultdict(set)
        # When each input last went from closed to active (monotonic seconds).
        self.last_active: dict[int, float] = {}
        self._input_active: dict[int, bool] = {}
        self._area_alarm: dict[int, bool] = {}
        # The input that set each area's alarm off, kept until the area is disarmed.
        self.alarm_cause: dict[int, str | None] = {}
        self._door_plans: dict[int, DoorUnlockPlan] = {}
        self._door_timers: dict[int, CALLBACK_TYPE] = {}
        self._reported_doors: dict[int, DoorStatus] = {}
        self._reported_unlocked: set[int] = set()
        self._tick_unsub: CALLBACK_TYPE | None = None

    # ----- identity -------------------------------------------------------

    @property
    def web(self) -> bool:
        """Web-only mode (no Automation and Control service)."""
        return self.entry.data.get(CONF_CONNECTION) == CONNECTION_WEB

    @property
    def has_events(self) -> bool:
        return self.web or self.events is not None

    @property
    def panel(self) -> PanelInfo:
        return self.client.panel

    @property
    def serial(self) -> str:
        return self.entry.unique_id or self.panel.serial_str or self.entry.entry_id

    @property
    def available(self) -> bool:
        return self.client.available

    @property
    def controller_device(self) -> DeviceInfo:
        # The control service reports "GX" for its firmware family, even on WX controllers.
        # Anything reached through the WX web interface is a WX.
        has_web = self.web or bool(self.entry.options.get(CONF_WX_USERNAME))
        fw_type = "WX" if has_web else (self.panel.firmware_type or "WX")
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=f"Protege {fw_type}",
            manufacturer=MANUFACTURER,
            model=f"Protege {fw_type} controller",
            serial_number=self.panel.serial_str,
            sw_version=self.panel.firmware,
            configuration_url=f"https://{self.client.host}"
            + (f":{self.client.port}" if self.web and self.client.port != 443 else ""),
        )

    def module_device(self, module: str | None) -> DeviceInfo:
        """Device for the hardware module a record is wired to."""
        if module is None or module == CONTROLLER_MODULE:
            return self.controller_device
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.serial}_module_{module}")},
            name=module,
            manufacturer=MANUFACTURER,
            model=f"{module[:2]} module",
        )

    def record_device(self, record: Record) -> DeviceInfo:
        """Doors, areas, inputs and outputs each get their own device, so each can sit in its own HA area."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.record_device_id(record))},
            name=record.default_name,
            manufacturer=MANUFACTURER,
            model=f"Protege {record.record_type.value}",
        )

    @property
    def by_record_id(self) -> bool:
        """Whether records are numbered by record ID (web interface, or the control service without
        ACPUseDisplayOrder) rather than 1, 2, 3 in display order."""
        return self.web or self.entry.data.get(CONF_NUMBERING) == NUMBERING_RECORD_ID

    def record_device_id(self, record: Record) -> str:
        """Also the prefix of the record's entity unique IDs.

        Record IDs get an "id" marker, so a record numbered one way can never take over
        the entity of a different record numbered the other way (door 1 by display order is
        not door ID 1).
        """
        return record_id_prefix(self.serial, record.record_type, record.index, self.by_record_id)

    @property
    def modules(self) -> set[str]:
        """Expansion modules with inputs or outputs (trouble inputs sit on the controller)."""
        return {
            r.module
            for r in self.records
            if r.module
            and r.module != CONTROLLER_MODULE
            and r.record_type in (RecordType.INPUT, RecordType.OUTPUT)
        }

    def records_of(self, record_type: RecordType) -> list[Record]:
        return [r for r in self.records if r.record_type is record_type]

    # ----- zones ----------------------------------------------------------

    @property
    def zones_known(self) -> bool:
        """True once the inputs' areas have been read from the web interface."""
        return any(r.areas is not None for r in self.records_of(RecordType.INPUT))

    def area_inputs(self, area_index: int) -> list[Record]:
        return [r for r in self.records_of(RecordType.INPUT) if area_index in (r.areas or ())]

    def open_zones(self, area_index: int) -> list[str] | None:
        """Names of the open, unbypassed inputs that stop this area arming (None if unknown).

        Inputs whose type lets them stay open when arming are left out.
        """
        names = []
        for record in self.area_inputs(area_index):
            if area_index in (record.arm_open or ()):
                continue
            status = self.client.status.get(record.key)
            if not isinstance(status, InputStatus):
                return None
            if status.active and not status.bypassed:
                names.append(record.default_name)
        return names

    def _find_alarm_cause(self, area_index: int) -> str | None:
        """The input that most recently went active in the area, else one active now."""
        inputs = self.area_inputs(area_index) if self.zones_known else self.records_of(RecordType.INPUT)
        cutoff = time.monotonic() - ALARM_CAUSE_WINDOW
        recent = [
            (self.last_active[r.index], r.default_name)
            for r in inputs
            if self.last_active.get(r.index, 0.0) >= cutoff
        ]
        if recent:
            return max(recent)[1]
        for record in inputs:
            status = self.client.status.get(record.key)
            if isinstance(status, InputStatus) and status.active and not status.bypassed:
                return record.default_name
        return None

    def _track(self, update: StatusUpdate) -> list[tuple[RecordType, int]]:
        """Note input activity and alarms; return other records whose entities depend on this one."""
        status = update.status
        if isinstance(status, InputStatus):
            was = self._input_active.get(update.index, False)
            self._input_active[update.index] = status.active
            if status.active and not was:
                self.last_active[update.index] = time.monotonic()
            record = next((r for r in self.records_of(RecordType.INPUT) if r.index == update.index), None)
            return [(RecordType.AREA, area) for area in (record.areas or ())] if record else []
        if isinstance(status, AreaStatus):
            was = self._area_alarm.get(update.index, False)
            self._area_alarm[update.index] = status.alarm
            if status.alarm and not was and self.alarm_cause.get(update.index) is None:
                self.alarm_cause[update.index] = self._find_alarm_cause(update.index)
            elif not status.armed and not status.alarm:
                self.alarm_cause[update.index] = None
        return []

    # ----- timed unlock ---------------------------------------------------

    def unlock_remaining(self, index: int) -> tuple[str, int] | None:
        """ISO unlock_until and whole seconds left, once the controller reports unlocked."""
        plan = self._door_plans.get(index)
        if plan is None or plan.until is None or index not in self._reported_unlocked:
            return None
        return dt_util.utc_from_timestamp(plan.until).isoformat(), max(0, int(plan.until - time.time()))

    async def async_schedule_relock(
        self, index: int, seconds: int, source: str = SOURCE_HOME_ASSISTANT
    ) -> None:
        """Unlock stays latched until `seconds` have passed, then this integration locks it."""
        self._door_plans[index] = DoorUnlockPlan(source=source, duration=seconds, until=time.time() + seconds)
        self._arm_timer(index)
        self._ensure_tick()
        await self._save_door_plans()

    async def async_cancel_relock(self, index: int) -> DoorUnlockPlan | None:
        """A manual lock. The timer must not lock the door again afterwards.

        Returns the suspended plan so the caller can put it back if the lock command fails.
        """
        plan = self._door_plans.get(index)
        if plan is None or plan.until is None:
            return None
        suspended = DoorUnlockPlan(source=plan.source, duration=plan.duration, until=plan.until)
        plan.until = None
        plan.relock_requested = True
        self._cancel_timer(index)
        self._ensure_tick()
        await self._save_door_plans()
        return suspended

    async def async_resume_relock(self, index: int, suspended: DoorUnlockPlan) -> None:
        """Put back a timer suspended for a lock command that the panel refused."""
        if suspended.until is None:
            return
        plan = self._door_plans.get(index)
        if plan is None:
            plan = DoorUnlockPlan(source=suspended.source)
            self._door_plans[index] = plan
        plan.source = suspended.source
        plan.duration = suspended.duration
        plan.until = suspended.until
        plan.relock_requested = False
        self._arm_timer(index)
        self._ensure_tick()
        await self._save_door_plans()

    async def async_forget_relock(self, index: int) -> None:
        """Drop a timer whose unlock command never succeeded."""
        if self._door_plans.pop(index, None) is None:
            return
        self._cancel_timer(index)
        self._ensure_tick()
        await self._save_door_plans()

    def _arm_timer(self, index: int, delay: float | None = None) -> None:
        plan = self._door_plans.get(index)
        if plan is None or plan.until is None:
            return
        if delay is None:
            delay = max(0.0, plan.until - time.time())
        self._cancel_timer(index)

        @callback
        def fire(_now: Any) -> None:
            self._door_timers.pop(index, None)
            self.hass.async_create_task(self._relock(index))

        self._door_timers[index] = async_call_later(self.hass, delay, fire)

    def _cancel_timer(self, index: int) -> None:
        if (unsub := self._door_timers.pop(index, None)) is not None:
            unsub()

    def _cancel_door_timers(self) -> None:
        for index in list(self._door_timers):
            self._cancel_timer(index)
        if self._tick_unsub is not None:
            self._tick_unsub()
            self._tick_unsub = None

    def _ensure_tick(self) -> None:
        """Refresh remaining time while a timed unlock is showing."""
        active = any(plan.until is not None for plan in self._door_plans.values())
        if active and self._tick_unsub is None:
            self._tick_unsub = async_track_time_interval(self.hass, self._tick_doors, timedelta(seconds=1))
        elif not active and self._tick_unsub is not None:
            self._tick_unsub()
            self._tick_unsub = None

    @callback
    def _tick_doors(self, _now: Any) -> None:
        for index, plan in self._door_plans.items():
            if plan.until is None:
                continue
            for listener in list(self._listeners.get((RecordType.DOOR, index), ())):
                listener()
        self._ensure_tick()

    async def _relock(self, index: int) -> None:
        plan = self._door_plans.get(index)
        if plan is None or plan.until is None:
            return
        plan.relock_requested = True
        try:
            await self.client.door_lock(index)
        except CommandRejected as err:
            plan.relock_requested = False
            if err.code == proto.NACK_SERVICE_INDEX_NOT_VALID:
                await self.async_forget_relock(index)
                return
            _LOGGER.warning("Could not relock door %s: %s", index, err)
            self._arm_timer(index, RELOCK_RETRY)
        except ProtegeError as err:
            plan.relock_requested = False
            _LOGGER.warning("Could not relock door %s: %s", index, err)
            self._arm_timer(index, RELOCK_RETRY)

    async def _restore_unlocks(self) -> None:
        data = await door_unlock_store(self.hass, self.entry).async_load() or {}
        known = {r.index for r in self.records_of(RecordType.DOOR)}
        for key, item in (data.get("doors") or {}).items():
            try:
                index = int(key)
                until = float(item["until"])
                duration = int(item["duration"])
                source = str(item.get("source") or SOURCE_HOME_ASSISTANT)
            except (KeyError, TypeError, ValueError):
                continue
            if index not in known:
                continue
            self._door_plans[index] = DoorUnlockPlan(source=source, duration=duration, until=until)
            self._arm_timer(index)
        self._ensure_tick()

    async def _save_door_plans(self) -> None:
        doors = {
            str(index): {"until": plan.until, "duration": plan.duration, "source": plan.source}
            for index, plan in self._door_plans.items()
            if plan.until is not None
        }
        await door_unlock_store(self.hass, self.entry).async_save({"doors": doors})

    def _door_source(self, index: int, status: DoorStatus, *, relock: bool) -> str:
        plan = self._door_plans.get(index)
        if relock:
            if plan is not None and plan.relock_requested:
                return plan.source
            return "controller"
        if plan is not None and plan.until is not None:
            return plan.source
        if status.door_state == DoorState.FORCED_OPEN:
            return "forced"
        state = status.lock_state
        if isinstance(state, DoorLockState):
            return _LOCK_SOURCES.get(state, state.name.lower())
        return "controller"

    def _fire_door(self, index: int, action: str, status: DoorStatus) -> None:
        record = next((r for r in self.records_of(RecordType.DOOR) if r.index == index), None)
        plan = self._door_plans.get(index)
        self.hass.bus.async_fire(
            EVENT_DOOR,
            {
                "entry_id": self.entry.entry_id,
                "action": action,
                "door": record.default_name if record else f"Door {index}",
                "source": self._door_source(index, status, relock=action == "relocked"),
                "duration": None if plan is None else plan.duration,
            },
        )

    def _on_reported_door(self, index: int, status: DoorStatus) -> None:
        """Fire door events from the controller's status, not from an assumed command."""
        previous = self._reported_doors.get(index)
        self._reported_doors[index] = status
        if status.locked:
            self._reported_unlocked.discard(index)
        else:
            self._reported_unlocked.add(index)
        if previous is None:
            return
        relocked = not previous.locked and status.locked
        if previous.locked and not status.locked:
            self._fire_door(index, "unlocked", status)
        elif relocked:
            self._fire_door(index, "relocked", status)
        was_open = previous.door_state != DoorState.CLOSED
        now_open = status.door_state != DoorState.CLOSED
        if not was_open and now_open:
            self._fire_door(index, "opened", status)
        elif was_open and not now_open:
            self._fire_door(index, "closed", status)
        if relocked and index in self._door_plans:
            self._door_plans.pop(index, None)
            self._cancel_timer(index)
            self._ensure_tick()
            self.hass.async_create_task(self._save_door_plans())

    # ----- lifecycle ------------------------------------------------------

    async def async_start(self) -> None:
        self._unsubs.append(self.client.add_status_listener(self._on_status))
        self._unsubs.append(self.client.add_connection_listener(self._on_connection))
        await self.client.start(self.records)
        await self._restore_unlocks()

        if self.web:
            # The web client reads the event log itself.
            self._unsubs.append(self.client.add_listener(self._on_event))
            self._unsubs.append(self.client.add_denied_listener(self._on_denied))
            self._on_denied(set(self.client.denied))
            return
        port = self.entry.options.get(CONF_EVENTS_PORT)
        pin = self.entry.options.get(CONF_EVENTS_PIN)
        if port and pin:
            self.events = ProtegeEventClient(self.client.host, int(port), str(pin))
            self._unsubs.append(self.events.add_listener(self._on_event))
            await self.events.start()

    async def async_stop(self) -> None:
        self._cancel_door_timers()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.events:
            await self.events.stop()
        await self.client.stop()

    # ----- routing --------------------------------------------------------

    @callback
    def async_add_listener(self, key: tuple[RecordType, int], listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners[key].append(listener)
        self._availability_listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners[key].remove(listener)
            self._availability_listeners.remove(listener)

        return remove

    @callback
    def async_add_availability_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    @callback
    def async_add_event_listener(self, listener: Callable[[PanelEvent], None]) -> CALLBACK_TYPE:
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    @callback
    def async_set_arm_mode(self, area_index: int, mode: str, enabled: bool) -> None:
        disabled = self.disabled_arm_modes[area_index]
        if enabled:
            disabled.discard(mode)
        else:
            disabled.add(mode)
        for listener in list(self._listeners.get((RecordType.AREA, area_index), ())):
            listener()

    @callback
    def _on_status(self, update: StatusUpdate) -> None:
        if (
            update.record_type is RecordType.DOOR
            and isinstance(update.status, DoorStatus)
            and update.reported
        ):
            self._on_reported_door(update.index, update.status)
        dependants = self._track(update)
        for key in [(update.record_type, update.index), *dependants]:
            for listener in list(self._listeners.get(key, ())):
                listener()

    @callback
    def _on_denied(self, denied: set[RecordType]) -> None:
        """Web-only mode: say which Monitoring boxes the operator's role is missing."""
        issue_id = f"web_rights_{self.entry.entry_id}"
        if not denied:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        boxes = {
            RecordType.AREA: "Areas",
            RecordType.DOOR: "Doors",
            RecordType.INPUT: "Inputs",
            RecordType.OUTPUT: "Outputs",
            RecordType.TROUBLE_INPUT: "Trouble Inputs",
        }
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="web_rights",
            translation_placeholders={
                "title": self.entry.title,
                "boxes": ", ".join(sorted(boxes[t] for t in denied)),
            },
        )

    @callback
    def _on_connection(self, available: bool) -> None:
        for listener in list(self._availability_listeners):
            listener()

    @callback
    def _on_event(self, event: PanelEvent) -> None:
        self.hass.bus.async_fire(
            EVENT_PANEL,
            {
                "entry_id": self.entry.entry_id,
                "text": event.text,
                "category": event.category,
                "user": event.user,
                "code": event.code,
            },
        )
        if (kind := emergency_kind(event.text)) is not None:
            self._raise_emergency(kind, event)
        if "door" in (event.text or "").lower():
            # Changes made by other systems (keypads, Savant, cards) are not always pushed on
            # the control connection; the event is a cue to read the doors back.
            self.client.watch_doors()
        for listener in list(self._event_listeners):
            listener(event)

    @callback
    def _raise_emergency(self, kind: str, event: PanelEvent) -> None:
        """A duress, panic or medical alarm: an event, and a Repairs alert that stays until handled."""
        self.hass.bus.async_fire(
            EVENT_EMERGENCY,
            {"entry_id": self.entry.entry_id, "kind": kind, "text": event.text, "user": event.user},
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"emergency_{self.entry.entry_id}_{int(time.time())}",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.CRITICAL,
            translation_key="emergency",
            translation_placeholders={"kind": kind, "text": event.text or ""},
        )


def link_child_devices(hass: HomeAssistant, hub: ProtegeHub) -> None:
    """Point inputs and outputs at their module, and doors, areas and modules at the controller.

    Done through the registry rather than DeviceInfo.via_device, which newer
    Home Assistant releases deprecate.
    """
    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, hub.entry.entry_id)
    # Look the controller up within this entry: async_get_device is deprecated.
    controller = next((d for d in devices if (DOMAIN, hub.serial) in d.identifiers), None)
    if controller is None:
        return
    by_identifier = {ident: d for d in devices for ident in d.identifiers}
    parent_of: dict[str, str] = {}
    for record in hub.records_of(RecordType.INPUT) + hub.records_of(RecordType.OUTPUT):
        device = by_identifier.get((DOMAIN, hub.record_device_id(record)))
        module = by_identifier.get((DOMAIN, f"{hub.serial}_module_{record.module}"))
        if device is not None and module is not None:
            parent_of[device.id] = module.id
    for device in devices:
        if device.id == controller.id:
            continue
        parent = parent_of.get(device.id, controller.id)
        if device.via_device_id != parent:
            dev_reg.async_update_device(device.id, via_device_id=parent)


def remove_stale_devices(hass: HomeAssistant, hub: ProtegeHub) -> None:
    """Drop devices for records that no longer exist on the panel."""
    dev_reg = dr.async_get(hass)
    valid = {(DOMAIN, hub.serial)}
    for record in hub.records:
        if record.record_type is not RecordType.TROUBLE_INPUT:
            valid.add((DOMAIN, hub.record_device_id(record)))
    valid.update((DOMAIN, f"{hub.serial}_module_{module}") for module in hub.modules)
    for device in dr.async_entries_for_config_entry(dev_reg, hub.entry.entry_id):
        if not device.identifiers & valid:
            dev_reg.async_remove_device(device.id)


__all__ = ["CONF_PIN", "ProtegeHub", "records_from_data", "records_to_data", "remove_stale_devices"]
