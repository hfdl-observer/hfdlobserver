# hfdl_observer/network.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import dataclasses
import datetime
import functools
import logging
from enum import Enum
from typing import Any, Optional, Sequence, Union

import hfdl_observer.bus as bus
import hfdl_observer.hfdl as hfdl
import hfdl_observer.util as util

logger = logging.getLogger(__name__)


class Strata(Enum):
    SYSTABLE = 0
    CACHE = 1
    PERFORMANCE = 2
    SQUITTER = 3
    SELF = 4


AVAILABILITY_LIFETIMES = {
    Strata.SYSTABLE.value: datetime.timedelta(seconds=1),
    Strata.CACHE.value: datetime.timedelta(hours=1),
    Strata.PERFORMANCE.value: datetime.timedelta(minutes=30),
    Strata.SQUITTER.value: datetime.timedelta(hours=1),
    Strata.SELF.value: datetime.timedelta(hours=1),
}


STATION_ABBREVIATIONS = {
    1: "SANFRAN",
    2: "MOLOKAI",
    3: "REYKJVK",
    4: "RIVERHD",
    5: "AUCKLAND",
    6: "HATYAI",
    7: "SHANNON",
    8: "JOBURG",
    9: "BARROW",
    10: "MUAN",
    11: "ALBROOK",
    13: "SNTACRUZ",
    14: "KRASNOY",
    15: "MUHARRAQ",
    16: "AGANA",
    17: "CANARIAS",
}


PosixTimestamp = int


@dataclasses.dataclass
class Station:
    station_id: int
    station_name: str
    latitude: float
    longitude: float
    assigned_frequencies: list[int]
    active_frequencies: list[int]
    observed_frequencies: set[int] | None = None

    def __str__(self) -> str:
        return f"<Station #{self.station_id} {self.station_name}>"

    def is_active(self, frequency: int) -> bool:
        return frequency in self.active_frequencies

    def is_assigned(self, frequency: int) -> bool:
        return frequency in self.assigned_frequencies

    def update(self, other: "Station") -> None:
        if self is not other:
            self.station_name = other.station_name or self.station_name
            self.latitude = other.latitude or self.latitude
            self.longitude = other.longitude or self.longitude
            self.assigned_frequencies = other.assigned_frequencies or self.assigned_frequencies
            self.update_active(other.active_frequencies)

    def update_active(self, frequencies: list[int]) -> None:
        if frequencies:
            self.active_frequencies = frequencies


@dataclasses.dataclass
class StationAvailability:
    station_id: int
    stratum: int
    when: datetime.datetime
    frequencies: list[int]
    agent: str
    valid_at_frame: int
    from_station: Optional[int] = None
    valid_to_frame: Optional[int] = None

    def as_station(self) -> Station:
        base = STATIONS[self.station_id]
        return Station(
            station_id=base.station_id,
            station_name=base.station_name,
            latitude=base.latitude,
            longitude=base.longitude,
            assigned_frequencies=base.assigned_frequencies,
            active_frequencies=self.frequencies,
        )


# protocol, really.
class AbstractNetworkUpdater(bus.EventNotifier):
    async def current(self) -> Sequence[StationAvailability]:
        raise NotImplementedError()

    async def current_freqs(self) -> Sequence[int]:
        raise NotImplementedError()

    async def active(self, at: Optional[datetime.datetime] = None) -> Sequence[StationAvailability]:
        raise NotImplementedError()

    # lru_cache does not work with async functions, so instead of using coroutines, we return a Task which is a Future
    # and can be reawaited... and therefore cached. There are some corner cases but none of those should appear here.
    @functools.lru_cache(maxsize=128)
    def _active_ts(self, timestamp: int) -> asyncio.Task[Sequence[StationAvailability]]:
        return util.schedule(self.active(util.timestamp_to_datetime(timestamp)))

    async def active_for_frame(self, at: Optional[datetime.datetime] = None) -> Sequence[StationAvailability]:
        # current/now/None should never be cached.
        if at is None:
            return await self.active(util.now())
        # each ground station has its own frame period. We construct a similar one for this app, but it's not official.
        return await self._active_ts(util.pseudoframe_timestamp(at))

    async def add(self, availability: StationAvailability) -> bool:
        raise NotImplementedError()

    async def updated(self, availabilities: Optional[Sequence[StationAvailability]] = None) -> None:
        if availabilities is None:
            STATIONS.refresh()
        else:
            STATIONS.update_active(await self.current())
            self.notify_event("availability", util.now())

    def on_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        util.schedule(self.aon_hfdl(packet_info))

    async def aon_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        valid_at = util.timestamp_to_datetime(packet_info.timestamp)
        from_station = packet_info.ground_station["id"]
        squitter = packet_info.get("spdu.gs_status", default=[])
        performance = packet_info.get("lpdu.hfnpdu.freq_data", default=[])
        if squitter:
            kind = "squitter"
            packet_stratum = Strata.SQUITTER  # or SELF_SQUITTER?
            freqs_key = "freqs"
        elif performance:
            kind = "performance"
            packet_stratum = Strata.PERFORMANCE
            freqs_key = "heard_on_freqs"
        else:
            if packet_info.ground_station:
                STATIONS.add_observed(from_station, packet_info.frequency)
            return None
        agent = packet_info.station or kind
        updates = 0
        for gs in squitter or performance or []:
            stn_id = gs["gs"]["id"]
            sid = int(stn_id) if stn_id else None
            if not sid or sid < 0:
                continue
            stratum = packet_stratum
            if squitter and sid == from_station:
                stratum = Strata.SELF
            valid_to = valid_at + AVAILABILITY_LIFETIMES[stratum.value]
            frequencies = sorted((int(sf["freq"]) for sf in gs[freqs_key] if "freq" in sf))

            added = await self.add(
                StationAvailability(
                    station_id=sid,
                    stratum=stratum.value,
                    frequencies=frequencies,
                    agent=agent,
                    from_station=from_station,
                    valid_at_frame=util.pseudoframe(valid_at),
                    valid_to_frame=util.pseudoframe(valid_to) if valid_to is not None else None,
                    when=valid_at,
                )
            )
            if added:
                updates += 1
        if updates:
            self.notify_event("event", (kind, agent, packet_info.timestamp))
            await self.updated(await self.current())

    def on_community(self, airframes: dict) -> None:
        util.schedule(self.aon_community(airframes))

    async def aon_community(self, airframes: dict) -> None:
        updates = 0
        for gs in airframes.get("ground_stations", []):
            try:
                sid = gs["id"]
            except KeyError:
                logger.warning(f"{self} ignoring spurious station `{gs}`")
                continue
            if not sid:
                continue

            last_updated = gs["last_updated"]
            if last_updated < 0:
                last_updated += util.now().timestamp()
            try:
                stratum = int(gs.get("stratum", Strata.CACHE.value))
            except ValueError:
                stratum = Strata.CACHE.value

            valid_at = util.timestamp_to_datetime(last_updated)
            valid_to = valid_at + AVAILABILITY_LIFETIMES[stratum]
            frequencies = sorted(gs["frequencies"].get("active", []))
            from_station = gs.get("update_source", None)

            added = await self.add(
                StationAvailability(
                    station_id=sid,
                    stratum=stratum,
                    frequencies=frequencies,
                    agent="community",
                    from_station=None if isinstance(from_station, str) else from_station,
                    valid_at_frame=util.pseudoframe(valid_at),
                    valid_to_frame=util.pseudoframe(valid_to) if valid_to is not None else None,
                    when=valid_at,
                )
            )
            if added:
                updates += 1
        if updates:
            await self.updated(await self.current())

    def on_systable(self, station_table: str) -> None:
        util.schedule(self.aon_systable(station_table))

    async def aon_systable(self, station_table: str) -> None:
        data = util.deserialise_station_table(station_table)

        stratum = Strata.SYSTABLE.value
        valid_at = util.now()
        valid_to = valid_at + AVAILABILITY_LIFETIMES[stratum]
        hfdl_stations: dict[int, Station] = {}
        for gs in data["stations"]:
            sid = gs.get("id", None)
            if not sid or sid < 0:
                continue
            frequencies = sorted(int(f) for f in gs["frequencies"])

            added = await self.add(
                StationAvailability(
                    station_id=sid,
                    stratum=stratum,
                    frequencies=[],
                    agent="systable",
                    from_station=None,
                    valid_at_frame=util.pseudoframe(valid_at),
                    valid_to_frame=util.pseudoframe(valid_to) if valid_to is not None else None,
                    when=valid_at,
                )
            )
            if added:
                hfdl_stations[sid] = Station(
                    station_id=sid,
                    station_name=gs["name"],
                    latitude=gs["lat"],
                    longitude=gs["lon"],
                    assigned_frequencies=frequencies,
                    active_frequencies=[],
                )
        if hfdl_stations:
            STATIONS.update(hfdl_stations)
            await self.updated()

    def prune(self, _: Any = None) -> None:
        pass


class CumulativePacketStats(bus.EventNotifier):
    packets: int = 0
    from_air: int = 0
    from_ground: int = 0
    with_position: int = 0
    no_position: int = 0
    squitters: int = 0

    def on_hfdl(self, packet: hfdl.HFDLPacketInfo) -> None:
        self.packets += 1
        if packet.is_downlink:
            self.from_air += 1
        if packet.is_uplink:
            self.from_ground += 1
        if packet.is_squitter:
            self.squitters += 1
        if packet.position:
            self.with_position += 1
        else:
            self.no_position += 1
        self.notify_event("update", self)


class StationLookup:
    by_id: dict[int, Station]
    by_freq: dict[int, Station]

    def __init__(self, initial: Optional[dict[int, Station]] = None) -> None:
        if initial:
            self.update(initial)

    def update(self, systable: dict[int, Station]) -> None:
        if hasattr(self, "by_id"):
            for sid, station in systable.items():
                current = self.by_id.setdefault(sid, station)
                current.update(station)
        else:
            self.by_id = systable
        self.refresh()

    def update_active(self, availabilities: Sequence[StationAvailability]) -> None:
        if hasattr(self, "by_id"):
            for availability in availabilities:
                self[availability.station_id].update_active(availability.frequencies)
            self.refresh()

    def add_observed(self, sid: int, frequency: int) -> None:
        station = self.by_id[sid]
        station.observed_frequencies = station.observed_frequencies or set()
        if frequency not in station.observed_frequencies:
            station.observed_frequencies.add(frequency)
            self.refresh()

    def refresh(self) -> None:
        self.by_freq = {}
        for station in self.by_id.values():
            for freq in station.active_frequencies:
                self.by_freq[freq] = station
            for freq in station.observed_frequencies or []:
                self.by_freq.setdefault(freq, station)
            for freq in station.assigned_frequencies:
                self.by_freq.setdefault(freq, station)

    def __getitem__(self, key: Union[str, int, float]) -> Station:
        return self.get(key)

    def get(self, key: Union[str, int, float], default: Optional[Station] = None) -> Station:
        try:
            k = int(key)
            if k < 2000:
                return self.by_id[k]
            return self.by_freq[k]
        except KeyError:
            # return default
            raise
        except AttributeError as err:
            # return default
            logger.error(f"ID   {self.by_id}")
            logger.error(f"FREQ {self.by_freq}")
            raise KeyError(f"Mapping error for {key}; no mappings yet?") from err

    def assigned(self) -> dict[int, list[int]]:
        result = {}
        for station in self.by_id.values():
            result[station.station_id] = station.assigned_frequencies
        return result

    def active(self) -> dict[int, list[int]]:
        result = {}
        for station in self.by_id.values():
            result[station.station_id] = station.active_frequencies
        return result

    def is_active(self, frequency: int) -> bool:
        return self[frequency].is_active(frequency)


STATIONS = StationLookup()
UPDATER = AbstractNetworkUpdater()  # Must be overridden on initialization of the main task
RECEIVER_FREQUENCIES: dict[int, str] = {}


def receiver_for(frequency: int) -> str:
    return RECEIVER_FREQUENCIES.get(frequency, f"{frequency}?")


def set_receiver_for_frequency(frequency: int, receiver: str) -> None:
    RECEIVER_FREQUENCIES[frequency] = receiver
