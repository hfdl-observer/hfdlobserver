# hfdl_observer/ormless.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
from __future__ import annotations

import asyncio
import collections
import dataclasses
import datetime
import functools
import inspect
import itertools
import logging
import sqlite3
import threading
from typing import Iterable, Mapping, Optional, Sequence, Type, TypeVar

import hfdl_observer.data as data
import hfdl_observer.hfdl as hfdl

# import hfdl_observer.messaging as messaging
import hfdl_observer.network as network
import hfdl_observer.settings as settings
import hfdl_observer.util as util

logger = logging.getLogger(__name__)

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


# an explicit thread lock is required to protect DDL and DML since we use 1 connection per thread.
# Queries run fine without locks.
db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    try:
        _db: sqlite3.Connection = util.thread_local.db
    except AttributeError:
        dburi = settings.db["uri"]
        try:
            # The db_lock keeps initialize_db from being called multiple times on top of each other (or DML).
            # db() should only be called a handful of times (one for each thread in the db executor pool).
            with db_lock:
                _db = util.thread_local.db = sqlite3.connect(dburi, uri=True, check_same_thread=True)
                StationAvailability._table(_db)
                ReceivedPacket._table(_db)
        except Exception as err:
            logger.error(f"Error opening database: {err}", exc_info=err)
            util.shutdown()
    return _db


@dataclasses.dataclass
class Table:
    @classmethod
    def factory(cls, cursor: sqlite3.Cursor, row: Sequence) -> Table:
        fields = [column[0] for column in cursor.description]
        data = {k.strip("_"): v for k, v in zip(fields, row)}
        cls.preprocess_data(data)
        return cls(**data)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def preprocess_data(cls, data: dict) -> None:
        pass

    @classmethod
    def _already_ran(cls, _db: sqlite3.Connection) -> None:
        pass


@functools.cache
def pagesize() -> int:  # currently unused.
    return int(db().execute("PRAGMA page_size;").fetchone()[0])


@dataclasses.dataclass
class StationAvailability(Table):
    station_id: int
    stratum: int
    frequencies: str
    agent: str
    from_station: str | None
    valid_at_frame: int
    valid_to_frame: int | None

    @classmethod
    def preprocess_data(cls, data: dict) -> None:
        data["frequencies"] = [int(f) for f in data["frequencies"].split(",") if f != ""]

    @classmethod
    def _table(cls, _db: sqlite3.Connection) -> None:
        # this method should run only once.
        cls._table = cls._already_ran  # type: ignore[method-assign]
        _db.execute("""
            CREATE TABLE IF NOT EXISTS StationAvailability (
            station_id INTEGER NOT NULL,
            stratum INTEGER NOT NULL,
            frequencies TEXT NOT NULL,
            agent TEXT NOT NULL,
            from_station INTEGER NULL,
            valid_at_frame INTEGER NOT NULL,
            valid_to_frame INTEGER NULL,
            PRIMARY KEY(station_id, stratum, valid_at_frame)
            );
        """)
        _db.execute("""
            CREATE INDEX IF NOT EXISTS index_StationAvailability
            ON StationAvailability (station_id, valid_at_frame, valid_to_frame, stratum);
        """)
        _db.execute("""
            CREATE TRIGGER IF NOT EXISTS StationAvailabilityPrune AFTER INSERT on StationAvailability
            BEGIN
                DELETE FROM StationAvailability
                WHERE coalesce(valid_to_frame, valid_at_frame)
                      < coalesce(NEW.valid_to_frame, NEW.valid_at_frame) - 86400 / 32;
            END;
        """)
        _db.execute("REINDEX StationAvailability;")

    def as_local(self) -> network.StationAvailability:
        d = self.to_dict()  # untracked_dict()
        d["when"] = to_datetime(util.timestamp_from_pseudoframe(self.valid_to_frame)) if self.valid_to_frame else None
        return network.StationAvailability(**d)

    @classmethod
    async def add(cls, base: network.StationAvailability) -> bool:
        ret: bool = await util.in_db_thread(cls._add, base)
        return ret

    @classmethod
    def _add(cls, base: network.StationAvailability) -> bool:
        sql = "REPLACE INTO StationAvailability "
        sql += "(station_id, stratum, frequencies, agent, from_station, valid_at_frame, valid_to_frame) "
        sql += "VALUES(?, ?, ?, ?, ?, ?, ?);"
        with db() as conn:
            with db_lock:
                data = (
                    base.station_id,
                    base.stratum,
                    ",".join(str(f) for f in base.frequencies),
                    base.agent,
                    base.from_station,
                    base.valid_at_frame,
                    base.valid_to_frame,
                )
                conn.execute(sql, data)
        return True

    @classmethod
    def _station_at(
        cls, station_id: int, at: Optional[datetime.datetime] = None
    ) -> Optional[network.StationAvailability]:
        pseudoframe = util.pseudoframe(at or util.now())
        sql = """
        SELECT * from StationAvailability
        WHERE station_id = ?
        AND valid_at_frame <= ?
        AND (valid_to_frame IS NULL or valid_to_Frame >= ?)
        ORDER BY stratum DESC, valid_at_frame DESC
        LIMIT 1;
        """
        with db() as conn:
            conn.row_factory = cls.factory
            row = conn.execute(sql, (station_id, pseudoframe, pseudoframe)).fetchone()
            local_row = row.as_local() if row else None
        return local_row


QUERY_FRAGMENTS = {
    "trunc(frequency / 1000)",
}
BinCounterT = TypeVar("BinCounterT")
FrequencyStationCounter = collections.namedtuple("FrequencyStationCounter", ["frequency", "ground_station", "count"])
BandCounter = collections.namedtuple("BandCounter", ["band", "count"])
AgentCounter = collections.namedtuple("AgentCounter", ["agent", "count"])
StationCounter = collections.namedtuple("StationCounter", ["ground_station", "count"])
ReceiverCounter = collections.namedtuple("ReceiverCounter", ["receiver", "count"])


@dataclasses.dataclass
class ReceivedPacket(Table):
    received: int
    agent: str
    ground_station: int
    frequency: int
    kind: str | None
    uplink: bool
    latitude: float | None
    longitude: float | None
    receiver: str
    freq_active: bool | None

    @classmethod
    def _table(cls, _db: sqlite3.Connection) -> None:
        # this method should run only once.
        cls._table = cls._already_ran  # type: ignore[method-assign]
        with _db as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ReceivedPacket (
                received INTEGER NOT NULL,
                agent TEXT NOT NULL,
                ground_station INTEGER NOT NULL,
                frequency INTEGER NOT NULL,
                kind TEXT NULL,
                uplink SMALLINT NOT NULL,
                latitude FLOAT NULL,
                longitude FLOAT NULL,
                receiver TEXT NOT NULL,
                freq_active SMALLINT NULL,
                PRIMARY KEY(received, frequency)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS ReceivedPacketWhen ON ReceivedPacket (received desc);
            """)
            try:
                conn.execute("ALTER TABLE ReceivedPacket ADD COLUMN freq_active SMALLINT NULL")
            except sqlite3.Error:
                logger.info("Not updating ReceivedPacket.")
            horizon_days = settings.db["horizon"]
            if horizon_days > 0:
                horizon_ts = int(horizon_days * 86_400 * TS_FACTOR)
                conn.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS ReceivedPacketPrune AFTER INSERT on ReceivedPacket
                    BEGIN
                        DELETE FROM ReceivedPacket
                        WHERE received < NEW.received - {horizon_ts};
                    END;
                """)  # nosec
            else:
                try:
                    conn.execute("DROP TRIGGER IF EXISTS ReceivedPacketPrune")
                except Exception as err:
                    logger.info(f"ignoring error on trigger removal {err}")
            conn.execute("REINDEX ReceivedPacket;")

    def as_local(self) -> data.ReceivedPacket:
        d = self.to_dict()  # untracked_dict()
        d["when"] = to_datetime(self.received)
        return data.ReceivedPacket(**d)

    def _add(self) -> bool:
        sql = "REPLACE INTO ReceivedPacket "
        sql += "(received, agent, ground_station, frequency, kind, uplink, latitude, longitude, receiver, freq_active) "
        sql += "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
        with db() as conn:
            with db_lock:
                data = (
                    self.received,
                    self.agent,
                    self.ground_station,
                    self.frequency,
                    self.kind,
                    self.uplink,
                    self.latitude,
                    self.longitude,
                    self.receiver,
                    self.freq_active,
                )
                conn.execute(sql, data)
        return False

    @classmethod
    def _since(cls, when: int) -> Iterable[ReceivedPacket]:
        with db() as conn:
            conn.row_factory = cls.factory
            yield from conn.execute("SELECT * FROM ReceivedPacket WHERE received >= ? ORDER BY received", [when])

    @classmethod
    def _count(cls, when: int) -> int | None:
        with db() as conn:
            conn.row_factory = None
            for row in conn.execute(
                "SELECT COUNT(*) FROM ReceivedPacket WHERE received >= ? ORDER BY received", [when]
            ):
                return int(row[0])
            return None

    @classmethod
    async def count(cls, when: datetime.timedelta) -> int | None:
        horizon_days = settings.db["horizon"]
        if horizon_days >= 0 and datetime.timedelta(days=horizon_days) < when:
            return None
        since = util.now() - when
        _count: int = await util.in_db_thread(cls._count, to_timestamp(since))
        return _count

    @classmethod
    def _daily_counts(cls, when: int, cutoff: int, limit: int) -> Sequence[int]:
        query = """
            select FLOOR((? - received) / 86400000000), count(*)
            from ReceivedPacket
            where received BETWEEN ? AND ?
            group by 1
            order by 1
            LIMIT ?;
        """
        result = [0] * limit
        with db() as conn:
            conn.row_factory = None
            for row in conn.execute(query, [when, cutoff, when, limit]):
                result[row[0]] = row[1]
        return result

    @classmethod
    async def daily_counts(cls, limit: int) -> Sequence[int]:
        horizon_days = settings.db["horizon"]
        limit = min(365 if horizon_days < 0 else horizon_days, limit)
        if limit < 1:
            return []
        when = util.now()
        cutoff = when - datetime.timedelta(days=limit)
        counts: list[int] = await util.in_db_thread(cls._daily_counts, to_timestamp(when), to_timestamp(cutoff), limit)
        return counts

    @classmethod
    def _binned_counters(
        cls, bin_class: Type[BinCounterT], since: int, bin_size: int, *params: str
    ) -> Sequence[tuple[int, BinCounterT]]:

        # only allow certain strings to be used, to protect against hypothetical injections.
        fields = set(dict(inspect.getmembers(cls))["__dataclass_fields__"].keys())
        for param in params:
            if param not in fields and param not in QUERY_FRAGMENTS:
                raise ValueError(f"invalid {cls.__name__} column name '{param}'")

        def factory(cursor: sqlite3.Cursor, row: Sequence) -> tuple[int, BinCounterT]:
            return (int(row[0]), bin_class(*row[1:]))

        factor = TS_FACTOR * bin_size
        columns = [f"floor((? - received) / ?)"] + list(params) + ["count(*)"]
        groups = ",".join(str(i) for i in range(1, len(columns)))

        # This is an assembled query, which might suggest injection possibilities. However...
        # Every element is either constructed in this method, or is a field name in the class or in QUERY_FRAGMENT.
        query = f"SELECT {','.join(columns)} FROM ReceivedPacket WHERE received > ? GROUP BY {groups}"
        result: list[tuple[int, BinCounterT]] = []
        with db() as conn:
            conn.row_factory = factory
            for row in conn.execute(query, [since, factor, since]):
                result.append(row)
        return result


class NetworkUpdater(network.AbstractNetworkUpdater):
    async def add(self, availability: network.StationAvailability) -> bool:
        return await StationAvailability.add(availability)

    def on_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        try:
            super().on_hfdl(packet_info)
        except Exception as err:
            logger.error("HFDL packet processing", exc_info=err)

    def on_community(self, airframes: dict) -> None:
        try:
            super().on_community(airframes)
        except Exception as err:
            logger.error("community update processing", exc_info=err)

    def on_systable(self, station_table: str) -> None:
        super().on_systable(station_table)

    async def active(self, at: Optional[datetime.datetime] = None) -> Sequence[network.StationAvailability]:
        r: Sequence[network.StationAvailability] = await util.in_db_thread(self._active, at)
        return r

    def _active(self, at: Optional[datetime.datetime] = None) -> Sequence[network.StationAvailability]:
        found = []
        sids = network.STATIONS.by_id.keys()
        for sid in sids:
            la = StationAvailability._station_at(sid, at=at)
            if la:
                found.append(la)
        return found

    async def current(self) -> Sequence[network.StationAvailability]:
        r: Sequence[network.StationAvailability] = await util.in_db_thread(self._active)
        return r

    async def current_freqs(self) -> Sequence[int]:
        current = await self.current()
        return list(itertools.chain(*[a.frequencies for a in current]))


class PacketWatcher(data.AbstractPacketWatcher):
    periodic_task: Optional[asyncio.Task] = None

    def on_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        util.schedule(self.add_packet(packet_info))

    async def add_packet(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        packet = await util.in_db_thread(self._add_packet, packet_info)
        # messaging.publish_soon(util.Message('firehose', 'packet', packet))

    def _add_packet(self, packet_info: hfdl.HFDLPacketInfo) -> ReceivedPacket:
        position = packet_info.position or (None, None)
        packet = ReceivedPacket(
            received=to_timestamp(util.now()),
            agent=packet_info.station or "(unknown)",
            ground_station=packet_info.ground_station["id"],
            frequency=packet_info.frequency,
            kind="spdu" if packet_info.is_squitter else "lpdu",
            uplink=packet_info.is_uplink,
            latitude=position[0],
            longitude=position[1],
            receiver=network.receiver_for(packet_info.frequency),
            freq_active=network.STATIONS[packet_info.ground_station["id"]].is_active(packet_info.frequency),
        )
        packet._add()
        return packet

    def _recent_packets(self, since: datetime.datetime) -> Iterable[ReceivedPacket]:
        when = to_timestamp(since)
        yield from ReceivedPacket._since(when)

    def _binned_recent_packets(self, since: datetime.datetime, bin_size: int) -> Iterable[tuple[int, ReceivedPacket]]:
        when_ts = to_timestamp(since)
        for packet in self._recent_packets(since):
            bin_number = int((when_ts - packet.received) // TS_FACTOR // bin_size)
            yield bin_number, packet

    async def recent_packets(self, since: datetime.datetime) -> Sequence[data.ReceivedPacket]:
        packets: Sequence[data.ReceivedPacket] = list(await util.in_db_thread(self._recent_packets, since))
        return packets

    async def packets_by_frequency_station(
        self, bin_size: int, num_bins: int
    ) -> Mapping[tuple[int, int], data.BinGroup]:
        r: Mapping[tuple[int, int], data.BinGroup] = await util.in_db_thread(
            self._packets_by_frequency_station, bin_size, num_bins
        )
        return r

    def _packets_by_frequency_station(self, bin_size: int, num_bins: int) -> Mapping[tuple[int, int], data.BinGroup]:
        _data: dict[tuple[int, int], data.BinGroup] = collections.defaultdict(lambda: data.BinGroup(num_bins))
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        counters = ReceivedPacket._binned_counters(
            FrequencyStationCounter, to_timestamp(when), bin_size, "frequency", "ground_station"
        )
        for bin_number, counter in counters:
            station_id = counter.ground_station or network.STATIONS[counter.frequency].station_id
            group = _data[(counter.frequency, station_id)]
            group.annotate(station_id)
            try:
                group[bin_number] = counter.count
            except IndexError:
                logging.error(f"unknown bin number {bin_number} {num_bins}")
        return _data

    async def packets_by_agent(self, bin_size: int, num_bins: int) -> Mapping[str, data.BinGroup]:
        r: Mapping[str, data.BinGroup] = await util.in_db_thread(self._packets_by_agent, bin_size, num_bins)
        return r

    def _packets_by_agent(self, bin_size: int, num_bins: int) -> Mapping[str, data.BinGroup]:
        _data: dict[str, data.BinGroup] = collections.defaultdict(lambda: data.BinGroup(num_bins))
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        counters = ReceivedPacket._binned_counters(AgentCounter, to_timestamp(when), bin_size, "agent")
        for bin_number, counter in counters:
            group = _data[counter.agent or "unknown"]
            try:
                group[bin_number] = counter.count
            except IndexError:
                logging.error(f"unknown bin number {bin_number} {num_bins}")
        return _data

    async def packets_by_station(self, bin_size: int, num_bins: int) -> Mapping[int, data.BinGroup]:
        r: dict[int, data.BinGroup] = await util.in_db_thread(self._packets_by_station, bin_size, num_bins)
        return r

    def _packets_by_station(self, bin_size: int, num_bins: int) -> Mapping[int, data.BinGroup]:
        _data: dict[int, data.BinGroup] = collections.defaultdict(lambda: data.BinGroup(num_bins))
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        counters = ReceivedPacket._binned_counters(StationCounter, to_timestamp(when), bin_size, "ground_station")
        for bin_number, counter in counters:
            station_id = counter.ground_station  # MAYBE?: or network.STATIONS[packet.frequency].station_id
            group = _data[station_id]
            group.annotate(station_id)
            try:
                group[bin_number] = counter.count
            except IndexError:
                logging.error(f"unknown bin number {bin_number} {num_bins}")
        return _data

    async def packets_by_band(self, bin_size: int, num_bins: int) -> Mapping[int, data.BinGroup]:
        r: Mapping[int, data.BinGroup] = await util.in_db_thread(self._packets_by_band, bin_size, num_bins)
        return r

    def _packets_by_band(self, bin_size: int, num_bins: int) -> Mapping[int, data.BinGroup]:
        _data: dict[int, data.BinGroup] = collections.defaultdict(lambda: data.BinGroup(num_bins))
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        counters = ReceivedPacket._binned_counters(BandCounter, to_timestamp(when), bin_size, "trunc(frequency / 1000)")
        for bin_number, counter in counters:
            group = _data[counter.band]
            try:
                group[bin_number] = counter.count
            except IndexError:
                logging.error(f"unknown bin number {bin_number} {num_bins}")
        return _data

    async def packets_by_receiver(self, bin_size: int, num_bins: int) -> Mapping[str, data.BinGroup]:
        r: Mapping[str, data.BinGroup] = await util.in_db_thread(self._packets_by_receiver, bin_size, num_bins)
        return r

    def _packets_by_receiver(self, bin_size: int, num_bins: int) -> Mapping[str, data.BinGroup]:
        _data: dict[str, data.BinGroup] = collections.defaultdict(lambda: data.BinGroup(num_bins))
        total_seconds = bin_size * num_bins
        when = util.now() - datetime.timedelta(seconds=total_seconds)
        counters = ReceivedPacket._binned_counters(ReceiverCounter, to_timestamp(when), bin_size, "receiver")
        for bin_number, counter in counters:
            group = _data[counter.receiver]
            try:
                group[bin_number] = counter.count
            except IndexError:
                logging.error(f"unknown bin number {bin_number} {num_bins}")
        return _data

    async def count_packets_since(self, since: datetime.timedelta) -> int | None:
        return await ReceivedPacket.count(since)

    async def daily_counts(self, limit: int) -> Sequence[int]:
        return await ReceivedPacket.daily_counts(limit)
