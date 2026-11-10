"""Doors as locks."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .entity import ProtegeEntity
from .protege import DoorLockState, DoorStatus, RecordType

SERVICE_UNLOCK_FOR = "unlock_for"
ATTR_DURATION = "duration"


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(ProtegeDoorLock(hub, r) for r in hub.records_of(RecordType.DOOR))

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_UNLOCK_FOR,
        {vol.Required(ATTR_DURATION): vol.All(cv.time_period, cv.positive_timedelta)},
        "async_unlock_for",
    )


class ProtegeDoorLock(ProtegeEntity, LockEntity):
    """Lock = lock, unlock = unlock latched, open = momentary unlock."""

    _attr_name = None
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "lock", hub.record_device(record))

    @property
    def is_locked(self) -> bool | None:
        status: DoorStatus | None = self.status
        return None if status is None else status.locked

    @property
    def is_open(self) -> bool | None:
        status: DoorStatus | None = self.status
        if status is None:
            return None
        return not status.locked and status.open

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status: DoorStatus | None = self.status
        if status is None:
            return None
        state = status.lock_state
        attributes: dict[str, Any] = {
            "lock_reason": state.name.lower() if isinstance(state, DoorLockState) else state
        }
        # Only after the controller has reported the door unlocked, not from the assumed state.
        if (remaining := self.hub.unlock_remaining(self.record.index)) is not None:
            attributes["unlock_until"], attributes["remaining"] = remaining
        return attributes

    async def async_lock(self, **kwargs: Any) -> None:
        suspended = await self.hub.async_cancel_relock(self.record.index)
        try:
            await self._run(self.hub.client.door_lock(self.record.index))
        except HomeAssistantError:
            if suspended is not None:
                await self.hub.async_resume_relock(self.record.index, suspended)
            raise

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.door_unlock_latched(self.record.index))

    async def async_open(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.door_unlock(self.record.index))

    async def async_unlock_for(self, duration) -> None:
        """Unlock for `duration`, then lock.

        Neither the control service nor the web interface accepts a duration on a door
        unlock: momentary unlock uses the door's programmed lock activation time. Latch
        the door and lock it again when the time is up.
        """
        seconds = max(1, min(65535, int(duration.total_seconds())))
        await self.hub.async_schedule_relock(self.record.index, seconds)
        try:
            await self._run(self.hub.client.door_unlock_latched(self.record.index))
        except HomeAssistantError:
            await self.hub.async_forget_relock(self.record.index)
            raise
