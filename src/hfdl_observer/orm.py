# hfdl_observer/orm.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import collections
import contextlib
import datetime
import functools
import itertools
import logging

from typing import Any, Callable, Generator, Iterable, Mapping, Optional, Sequence, Type

import pony.orm  # type: ignore[import-not-found]
import pony.orm.core  # type: ignore[import-not-found]

import hfdl_observer.bus as bus
import hfdl_observer.hfdl as hfdl
import hfdl_observer.network as network
import hfdl_observer.data as data
import hfdl_observer.util as util


logger = logging.getLogger(__name__)

db = pony.orm.Database()
db.bind(provider='sqlite', filename=':sharedmemory:')


TS_FACTOR = 1_000_000.0


def to_datetime(timestamp: int) -> datetime.datetime:
    return util.timestamp_to_datetime(timestamp / TS_FACTOR)


def to_datetime_or_none(timestamp: int | None) -> datetime.datetime | None:
    if timestamp is None:
        return None
    return to_datetime(timestamp)


def to_timestamp(when: datetime.datetime) -> int:
    return int(util.datetime_to_timestamp(when) * TS_FACTOR)


def to_timestamp_or_none(when: None | datetime.datetime) -> int | None:
    if when is None:
        return None
    return to_timestamp(when)


@functools.cache
@pony.orm.db_session(strict=True)
def pagesize() -> int:
    return int(db.execute("PRAGMA page_size;").fetchone()[0])


# Using `db.Entity` directly won't work, as both MyPy and Pyright won't
# allow inheriting a class from a variable. For Pyright this declaration
# is enough misdirection for it not to complain, but MyPy needs an extra
# `type: ignore` comment above each model declaration to work.
#  DbEntity = db.Entity
# The more clever way to do this is to respecify the type of the db.Entity attribute. mypy behaves much better.
DbEntity: Type[pony.orm.core.Entity] = db.Entity
# However there are still a couple of snags. mypy won't see the `__iter__` injected by the metaclass EntityMeta,
# but we could wrap those calls as `iter(MyEntity)`. That has a further snag, since the EntityIter class is not
# self iterable. There are valid reasons for doing it that way, but since I know I'm not going to be doing nasty
# things, I can patch the class to add the necessary dunder.
pony.orm.core.EntityIter.__iter__ = lambda self: self
# any now mypy is happy with no further ignores or other funkiness.


class Untracked:
    to_dict: Callable[[], dict]

    def untracked_dict(self) -> dict:
        return {k: getattr(v, 'get_untracked', lambda: v)() for k, v in self.to_dict().items()}


class StationAvailability(DbEntity, Untracked):
    station_id = pony.orm.Required(int)
    stratum = pony.orm.Required(int)
    frequencies = pony.orm.Required(pony.orm.IntArray)
    agent = pony.orm.Required(str)
    from_station = pony.orm.Optional(int)
    valid_at_frame = pony.orm.Required(int, size=32)
    valid_to_frame = pony.orm.Optional(int, size=32)
    # SELECT * from Availability where sid = ? and valid_to > NOW order by stratum desc, valid_at desc
    pony.orm.PrimaryKey(station_id, stratum, valid_at_frame)
    pony.orm.composite_index(station_id, stratum, valid_at_frame, valid_to_frame)

    def as_local(self) -> network.StationAvailability:
        d = self.untracked_dict()
        d['when'] = to_datetime(util.timestamp_from_pseudoframe(self.valid_to_frame)) if self.valid_to_frame else None
        return network.StationAvailability(**d)

    @classmethod
    def prune(cls) -> None:
        util.schedule(cls._prune())

    @classmethod
    async def _prune(cls) -> None:
        await util.in_db_thread(cls._prune_before)

    @classmethod
    @pony.orm.db_session(strict=True, retry=3)
    def _prune_before(cls) -> None:
        before_prune = pony.orm.count(a for a in iter(StationAvailability))
        horizon = util.pseudoframe(util.now() - datetime.timedelta(days=1))
        pony.orm.delete(a for a in iter(StationAvailability) if a.valid_to_frame is not None and a.valid_to_frame < horizon)
        pony.orm.delete(a for a in iter(StationAvailability) if a.valid_to_frame is None and a.valid_at_frame < horizon)
        pages = int(db.execute("PRAGMA page_count;").fetchone()[0])
        # logger.debug(f'DB size is {pages * pagesize()}')
        after_prune = pony.orm.count(a for a in iter(StationAvailability))
        if after_prune < before_prune:
            logger.info(f'pruned {before_prune - after_prune} StationAvailability ({after_prune} records, {pages} pages)')

    @classmethod
    async def add(cls, base: network.StationAvailability) -> bool:
        ret: bool = await util.in_db_thread(cls._add, base)
        return ret

    @classmethod
    @pony.orm.db_session(strict=True, retry=3)
    def _add(cls, base: network.StationAvailability) -> bool:
        try:
            a = StationAvailability.__getitem__((base.station_id, base.stratum, base.valid_at_frame))
        except pony.orm.ObjectNotFound:
            StationAvailability(
                station_id=base.station_id,
                stratum=base.stratum,
                frequencies=base.frequencies,
                agent=base.agent,
                from_station=base.from_station,
                valid_at_frame=base.valid_at_frame,
                valid_to_frame=base.valid_to_frame,
                # when=base.valid_at_frame,
            )
            return True
        else:
            if a.frequencies != base.frequencies:
                if a.frequencies:
                    logger.debug(f'{base.station_id} has updated frequencies? {a.frequencies} to {base.frequencies}')
                if base.frequencies:
                    a.frequencies = base.frequencies
            if base.valid_to_frame is not None and a.valid_to_frame != base.valid_to_frame:
                logger.debug(f'{base.station_id} has updated valid_to {a.valid_to_frame} to {base.valid_to_frame}')
                a.valid_to_frame = base.valid_to_frame
        return False


class ReceivedPacket(DbEntity, Untracked):
    when = pony.orm.Required(int, size=64)
    agent = pony.orm.Required(str)
    ground_station = pony.orm.Required(int)
    frequency = pony.orm.Required(int)
    kind = pony.orm.Optional(str)
    uplink = pony.orm.Required(bool)
    latitude = pony.orm.Optional(float)
    longitude = pony.orm.Optional(float)
    receiver = pony.orm.Required(str)
    # select when // 60, frequency, count(*) from ReceivedPacket where when > ? group by when // 60, frequency
    pony.orm.PrimaryKey(when, frequency)

    def as_local(self) -> data.ReceivedPacket:
        d = self.untracked_dict()
        d['when'] = to_datetime(self.when)
        return data.ReceivedPacket(**d)

    @classmethod
    def prune(cls, before: datetime.datetime) -> None:
        util.schedule(cls._prune(before))

    @classmethod
    async def _prune(cls, before: datetime.datetime) -> None:
        await util.in_db_thread(cls._prune_before, before)

    @classmethod
    @pony.orm.db_session(strict=True, retry=3)
    def _prune_before(cls, before: datetime.datetime) -> None:
        try:
            before_prune = pony.orm.count(r for r in iter(ReceivedPacket))
            horizon = to_timestamp(before)
            # initial = int(db.execute("PRAGMA page_count;").fetchone()[0])
            pony.orm.delete(p for p in iter(ReceivedPacket) if p.when < horizon)
            after = int(db.execute("PRAGMA page_count;").fetchone()[0])
            # logger.debug(f'DB size was {initial * pagesize()}, now {after * pagesize()}')
            after_prune = pony.orm.count(r for r in iter(ReceivedPacket))
            if after_prune < before_prune:
                logger.info(f'pruned {before_prune - after_prune} ReceivedPackets ({after_prune} records, {after} pages)')
        except Exception as err:
            logger.error('cannot prune', exc_info=err)


# Not currently used
class FrequencyWatch(DbEntity, Untracked):
    sequence = pony.orm.PrimaryKey(int, auto=True)
    started = pony.orm.Required(int, size=64)
    ended = pony.orm.Optional(int, size=64)
    station_id = pony.orm.Required(int)
    frequency = pony.orm.Required(int)
    is_targetted = pony.orm.Required(bool)
    observer_id = pony.orm.Optional(str)

    def as_local(self) -> data.FrequencyWatch:
        d = self.untracked_dict()
        d['started'] = to_datetime(self.started)
        d['ended'] = to_datetime_or_none(self.ended)
        return data.FrequencyWatch(**d)

    @classmethod
    @pony.orm.db_session(strict=True, retry=3)
    def prune(cls, before: datetime.datetime) -> None:
        horizon = to_timestamp(before)
        pony.orm.delete(p for p in iter(cls) if p.ended < horizon)


# after all entities have been created
db.generate_mapping(create_tables=True)


class NetworkUpdater(network.AbstractNetworkUpdater):
    async def add(self, availability: network.StationAvailability) -> bool:
        return await StationAvailability.add(availability)

    # async def updated(self, availabilities: Optional[Sequence[network.StationAvailability]] = None) -> None:
    #     db.commit()
    #     super().updated(availabilities)

    # wrap in a session for database access on delegated functions.
    @pony.orm.db_session(strict=True, retry=3)
    def on_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        try:
            super().on_hfdl(packet_info)
        except Exception as err:
            logger.error('HFDL packet processing', exc_info=err)

    # wrap in a session for database access on delegated functions.
    @pony.orm.db_session(strict=True, retry=3)
    def on_community(self, airframes: dict) -> None:
        try:
            super().on_community(airframes)
        except Exception as err:
            logger.error('community update processing', exc_info=err)

    # wrap in a session for database access on delegated functions.
    @pony.orm.db_session(strict=True, retry=3)
    def on_systable(self, station_table: str) -> None:
        super().on_systable(station_table)

    def _for_station(
        self, station_id: int, at: Optional[datetime.datetime] = None
    ) -> Optional[network.StationAvailability]:
        q = pony.orm.select(
            a for a in iter(StationAvailability)
            if a.station_id == station_id
        )
        pseudoframe = util.pseudoframe(at or util.now())
        q = q.where(lambda a: a.valid_at_frame <= pseudoframe)
        q = q.where(lambda a: a.valid_to_frame is None or a.valid_to_frame >= pseudoframe)
        q = q.sort_by(pony.orm.desc(StationAvailability.stratum), pony.orm.desc(StationAvailability.valid_at_frame))
        row = q.first()
        local_row = row.as_local() if row else None
        del q
        return local_row

    async def active(self, at: Optional[datetime.datetime] = None) -> Sequence[network.StationAvailability]:
        r: Sequence[network.StationAvailability] = await util.in_db_thread(self._active, at)
        return r

    @pony.orm.db_session(strict=True)
    def _active(self, at: Optional[datetime.datetime] = None) -> Sequence[network.StationAvailability]:
        found = []
        sids = network.STATIONS.by_id.keys()
        for sid in sids:
            la = self._for_station(sid, at=at)
            if la:
                found.append(la)
        return found

    async def current(self) -> Sequence[network.StationAvailability]:
        r: Sequence[network.StationAvailability] = await util.in_db_thread(self._active)
        return r

    async def current_freqs(self) -> Sequence[int]:
        current = await self.current()
        return list(itertools.chain(*[a.frequencies for a in current]))

    def prune(self, _: Any = None) -> None:
        StationAvailability.prune()


class PacketWatcher(data.AbstractPacketWatcher):
    periodic_task: Optional[asyncio.Task] = None

    def on_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        util.schedule(self.add_packet(packet_info))

    async def add_packet(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        await util.in_db_thread(self._add_packet, packet_info)

    @pony.orm.db_session(strict=True, retry=3)
    def _add_packet(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        position = packet_info.position or (None, None)
        try:
            ReceivedPacket(
                when=to_timestamp(util.now()),
                agent=packet_info.station or '(unknown)',
                ground_station=packet_info.ground_station['id'],
                frequency=packet_info.frequency,
                kind='spdu' if packet_info.is_squitter else 'lpdu',
                uplink=packet_info.is_uplink,
                latitude=position[0],
                longitude=position[1],
                receiver=network.receiver_for(packet_info.frequency),
            )
        except TypeError as exc:
            logger.info(f'bad packet? {exc}\n{packet_info.packet}')
        db.commit()

    def recent_packets_list(cls, since: datetime.datetime) -> Sequence[ReceivedPacket]:
        when = to_timestamp(since)
        q = pony.orm.select(r for r in iter(ReceivedPacket)).where(lambda r: r.when > when)
        results: Sequence[ReceivedPacket] = q[:]
        del q
        return results

    @contextlib.contextmanager
    # def recent_packets(cls, since: datetime.datetime) -> Iterable[ReceivedPacket]:
    # That should be an acceptable type hint but mypy doesn't like it. The "correct" way for mypy follows
    def recent_packets(cls, since: datetime.datetime) -> Generator[Iterable[ReceivedPacket], None, None]:
        when = to_timestamp(since)
        q = pony.orm.select(r for r in iter(ReceivedPacket)).where(lambda r: r.when > when)
        yield q
        del q

    def prune(self) -> None:
        ReceivedPacket.prune(util.now() - datetime.timedelta(days=1))

    def prune_every(self, period: int) -> None:
        if self.periodic_task:
            self.periodic_task.cancel()
        periodic_callback = bus.PeriodicCallback(period, [self.prune], False)
        self.periodic_task = asyncio.get_event_loop().create_task(periodic_callback.run())

    async def stop_pruning(self) -> None:
        if self.periodic_task:
            self.periodic_task.cancel()
            await self.periodic_task
            self.periodic_task = None

    @pony.orm.db_session(strict=True)
    def binned_recent_packets(self, since: datetime.datetime, bin_size: int) -> Iterable[tuple[int, ReceivedPacket]]:
        when_ts = to_timestamp(since)
        recent_packets: Iterable[ReceivedPacket]
        with self.recent_packets(since) as recent_packets:
            for packet in recent_packets:
                bin_number = int((when_ts - packet.when) // TS_FACTOR // bin_size)
                yield bin_number, packet

    async def packets_by_frequency(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        r: Mapping[int | str, Sequence[int]] = await util.in_db_thread(self._packets_by_frequency, bin_size, num_bins)
        return r

    def _packets_by_frequency(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        data: dict[int | str, list[int]] = collections.defaultdict(lambda: [0] * num_bins)
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        for bin_number, packet in self.binned_recent_packets(when, bin_size):
            data[packet.frequency][bin_number] += 1
        return data

    async def packets_by_agent(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        r: Mapping[int | str, Sequence[int]] = await util.in_db_thread(self._packets_by_agent, bin_size, num_bins)
        return r

    def _packets_by_agent(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        data: dict[int | str, list[int]] = collections.defaultdict(lambda: [0] * num_bins)
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        for bin_number, packet in self.binned_recent_packets(when, bin_size):
            data[packet.agent or 'unknown'][bin_number] += 1
        return data

    async def packets_by_station(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        r: Mapping[int | str, Sequence[int]] = await util.in_db_thread(self._packets_by_station, bin_size, num_bins)
        return r

    def _packets_by_station(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        data: dict[int | str, list[int]] = collections.defaultdict(lambda: [0] * num_bins)
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        for bin_number, packet in self.binned_recent_packets(when, bin_size):
            station = network.STATIONS[packet.frequency]
            data[station.station_id][bin_number] += 1
        return data

    async def packets_by_band(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        r: Mapping[int | str, Sequence[int]] = await util.in_db_thread(self._packets_by_band, bin_size, num_bins)
        return r

    def _packets_by_band(self, bin_size: int, num_bins: int) -> Mapping[int | str | str, Sequence[int]]:
        data: dict[int | str, list[int]] = collections.defaultdict(lambda: [0] * num_bins)
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        for bin_number, packet in self.binned_recent_packets(when, bin_size):
            data[packet.frequency // 1000][bin_number] += 1
        return data

    async def packets_by_frequency_set(
        self, bin_size: int, num_bins: int, frequency_sets: dict[int, str]
    ) -> Mapping[int | str, Sequence[int]]:
        r: Mapping[int | str, Sequence[int]] = await util.in_db_thread(self._packets_by_frequency_set, bin_size, num_bins)
        return r

    def _packets_by_frequency_set(
        self, bin_size: int, num_bins: int, frequency_sets: dict[int, str]
    ) -> Mapping[int | str | str, Sequence[int]]:
        data: dict[int | str, list[int]] = collections.defaultdict(lambda: [0] * num_bins)
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        for bin_number, packet in self.binned_recent_packets(when, bin_size):
            data[frequency_sets.get(packet.frequency, str(packet.frequency))][bin_number] += 1
        return data

    async def packets_by_receiver(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        r: Mapping[int | str, Sequence[int]] = await util.in_db_thread(self._packets_by_receiver, bin_size, num_bins)
        return r

    def _packets_by_receiver(self, bin_size: int, num_bins: int) -> Mapping[int | str, Sequence[int]]:
        data: dict[int | str, list[int]] = collections.defaultdict(lambda: [0] * num_bins)
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        for bin_number, packet in self.binned_recent_packets(when, bin_size):
            data[packet.receiver][bin_number] += 1
        return data
