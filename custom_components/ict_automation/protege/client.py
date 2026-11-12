"""Async clients for the ICT Automation and Control Service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from . import protocol as p
from .models import (
    DoorLockState,
    DoorStatus,
    InputStatus,
    OutputState,
    OutputStatus,
    PanelEvent,
    PanelInfo,
    Record,
    RecordType,
    Status,
    StatusUpdate,
    decode_event,
    decode_panel_info,
    decode_status,
)

_LOGGER = logging.getLogger(__name__)

MAX_MONITORED = 250
MONITOR_BATCH = 10
# Read a door back this long after it moves or is seen on a timed unlock.
DOOR_WATCH_DELAYS = (0.3, 1.5, 4.0, 8.0, 15.0)
# A single quick check after an output change or door event.
DOOR_CHECK_DELAYS = (0.3,)
_TIMED_UNLOCKS = {
    DoorLockState.UNLOCKED_USER,
    DoorLockState.UNLOCKED_USER_TIMED,
    DoorLockState.UNLOCKED_REX,
    DoorLockState.UNLOCKED_REN,
    DoorLockState.UNLOCKED_KEYPAD,
}
MONITOR_PRIORITY = (
    RecordType.AREA,
    RecordType.DOOR,
    RecordType.OUTPUT,
    RecordType.INPUT,
    RecordType.TROUBLE_INPUT,
)
# Highest record index probed for each type (inclusive). A full scan (gap 0) stops here
# so a sparse panel is not walked into the tens of thousands. Typical GX sites sit well
# inside these: doors and areas in the low hundreds, expander inputs and outputs a few
# hundred. Anything higher can be added by record ID.
DEFAULT_DISCOVERY_LIMITS = {
    RecordType.AREA: 255,
    RecordType.DOOR: 255,
    RecordType.OUTPUT: 511,
    RecordType.INPUT: 511,
    RecordType.TROUBLE_INPUT: 255,
}
# Confirmed empty (the panel said the index does not exist) versus no usable answer.
_ABSENT = "absent"
_INCONCLUSIVE = "inconclusive"


class ProtegeError(Exception):
    """Base error for the Protege client."""


class CannotConnect(ProtegeError):
    """The panel could not be reached."""


class ProtegeTimeout(ProtegeError):
    """The panel did not answer in time."""


class CommandRejected(ProtegeError):
    """The panel answered a command with a Nack."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        message = p.NACK_MESSAGES.get(code or -1, "The panel rejected the command")
        if code is not None:
            message = f"{message} (code {code:#06x})"
        super().__init__(message)


class InvalidPin(CommandRejected):
    """The PIN was not accepted."""


@dataclass
class _Waiter:
    accept: Callable[[p.ICTPacket], bool]
    future: asyncio.Future[p.ICTPacket]


class ProtegeConnection:
    """One TCP connection to an ACS service, with request/response matching."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        on_data: Callable[[p.ICTPacket], None] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._on_data = on_data
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._waiters: list[_Waiter] = []
        self.lock = asyncio.Lock()
        self.closed = asyncio.Event()
        self.closed.set()
        # Off: on site the panel Nacked (0x0120) every Ack we sent for pushed data.
        self.ack_pushes = False

    @property
    def connected(self) -> bool:
        return not self.closed.is_set()

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (TimeoutError, OSError) as err:
            raise CannotConnect(f"Cannot connect to {self.host}:{self.port}: {err}") from err
        self.closed.clear()
        self._read_task = asyncio.create_task(self._read_loop(), name="ict_protege_reader")

    async def close(self) -> None:
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
        self._shutdown()

    def _shutdown(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._writer = None
        self._reader = None
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_exception(CannotConnect("Connection closed"))
        self._waiters.clear()
        self.closed.set()

    async def _read_loop(self) -> None:
        assembler = p.PacketReader()
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    _LOGGER.debug("Panel %s:%s closed the connection", self.host, self.port)
                    break
                for raw in assembler.feed(chunk):
                    try:
                        packet = p.parse_packet(raw)
                    except p.ICTProtocolError as err:
                        _LOGGER.debug("Ignoring invalid packet %s: %s", raw.hex(), err)
                        continue
                    self._dispatch(packet)
        except asyncio.CancelledError:
            raise
        except OSError as err:
            _LOGGER.debug("Read error from %s:%s: %s", self.host, self.port, err)
        finally:
            self._shutdown()

    def _dispatch(self, packet: p.ICTPacket) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "RX %s:%s type=%#04x %s", self.host, self.port, packet.packet_type, packet.data.hex(" ")
            )
        consumed = False
        for waiter in list(self._waiters):
            if not waiter.future.done() and waiter.accept(packet):
                waiter.future.set_result(packet)
                self._waiters.remove(waiter)
                consumed = True
                break
        if packet.packet_type == p.PKT_TYPE_DATA and not consumed and self.ack_pushes:
            # Pushed data (monitoring changes, events) must be acked, or the panel keeps
            # resending it and can hold back later changes. Replies to requests are not acked.
            asyncio.get_running_loop().create_task(self._send_ack())
        if packet.packet_type == p.PKT_TYPE_DATA and self._on_data is not None:
            # Data always goes to the listener, including replies to status requests.
            try:
                self._on_data(packet)
            except Exception:
                _LOGGER.exception("Error handling panel data")
        elif not consumed and packet.packet_type == p.PKT_TYPE_SYSTEM:
            _LOGGER.debug("Unmatched system packet %s", packet.data.hex())

    async def _send_ack(self) -> None:
        with contextlib.suppress(ProtegeError):
            await self.write(p.build_ack())

    async def configure_acks(self) -> None:
        """Ask the panel to Ack our commands but not to expect Acks for pushed data.

        Then nothing we send is a reply the panel might answer, so every Ack or Nack we
        receive belongs to a command. Call with the lock held. Panels set up with "Ack
        Commands" only (as on site) refuse Acks for pushed data, so pushes are not acked
        even if this command is refused.
        """
        try:
            await self.command(p.build_ack_config(command_ack=True, monitor_ack=False, event_ack=False))
        except CommandRejected as err:
            _LOGGER.debug("Panel refused the Ack configuration: %s", err)

    async def write(self, raw: bytes) -> None:
        if self._writer is None:
            raise CannotConnect("Not connected")
        if _LOGGER.isEnabledFor(logging.DEBUG):
            if raw[4:5] == bytes((p.PKT_TYPE_COMMAND,)) and raw[6:8] == bytes((p.GROUP_SYSTEM, p.SYS_LOGIN)):
                # Login packets carry the PIN (or the code someone typed to arm/disarm).
                _LOGGER.debug("TX %s:%s login [PIN redacted]", self.host, self.port)
            else:
                _LOGGER.debug("TX %s:%s %s", self.host, self.port, raw.hex(" "))
        try:
            self._writer.write(raw)
            await self._writer.drain()
        except OSError as err:
            self._shutdown()
            raise CannotConnect(str(err)) from err

    def _add_waiter(self, accept: Callable[[p.ICTPacket], bool]) -> asyncio.Future[p.ICTPacket]:
        future: asyncio.Future[p.ICTPacket] = asyncio.get_running_loop().create_future()
        self._waiters.append(_Waiter(accept, future))
        return future

    async def _await(self, future: asyncio.Future[p.ICTPacket], timeout: float | None) -> p.ICTPacket:
        try:
            return await asyncio.wait_for(future, timeout or self.timeout)
        except TimeoutError as err:
            self._waiters = [w for w in self._waiters if w.future is not future]
            raise ProtegeTimeout("The panel did not respond") from err

    async def wait_for(
        self, accept: Callable[[p.ICTPacket], bool], *, timeout: float | None = None
    ) -> p.ICTPacket:
        """Wait for a packet without sending anything."""
        return await self._await(self._add_waiter(accept), timeout)

    async def request(
        self,
        raw: bytes,
        accept: Callable[[p.ICTPacket], bool],
        *,
        timeout: float | None = None,
    ) -> p.ICTPacket:
        """Send a packet and wait for the first reply that `accept` matches."""
        future = self._add_waiter(accept)
        await self.write(raw)
        return await self._await(future, timeout)

    async def command(self, raw: bytes, *, timeout: float | None = None) -> None:
        """Send a command and wait for Ack; raise CommandRejected on Nack."""
        reply = await self.request(raw, _is_system, timeout=timeout)
        if reply.is_nack:
            code = reply.nack_code
            if code == p.NACK_USER_INVALID:
                raise InvalidPin(code)
            raise CommandRejected(code)

    async def command_batch(self, packets: list[bytes], *, timeout: float | None = None) -> list[p.ICTPacket]:
        """Send several commands back to back and collect one system reply each."""
        futures = [self._add_waiter(_is_system) for _ in packets]
        results: list[p.ICTPacket] = []
        try:
            await self.write(b"".join(packets))
            for future in futures:
                results.append(await self._await(future, timeout))
        finally:
            pending = {id(f) for f in futures if not f.done()}
            self._waiters = [w for w in self._waiters if id(w.future) not in pending]
            for future in futures:
                if future.done() and not future.cancelled():
                    future.exception()  # mark retrieved
                else:
                    future.cancel()
        return results


def _command_target(raw: bytes) -> tuple[RecordType, int] | None:
    """Return the record a record command packet addresses."""
    if len(raw) < 12 or raw[4] != p.PKT_TYPE_COMMAND:
        return None
    record_type = RecordType.from_group(raw[6])
    if record_type is None:
        return None
    return record_type, struct.unpack("<I", raw[8:12])[0]


def _is_system(packet: p.ICTPacket) -> bool:
    return packet.packet_type == p.PKT_TYPE_SYSTEM


def _status_matcher(group: int, index: int) -> Callable[[p.ICTPacket], bool]:
    def accept(packet: p.ICTPacket) -> bool:
        if packet.is_nack:
            return True
        if packet.packet_type != p.PKT_TYPE_DATA:
            return False
        for block in p.iter_data_blocks(packet.data):
            update = decode_status(block)
            if update and update.record_type.group == group and update.index == index:
                return True
        return False

    return accept


def _panel_info_matcher(packet: p.ICTPacket) -> bool:
    if packet.is_nack:
        return True
    if packet.packet_type != p.PKT_TYPE_DATA:
        return False
    return any(block.type_high == 0x00 for block in p.iter_data_blocks(packet.data))


# How the control service numbers records: by record ID (the GX/WX default, the same IDs the
# web interface uses) or 1, 2, 3 in display order (after the ACPUseDisplayOrder command).
NUMBERING_RECORD_ID = "record_id"
NUMBERING_DISPLAY_ORDER = "display_order"

StatusListener = Callable[[StatusUpdate], None]
ConnectionListener = Callable[[bool], None]


class ProtegeClient:
    """Control client.

    Monitors records without a logged-in user (needs the service option
    "Allow Status Requests When Not Logged In"), and logs in only for the
    duration of each command, then restores monitoring.
    """

    def __init__(
        self,
        host: str,
        port: int,
        pin: str,
        *,
        timeout: float = 5.0,
        keepalive_interval: float = 30.0,
        poll_interval: float = 60.0,
        reconnect_interval: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.pin = pin
        self.keepalive_interval = keepalive_interval
        self.poll_interval = poll_interval
        self.reconnect_interval = reconnect_interval
        self._conn = ProtegeConnection(host, port, on_data=self._handle_data, timeout=timeout)
        self.panel = PanelInfo()
        self.records: dict[tuple[RecordType, int], Record] = {}
        self.status: dict[tuple[RecordType, int], Status] = {}
        self._status_listeners: list[StatusListener] = []
        self._connection_listeners: list[ConnectionListener] = []
        self._monitored: list[Record] = []
        self.numbering = NUMBERING_DISPLAY_ORDER
        self._polled: list[Record] = []
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._command_generation = 0
        self._door_watch: dict[int, asyncio.Task] = {}
        self._door_check: dict[int, asyncio.Task] = {}
        self._stopping = False
        self._available = False
        # Indexes the last discover() proved do not exist. A timeout is not in here.
        self.last_absent: set[tuple[RecordType, int]] = set()
        # Indexes a later status read proved do not exist, so the poll does not log in
        # again for a pinned ID the panel has already rejected.
        self._absent_status: set[tuple[RecordType, int]] = set()

    # ----- listeners -----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    def add_status_listener(self, listener: StatusListener) -> Callable[[], None]:
        self._status_listeners.append(listener)
        return lambda: self._status_listeners.remove(listener)

    def add_connection_listener(self, listener: ConnectionListener) -> Callable[[], None]:
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for listener in list(self._connection_listeners):
            listener(available)

    def _handle_data(self, packet: p.ICTPacket) -> None:
        for block in p.iter_data_blocks(packet.data):
            update = decode_status(block)
            if update is None:
                continue
            previous = self.status.get((update.record_type, update.index))
            self.status[(update.record_type, update.index)] = update.status
            if update.record_type is RecordType.DOOR:
                self._watch_door(update.index, previous, update.status)
            elif (
                update.record_type is RecordType.OUTPUT and previous is not None and previous != update.status
            ):
                # Unlocks made by another system while logged in (Savant, a keypad) are not
                # pushed, but the door's lock output is. Read the doors back.
                self.watch_doors()
            record = self.records.get((update.record_type, update.index))
            if record is not None and record.reference is None:
                record.reference = getattr(update.status, "reference", None) or None
            for listener in list(self._status_listeners):
                listener(update)

    def watch_doors(self) -> None:
        """Read every door back once, shortly (e.g. after a door event or an output change).

        If a door turns out to be on a timed unlock, _watch_door keeps reading it until it
        relocks; otherwise one read is enough, which keeps busy outputs (keypad LEDs that
        follow PIRs) from flooding the connection.
        """
        for record in self.records.values():
            if record.record_type is RecordType.DOOR:
                self._start_door_watch(record.index, DOOR_CHECK_DELAYS)

    def _start_door_watch(self, index: int, delays: tuple[float, ...] | None = None) -> None:
        if self._stopping or not self._conn.connected:
            return
        # Quick checks and full watches are tracked apart, so a check that finds a timed
        # unlock can still start the full watch.
        tasks = self._door_check if delays else self._door_watch
        task = tasks.get(index)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._refresh_later((RecordType.DOOR, index), delays or DOOR_WATCH_DELAYS))
        tasks[index] = task
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _watch_door(self, index: int, previous: Status | None, status: Status) -> None:
        """Catch lock changes the panel does not push.

        On some panels a card or REX unlock pushes the door opening but not the unlock.
        When a door opens or closes, or is seen unlocked by a timer, read it back a few
        times so HA shows the unlock and the relock.
        """
        if self._stopping or not isinstance(status, DoorStatus):
            return
        moved = isinstance(previous, DoorStatus) and previous.door_state != status.door_state
        timed_unlock = status.lock_state in _TIMED_UNLOCKS
        if moved or timed_unlock:
            self._start_door_watch(index)

    # ----- connection ----------------------------------------------------

    async def connect(self) -> PanelInfo:
        """Open the connection and read the panel description."""
        await self._conn.open()
        try:
            async with self._conn.lock:
                await self._conn.command(p.build_command_packet(p.GROUP_SYSTEM, p.SYS_ARE_YOU_THERE))
                await self._conn.configure_acks()
                self.panel = await self._read_panel_info()
        except ProtegeError:
            await self._conn.close()
            raise
        return self.panel

    async def _read_panel_info(self) -> PanelInfo:
        # The description may arrive as one packet or several; gather briefly.
        info = PanelInfo()
        raw = p.build_command_packet(p.GROUP_SYSTEM, p.SYS_PANEL_DESCRIPTION)
        try:
            reply = await self._conn.request(raw, _panel_info_matcher)
        except ProtegeTimeout:
            _LOGGER.warning("Panel did not return a description")
            return info
        if reply.packet_type == p.PKT_TYPE_DATA:
            decode_panel_info(list(p.iter_data_blocks(reply.data)), info)
            while info.serial is None or info.firmware_version is None:
                try:
                    more = await self._conn.wait_for(_panel_info_matcher, timeout=0.5)
                except ProtegeTimeout:
                    break
                if more.packet_type != p.PKT_TYPE_DATA:
                    break
                decode_panel_info(list(p.iter_data_blocks(more.data)), info)
        return info

    async def disconnect(self) -> None:
        await self._conn.close()

    async def verify_pin(self) -> None:
        """Log in with the saved PIN and log out again. Raises InvalidPin."""
        async with self._conn.lock:
            await self._login(self.pin)
            await self._logout()

    async def start(self, records: Iterable[Record]) -> None:
        """Start monitoring the given records and keep the connection alive."""
        self.set_records(records)
        self._stopping = False
        if self._conn.connected:
            await self._restore_monitoring()
            await self.ensure_status()
            self._set_available(True)
        self._tasks = [
            asyncio.create_task(self._supervisor(), name="ict_protege_supervisor"),
            asyncio.create_task(self._poll_loop(), name="ict_protege_poll"),
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        for task in list(self._background):
            task.cancel()
        await self._conn.close()
        self._set_available(False)

    def set_records(self, records: Iterable[Record]) -> None:
        self.records = {r.key: r for r in records}
        ordered = sorted(
            self.records.values(),
            key=lambda r: (MONITOR_PRIORITY.index(r.record_type), r.index),
        )
        self._monitored = ordered[:MAX_MONITORED]
        self._polled = ordered[MAX_MONITORED:]
        if self._polled:
            _LOGGER.info(
                "%s records exceed the panel's monitoring limit of %s; they are polled every %ss",
                len(self._polled),
                MAX_MONITORED,
                self.poll_interval,
            )

    async def _supervisor(self) -> None:
        while not self._stopping:
            if not self._conn.connected:
                self._set_available(False)
                try:
                    await self._conn.open()
                    async with self._conn.lock:
                        await self._conn.command(p.build_command_packet(p.GROUP_SYSTEM, p.SYS_ARE_YOU_THERE))
                        await self._conn.configure_acks()
                    await self._restore_monitoring()
                    _LOGGER.info("Connected to ICT panel at %s:%s", self.host, self.port)
                    self._set_available(True)
                except ProtegeError as err:
                    _LOGGER.debug("Reconnect failed: %s", err)
                    await self._conn.close()
                    await asyncio.sleep(self.reconnect_interval)
                    continue
            try:
                await asyncio.wait_for(self._conn.closed.wait(), self.keepalive_interval)
                continue  # connection dropped
            except TimeoutError:
                pass
            try:
                async with self._conn.lock:
                    await self._conn.command(p.build_command_packet(p.GROUP_SYSTEM, p.SYS_ARE_YOU_THERE))
            except ProtegeError:
                _LOGGER.warning("ICT panel stopped responding; reconnecting")
                await self._conn.close()

    async def _poll_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.poll_interval)
            if not self._conn.connected:
                continue
            # Everything is re-read periodically, not just records beyond the monitoring limit.
            await self.refresh_all()

    async def _restore_monitoring(self, *, generation: int | None = None) -> bool:
        """(Re)register monitoring for all records; the panel resets it on login/logout.

        The panel takes roughly 80 ms per monitor request, so a large site takes several
        seconds. The connection lock is released between small batches so commands and
        read-backs are not stuck behind it. When `generation` is given and another command
        starts meanwhile (which resets monitoring again), stop and return False; that
        command restores monitoring itself.
        """
        packets = [p.build_monitor(r.record_type.group, r.index) for r in self._monitored]
        for start in range(0, len(packets), MONITOR_BATCH):
            async with self._conn.lock:
                if generation is not None and generation != self._command_generation:
                    return False
                replies = await self._conn.command_batch(packets[start : start + MONITOR_BATCH])
            for record, reply in zip(self._monitored[start : start + MONITOR_BATCH], replies):
                if reply.is_nack:
                    if reply.nack_code == p.NACK_USER_LOGOUT:
                        raise CommandRejected(reply.nack_code)
                    _LOGGER.debug("Monitoring %s rejected: %s", record.key, reply.nack_code)
        return True

    async def _restore_after_command(self, generation: int, refresh_delay: float) -> None:
        try:
            if not await self._restore_monitoring(generation=generation):
                return
        except ProtegeError as err:
            _LOGGER.warning("Could not restore monitoring after command: %s", err)
            await self._conn.close()
            return
        # Catch anything that changed while monitoring was being re-registered.
        await asyncio.sleep(refresh_delay)
        if generation == self._command_generation:
            await self.refresh_all()

    # ----- status and discovery -------------------------------------------

    async def request_status(self, record_type: RecordType, index: int) -> Status | None:
        """Ask for one record's status. Returns None if the record does not exist."""
        async with self._conn.lock:
            return await self._request_status_locked(record_type, index)

    async def _request_status_locked(
        self, record_type: RecordType, index: int, *, timeout: float | None = None
    ) -> Status | None:
        reply = await self._conn.request(
            p.build_record_command(record_type.group, p.SUB_STATUS, index),
            _status_matcher(record_type.group, index),
            timeout=timeout,
        )
        if reply.is_nack:
            if reply.nack_code == p.NACK_SERVICE_INDEX_NOT_VALID:
                return None
            # Logged out, no rights, or "command not valid". Callers decide whether to
            # log in or subscribe; only "index not valid" means the record is absent.
            raise CommandRejected(reply.nack_code)
        return self.status.get((record_type, index))

    async def _status_from_monitor_locked(
        self, record_type: RecordType, index: int, timeout: float
    ) -> Status | str:
        """Subscribe with force-update and read the push, then drop the subscription.

        GX firmware that NACKs an area status request (or never answers it) still sends the
        current state when monitoring is turned on with the force-update flag. Returns a
        Status, `_ABSENT` when the index does not exist, or `_INCONCLUSIVE`.
        """
        try:
            reply = await self._conn.request(
                p.build_monitor(record_type.group, index, start=True, force_update=True),
                _is_system,
                timeout=timeout,
            )
        except ProtegeTimeout:
            return _INCONCLUSIVE
        if reply.is_nack:
            if reply.nack_code == p.NACK_SERVICE_INDEX_NOT_VALID:
                return _ABSENT
            if reply.nack_code == p.NACK_USER_LOGOUT:
                raise CommandRejected(reply.nack_code)
            return _INCONCLUSIVE
        result: Status | str
        if (record_type, index) in self.status:
            result = self.status[(record_type, index)]
        else:
            try:
                await self._conn.wait_for(_status_matcher(record_type.group, index), timeout=timeout)
            except ProtegeTimeout:
                result = _INCONCLUSIVE
            else:
                result = self.status.get((record_type, index)) or _INCONCLUSIVE
        # Discovery must not leave a subscription behind for every index it tried.
        with contextlib.suppress(ProtegeError):
            await self._conn.command(
                p.build_monitor(record_type.group, index, start=False, force_update=False),
                timeout=min(timeout, 1.0),
            )
        return result

    async def discover(
        self,
        limits: dict[RecordType, int] | None = None,
        *,
        listed: dict[RecordType, list[int]] | None = None,
        extra: Iterable[tuple[RecordType, int]] | None = None,
        gap_tolerance: int = 64,
        probe_timeout: float = 1.0,
    ) -> list[Record]:
        """Find records on the panel.

        Probes start at record ID 0. `gap_tolerance` is how many missing IDs in a row end
        the scan of that type (the 2.0–2.2 default of 3 dropped a door at ID 0 and anything
        after a short hole). 0 scans every index up to the per-type limit. Requests are one
        at a time: a NACK is not tagged with an index, so overlapping probes would assign
        it to the wrong record. Only "index not valid" is a confirmed hole. A timeout or any
        other NACK is retried once logged in and then via a force-update subscription, and
        is not treated as proof the record is gone.

        With `listed` (record IDs per type from the WX web interface), the panel is first
        asked whether it numbers records by record ID (the GX/WX default) or by display
        order (ACPUseDisplayOrder). By record ID, the listed IDs are read and the gap scan
        is skipped. `extra` IDs (saved or typed in) are always probed, past the gap as well.
        """
        limits = limits or DEFAULT_DISCOVERY_LIMITS
        gap_tolerance = max(gap_tolerance, 0)
        found: list[Record] = []
        found_keys: set[tuple[RecordType, int]] = set()
        probed: set[tuple[RecordType, int]] = set()
        self.last_absent = set()
        self.numbering = NUMBERING_DISPLAY_ORDER
        logged_in = False

        async def login_once() -> bool:
            nonlocal logged_in
            if logged_in:
                return False
            await self._login(self.pin)
            logged_in = True
            return True

        async def ask(record_type: RecordType, index: int) -> Status | str:
            try:
                status = await self._request_status_locked(record_type, index, timeout=probe_timeout)
            except ProtegeTimeout:
                return _INCONCLUSIVE
            except CommandRejected as err:
                if err.code == p.NACK_SERVICE_INDEX_NOT_VALID:
                    return _ABSENT
                return _INCONCLUSIVE
            return status if status is not None else _ABSENT

        async def probe(record_type: RecordType, index: int) -> Status | str:
            result = await ask(record_type, index)
            if result == _INCONCLUSIVE and await login_once():
                result = await ask(record_type, index)
            if result == _INCONCLUSIVE:
                try:
                    result = await self._status_from_monitor_locked(record_type, index, probe_timeout)
                except CommandRejected as err:
                    if err.code == p.NACK_USER_LOGOUT and await login_once():
                        try:
                            result = await self._status_from_monitor_locked(
                                record_type, index, probe_timeout
                            )
                        except ProtegeError:
                            return _INCONCLUSIVE
                    else:
                        return _INCONCLUSIVE
                except ProtegeTimeout:
                    return _INCONCLUSIVE
            return result

        def add(record_type: RecordType, index: int, status: Status) -> None:
            key = (record_type, index)
            if key in found_keys:
                return
            found_keys.add(key)
            found.append(Record(record_type, index, reference=getattr(status, "reference", None) or None))

        async def consider(record_type: RecordType, index: int) -> bool:
            """Probe one index. True when a record is there."""
            key = (record_type, index)
            if index < 0 or key in probed:
                return key in found_keys
            probed.add(key)
            result = await probe(record_type, index)
            if isinstance(result, str):
                if result == _ABSENT:
                    self.last_absent.add(key)
                return False
            add(record_type, index, result)
            return True

        async with self._conn.lock:
            try:
                if listed and await self._numbered_by_record_id(listed, probe):
                    self.numbering = NUMBERING_RECORD_ID
                    for record_type in MONITOR_PRIORITY:
                        for index in listed.get(record_type, []):
                            await consider(record_type, index)
                else:
                    for record_type in MONITOR_PRIORITY:
                        misses = 0
                        unanswered = 0
                        for index in range(limits.get(record_type, 0) + 1):
                            if await consider(record_type, index):
                                misses = 0
                                unanswered = 0
                                continue
                            misses += 1
                            if (record_type, index) not in self.last_absent:
                                unanswered += 1
                            if gap_tolerance > 0 and misses >= gap_tolerance:
                                if unanswered == misses:
                                    _LOGGER.warning(
                                        "%s discovery stopped at index %s after %s unanswered probes",
                                        record_type.value,
                                        index,
                                        unanswered,
                                    )
                                break
                for record_type, index in extra or ():
                    await consider(record_type, index)
            finally:
                if logged_in:
                    with contextlib.suppress(ProtegeError):
                        await self._logout()
        if self.numbering != NUMBERING_RECORD_ID and _ids_use_record_numbers(found):
            self.numbering = NUMBERING_RECORD_ID
        if logged_in and self._monitored:
            # Logging in and out cleared monitoring on the panel.
            await self._restore_monitoring()
        return found

    @staticmethod
    async def _numbered_by_record_id(
        listed: dict[RecordType, list[int]],
        probe: Callable[[RecordType, int], Awaitable[Status | str]],
    ) -> bool:
        """One probe tells: a listed ID above the record count exists only when numbered by record ID.

        When every type's IDs are simply 1..n the two numberings agree, so display order is used.
        """
        for record_type in MONITOR_PRIORITY:
            ids = listed.get(record_type) or []
            if ids and sorted(ids) != list(range(1, len(ids) + 1)):
                return not isinstance(await probe(record_type, max(ids)), str)
        return False

    # ----- commands -------------------------------------------------------

    async def _login(self, pin: str) -> None:
        try:
            raw = p.build_login(pin)
        except ValueError as err:
            raise InvalidPin(p.NACK_USER_INVALID) from err
        try:
            await self._conn.command(raw)
        except CommandRejected as err:
            if err.code != p.NACK_USER_LOGIN:
                raise
            # Someone is still logged in on this connection; reset and retry.
            await self._logout()
            await self._conn.command(raw)

    async def _logout(self) -> None:
        with contextlib.suppress(CommandRejected):
            await self._conn.command(p.build_command_packet(p.GROUP_SYSTEM, p.SYS_LOGOUT))

    async def _run_command(
        self,
        raw: bytes,
        pin: str | None = None,
        *,
        assumed: Status | None = None,
        readback: tuple[float, ...] = (0.5, 1.5, 3.0, 6.0, 10.0),
    ) -> None:
        """Log in, send one command, log out, and restore monitoring.

        `assumed` is shown straight away once the panel acknowledges the command, so the
        UI responds instantly; read-backs from the panel then confirm or correct it.
        """
        if not self._conn.connected:
            raise CannotConnect("Not connected to the panel")
        error: ProtegeError | None = None
        async with self._conn.lock:
            self._command_generation += 1
            generation = self._command_generation
            await self._login(pin or self.pin)
            try:
                await self._conn.command(raw)
            except CommandRejected as err:
                if err.code not in p.NACK_HARMLESS:
                    error = err
            finally:
                with contextlib.suppress(ProtegeError):
                    await self._logout()
        target = _command_target(raw)
        if error is None and assumed is not None and target is not None:
            self._apply(StatusUpdate(target[0], target[1], assumed, reported=False))
        # Changes made while logged in are not pushed, and not every panel honours the
        # "force update" monitoring flag. Read the commanded record back straight away and
        # again over the next few seconds (doors and outputs change on timers), while
        # monitoring is re-registered in the background.
        if target is not None:
            await self._refresh(*target)
            self._spawn(self._refresh_later(target, readback))
        self._spawn(self._restore_after_command(generation, 2.0))
        if error is not None:
            raise error

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _refresh(self, record_type: RecordType, index: int) -> None:
        with contextlib.suppress(ProtegeError):
            await self.request_status(record_type, index)

    async def _refresh_later(self, target: tuple[RecordType, int], delays: tuple[float, ...]) -> None:
        elapsed = 0.0
        for delay in delays:
            await asyncio.sleep(delay - elapsed)
            elapsed = delay
            if self._stopping or not self._conn.connected:
                return
            await self._refresh(*target)

    def _apply(self, update: StatusUpdate) -> None:
        self.status[(update.record_type, update.index)] = update.status
        for listener in list(self._status_listeners):
            listener(update)

    def _assumed_door(self, index: int, lock_state: DoorLockState) -> DoorStatus:
        current = self.status.get((RecordType.DOOR, index))
        door_state = current.door_state if isinstance(current, DoorStatus) else 0
        return DoorStatus(lock_state, door_state)

    async def ensure_status(self) -> None:
        """Read records that monitoring did not push, logging in if a logged-out read fails.

        `start` only subscribes. The force-update flag is not honoured for areas on every
        GX build, and a logged-out area status request can NACK or time out while inputs
        still answer, so the alarm panel otherwise stays unknown. One failed record does
        not skip the rest, and a logout NACK does not abandon the poll.
        """
        if not self._conn.connected:
            return
        pending = [
            r
            for r in self.records.values()
            if r.key not in self.status and r.key not in self._absent_status
        ]
        if not pending:
            return
        still: list[Record] = []
        for record in pending:
            if self._stopping or not self._conn.connected:
                return
            try:
                found = await self.request_status(record.record_type, record.index)
            except ProtegeError as err:
                _LOGGER.debug(
                    "Status read failed for %s %s: %s", record.record_type.value, record.index, err
                )
                still.append(record)
                continue
            if found is None:
                self._absent_status.add(record.key)
        if not still or not self._conn.connected:
            return
        _LOGGER.debug("Reading %s records while logged in; they had no status yet", len(still))
        generation: int | None = None
        try:
            async with self._conn.lock:
                self._command_generation += 1
                generation = self._command_generation
                await self._login(self.pin)
                try:
                    for record in still:
                        try:
                            found = await self._request_status_locked(
                                record.record_type, record.index, timeout=2.0
                            )
                        except ProtegeError:
                            continue
                        if found is None:
                            self._absent_status.add(record.key)
                finally:
                    with contextlib.suppress(ProtegeError):
                        await self._logout()
        except ProtegeError as err:
            _LOGGER.warning("Could not read status while logged in: %s", err)
        if generation is not None and self._conn.connected:
            with contextlib.suppress(ProtegeError):
                await self._restore_monitoring(generation=generation)

    async def refresh_all(self) -> None:
        """Read the status of every record (safety net for missed pushes)."""
        for record in list(self.records.values()):
            if self._stopping or not self._conn.connected:
                return
            try:
                await self.request_status(record.record_type, record.index)
            except ProtegeTimeout:
                continue
            except CommandRejected as err:
                _LOGGER.debug(
                    "Status refresh skipped %s %s: %s", record.record_type.value, record.index, err
                )
                continue
            except ProtegeError as err:
                _LOGGER.debug("Status refresh stopped: %s", err)
                return
        await self.ensure_status()

    async def door_lock(self, index: int) -> None:
        await self._run_command(
            p.build_record_command(p.GROUP_DOOR, p.DOOR_LOCK, index),
            assumed=self._assumed_door(index, DoorLockState.LOCKED),
        )

    async def door_unlock(self, index: int) -> None:
        """Momentary unlock. The panel relocks after the door's programmed time.

        The command has no duration field (ACS door subcommand 0x01, and the web
        interface's Unlock Door). A caller-chosen time has to latch and lock later.
        """
        # Read back for longer so the automatic relock shows up.
        await self._run_command(
            p.build_record_command(p.GROUP_DOOR, p.DOOR_UNLOCK_MOMENTARY, index),
            assumed=self._assumed_door(index, DoorLockState.UNLOCKED_USER),
            readback=(0.5, 1.5, 3.0, 6.0, 10.0, 20.0, 35.0, 60.0),
        )

    async def door_unlock_latched(self, index: int) -> None:
        await self._run_command(
            p.build_record_command(p.GROUP_DOOR, p.DOOR_UNLOCK_LATCHED, index),
            assumed=self._assumed_door(index, DoorLockState.UNLOCKED_LATCHED),
        )

    async def area_command(self, index: int, sub_command: int, code: str) -> None:
        await self._run_command(p.build_record_command(p.GROUP_AREA, sub_command, index), pin=code)

    async def output_on(self, index: int, seconds: int | None = None) -> None:
        if seconds:
            extra = int(seconds).to_bytes(2, "little")
            raw = p.build_record_command(p.GROUP_OUTPUT, p.OUTPUT_ON_TIMED, index, extra)
        else:
            raw = p.build_record_command(p.GROUP_OUTPUT, p.OUTPUT_ON, index)
        await self._run_command(raw, assumed=self._assumed_output(index, OutputState.ON))

    async def output_off(self, index: int) -> None:
        await self._run_command(
            p.build_record_command(p.GROUP_OUTPUT, p.OUTPUT_OFF, index),
            assumed=self._assumed_output(index, OutputState.OFF),
        )

    def _assumed_output(self, index: int, state: OutputState) -> OutputStatus | None:
        current = self.status.get((RecordType.OUTPUT, index))
        if not isinstance(current, OutputStatus):
            return None
        return OutputStatus(current.reference, state)

    async def set_bypass(
        self, record_type: RecordType, index: int, bypass: bool, *, permanent: bool = False
    ) -> None:
        if record_type not in (RecordType.INPUT, RecordType.TROUBLE_INPUT):
            raise ValueError("Only inputs and trouble inputs can be bypassed")
        if not bypass:
            sub = p.INPUT_BYPASS_REMOVE
        elif permanent:
            sub = p.INPUT_BYPASS_PERMANENT
        else:
            sub = p.INPUT_BYPASS_TEMPORARY
        assumed = None
        current = self.status.get((record_type, index))
        if isinstance(current, InputStatus):
            flags = current.bypass_flags & ~0x03
            if bypass:
                flags |= 0x03 if permanent else 0x01
            assumed = InputStatus(current.reference, current.state, flags)
        await self._run_command(p.build_record_command(record_type.group, sub, index), assumed=assumed)


def _ids_use_record_numbers(found: list[Record]) -> bool:
    """True when IDs are not 1..n with no holes (display order). ID 0 and gaps are record IDs."""
    by_type: dict[RecordType, list[int]] = {}
    for record in found:
        by_type.setdefault(record.record_type, []).append(record.index)
    for indexes in by_type.values():
        ordered = sorted(indexes)
        if ordered != list(range(1, len(ordered) + 1)):
            return True
    return False


EventListener = Callable[[PanelEvent], None]


class ProtegeEventClient:
    """Read-only client that stays logged in to receive system events.

    Point it at a separate ACS service and log in with a user that has no
    control rights. It never sends control commands.
    """

    def __init__(
        self,
        host: str,
        port: int,
        pin: str,
        *,
        timeout: float = 5.0,
        keepalive_interval: float = 60.0,
        reconnect_interval: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.pin = pin
        self.keepalive_interval = keepalive_interval
        self.reconnect_interval = reconnect_interval
        self._conn = ProtegeConnection(host, port, on_data=self._handle_data, timeout=timeout)
        self._listeners: list[EventListener] = []
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._warned = False
        self.connected = False

    def add_listener(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _handle_data(self, packet: p.ICTPacket) -> None:
        # The connection acks pushed data packets.
        for block in p.iter_data_blocks(packet.data):
            event = decode_event(block)
            if event is None:
                _LOGGER.debug("Ignoring data block %s on the events service", block.data_type)
                continue
            for listener in list(self._listeners):
                listener(event)

    async def open(self) -> None:
        """Connect, log in, and subscribe to events. Raises on failure."""
        await self._conn.open()
        try:
            async with self._conn.lock:
                await self._conn.configure_acks()
                await self._conn.command(p.build_login(self.pin))
                # Some firmware rejects these; the service may already be set to send events.
                for raw, what in (
                    (p.build_set_login_time(6000), "login time"),
                    (p.build_request_events(start=True, human_readable=True), "event request"),
                ):
                    try:
                        await self._conn.command(raw)
                    except CommandRejected as err:
                        _LOGGER.debug("Events service rejected the %s: %s", what, err)
        except (ProtegeError, ValueError):
            await self._conn.close()
            raise
        self.connected = True

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._supervisor(), name="ict_protege_events")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        with contextlib.suppress(ProtegeError):
            if self._conn.connected:
                async with self._conn.lock:
                    await self._conn.command(p.build_command_packet(p.GROUP_SYSTEM, p.SYS_LOGOUT), timeout=1)
        await self._conn.close()
        self.connected = False

    async def _supervisor(self) -> None:
        while not self._stopping:
            if not self._conn.connected:
                self.connected = False
                try:
                    await self.open()
                    _LOGGER.info("Receiving ICT events from %s:%s", self.host, self.port)
                    self._warned = False
                except (ProtegeError, ValueError) as err:
                    if not self._warned:
                        _LOGGER.warning("Event logging on %s:%s failed: %s", self.host, self.port, err)
                        self._warned = True
                    else:
                        _LOGGER.debug("Event connection failed: %s", err)
                    await asyncio.sleep(self.reconnect_interval)
                    continue
            try:
                await asyncio.wait_for(self._conn.closed.wait(), self.keepalive_interval)
                continue
            except TimeoutError:
                pass
            try:
                async with self._conn.lock:
                    await self._conn.command(p.build_command_packet(p.GROUP_SYSTEM, p.SYS_ARE_YOU_THERE))
            except ProtegeError:
                await self._conn.close()
