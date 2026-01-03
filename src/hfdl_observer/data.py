# hfdl_observer/data.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
import collections
import dataclasses
import datetime
from typing import Any, Mapping, Optional

import hfdl_observer.hfdl as hfdl

DEFAULT_RECEIVER_WEIGHT = 0


class ListenerConfig:
    proto: str = "udp"
    address: str = "127.0.0.1"
    port: int = 5542

    def __init__(self, data: dict | None = None) -> None:
        if data is not None:
            self.proto = data["protocol"]
            self.address = data["address"]
            self.port = int(data["port"])


class ObservingChannel:
    _frequencies: set[int]
    allowed_width_hz: int

    def __init__(self, allowed_width_hz: int, frequencies: Optional[list[int]] = None):
        self.allowed_width_hz = int(allowed_width_hz)
        self._frequencies = set(frequencies) if frequencies else set()

    @property
    def min_khz(self) -> int:
        return min(self._frequencies)

    @property
    def min_hz(self) -> int:
        return int(self.min_khz * 1000)

    @property
    def max_khz(self) -> int:
        return max(self._frequencies)

    @property
    def max_hz(self) -> int:
        return int(self.max_khz * 1000)

    @property
    def center_khz(self) -> float:
        return (self.min_khz + self.max_khz) / 2.0

    @property
    def width_hz(self) -> int:
        if self.frequencies:
            channel_width = hfdl.HFDL_CHANNEL_WIDTH
            return self.max_hz - self.min_hz + channel_width
        else:
            return 0

    @property
    def frequencies(self) -> list[int]:
        return sorted(self._frequencies)

    def maybe_add(self, frequency: int) -> bool:
        peephole_width = self.allowed_width_hz - hfdl.HFDL_CHANNEL_WIDTH
        freq_hz = frequency * 1000
        if not self._frequencies or (
            abs(freq_hz - self.min_hz) <= peephole_width and abs(freq_hz - self.max_hz) <= peephole_width
        ):
            self._frequencies.add(frequency)
            return True
        return False

    def maybe_add_all(self, frequencies: list[int]) -> bool:
        # all or nothing.
        peephole_width = self.allowed_width_hz - hfdl.HFDL_CHANNEL_WIDTH
        min_freq_hz = min(frequencies) * 1000
        max_freq_hz = max(frequencies) * 1000
        if not self._frequencies or (
            abs(min_freq_hz - self.min_hz) <= peephole_width
            and abs(min_freq_hz - self.max_hz) <= peephole_width
            and abs(max_freq_hz - self.min_hz) <= peephole_width
            and abs(max_freq_hz - self.max_hz) <= peephole_width
        ):
            self._frequencies |= set(frequencies)
            return True
        return False

    def clone(self) -> "ObservingChannel":
        return ObservingChannel(self.allowed_width_hz, self.frequencies)

    def matches(self, other: "ObservingChannel") -> bool:
        return other is not None and self._frequencies == other._frequencies

    def __str__(self) -> str:
        return f"{self.frequencies}"

    def __repr__(self) -> str:
        return f"ObservingChannel({self.allowed_width_hz}, {self.frequencies})"


class ChannelObserver:
    def observable_widths(self) -> list[int]:
        raise NotImplementedError(str(self.__class__))

    @classmethod
    def required_width(cls, frequencies: list[int]) -> int:
        if not frequencies:
            return 0
        max_hz = max(frequencies) * 1000
        min_hz = min(frequencies) * 1000
        needed = max(hfdl.HFDL_CHANNEL_WIDTH, max_hz - min_hz)
        return needed

    def width_for(self, frequencies: list[int]) -> int:
        needed = self.required_width(frequencies)
        widths = sorted(self.observable_widths(), reverse=True)
        for available_width in widths:
            if needed <= available_width:
                return available_width
        raise ValueError(f"cannot accomodate {frequencies} in channel widths {widths}")

    def observing_channel_for(self, frequencies: list[int]) -> ObservingChannel:
        return ObservingChannel(self.width_for(frequencies), frequencies)


@dataclasses.dataclass
class ReceivedPacket:
    when: datetime.datetime
    agent: str
    ground_station: int
    frequency: int
    uplink: bool
    latitude: Optional[float]
    longitude: Optional[float]
    receiver: str
    kind: Optional[str] = None


@dataclasses.dataclass
class FrequencyWatch:
    sequence: int
    started: datetime.datetime
    station_id: int
    frequency: int
    is_targetted: bool
    ended: Optional[datetime.datetime] = None
    observer_id: Optional[str] = None


class BinGroup(list):
    annotations: set[int | str]

    def __init__(self, num_bins: int) -> None:
        self.annotations = set()
        super().__init__([0] * num_bins)

    def annotate(self, annotation: int | str) -> None:
        self.annotations.add(annotation)


BinnedPacketDataType = Mapping[int | str, BinGroup]


class AbstractPacketWatcher:
    def on_hfdl(self, packet_info: hfdl.HFDLPacketInfo) -> None:
        raise NotImplementedError(str(self.__class__))

    async def packets_by_frequency(self, bin_size: int, num_bins: int) -> BinnedPacketDataType:
        raise NotImplementedError(str(self.__class__))

    async def packets_by_agent(self, bin_size: int, num_bins: int) -> BinnedPacketDataType:
        raise NotImplementedError(str(self.__class__))

    async def packets_by_station(self, bin_size: int, num_bins: int) -> BinnedPacketDataType:
        raise NotImplementedError(str(self.__class__))

    async def packets_by_band(self, bin_size: int, num_bins: int) -> BinnedPacketDataType:
        raise NotImplementedError(str(self.__class__))

    async def packets_by_frequency_set(
        self, bin_size: int, num_bins: int, frequency_sets: dict[int, str]
    ) -> BinnedPacketDataType:
        raise NotImplementedError(str(self.__class__))

    async def packets_by_receiver(self, bin_size: int, num_bins: int) -> BinnedPacketDataType:
        raise NotImplementedError(str(self.__class__))

    async def count_packets_since(self, since: datetime.timedelta) -> int | None:
        raise NotImplementedError(str(self.__class__))


PACKET_WATCHER = AbstractPacketWatcher()  # Must be overridden during app initialization


class AgentStats:
    events: dict[str, dict[str, Any]]

    def __init__(self, horizon: int = 3600):
        self.events = collections.defaultdict(dict)
        self.horizon = horizon

    def add_event(self, event: str, agent: str, timestamp: int) -> None:
        self.events[event].setdefault(agent, []).append(timestamp)

    def counts(self, event: str) -> dict[str, int]:
        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - self.horizon
        results = {}
        agent: str
        hits: list[int]
        for agent, hits in self.events[event].items():
            results[agent] = sum(1 for hit in hits if hit >= cutoff)
        return results

    def prune(self, epoch: Optional[int] = None) -> None:
        if epoch is None:
            epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        cutoff = epoch - self.horizon
        for event in self.events.values():
            for agent in event:
                current = event[agent]
                pruned = [hit for hit in current if hit >= cutoff]
                event[agent] = pruned
