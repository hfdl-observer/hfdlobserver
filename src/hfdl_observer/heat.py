# hfdl_observer/heat.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import datetime
import logging
from typing import Any, Callable, Generic, Iterable, Iterator, Mapping, Optional, Sequence, TypeVar

import hfdl_observer.data as data
import hfdl_observer.network as network
import hfdl_observer.util as util

logger = logging.getLogger(__name__)


TableKeyT = TypeVar("TableKeyT")


class Taggable:
    _tags: set[str] | None = None

    def tag(self, tag: str) -> None:
        if self._tags is None:
            self._tags = set()
        self._tags.add(tag)

    def is_tagged(self, tag: str) -> bool:
        return self._tags is not None and tag in self._tags

    def tags_as_str(self) -> str:
        tags = "".join(t[0] for t in self._tags or [])
        if tags:
            return f"[{tags}]"
        return ""


class RowHeader(Taggable):
    label: str = ""
    station_id: Optional[int] = None

    def __init__(self, label: str, station_id: Optional[int] = None, tags: Optional[Sequence[str]] = None) -> None:
        self.label = label
        for tag in tags or []:
            self.tag(tag)
        self.station_id = station_id

    def __str__(self) -> str:
        if self.station_id:
            sid = f"#{self.station_id}:"
        else:
            sid = ""
        return f"{self.tags_as_str()} {sid}{self.label}"


class ColumnHeader:
    index: int
    label: str
    when: datetime.datetime
    size: int
    offset: int

    def __init__(self, index: int, when: datetime.datetime, size: int) -> None:
        self.index = index
        self.when = when
        self.size = size
        self.offset = index * size
        self.label = str(index)

    def __str__(self) -> str:
        if self.offset:
            return f"{self.label}@{self.offset}"
        return "NOW"


class Cell(Taggable):
    value: int

    def __init__(self, value: int, tags: Optional[Sequence[str]] = None) -> None:
        self.value = value

    def __str__(self) -> str:
        return f"{self.value}{self.tags_as_str()}"


class Table(Generic[TableKeyT]):
    column_headers: Sequence[ColumnHeader]
    row_headers: dict[TableKeyT, RowHeader]
    bins: dict[TableKeyT, Sequence[Cell]]

    def _populate(
        self, groups: Mapping[TableKeyT, data.BinGroup], bin_size: int, start: Optional[datetime.datetime] = None
    ) -> None:
        self.bins = {k: [Cell(v) for v in r] for k, r in groups.items()}
        self.row_headers = {}
        for key, values in groups.items():
            if isinstance(key, tuple):
                station_id: int | None = int(key[1])
                label = str(key[0])
            else:
                station_id = int(next(iter(values.annotations))) if values.annotations else None
                label = str(key)
            self.row_headers[key] = RowHeader(label, station_id)
        when = start if start is not None else util.now()
        if groups:
            num_columns = max(len(r) for r in groups.values())
            self.column_headers = [
                ColumnHeader(n, when - datetime.timedelta(seconds=n * bin_size), bin_size) for n in range(num_columns)
            ]
        else:
            self.column_headers = []

    def rows_matching(self, condition: Callable[[TableKeyT, Sequence[Cell]], bool]) -> dict[TableKeyT, Sequence[Cell]]:
        out = {}
        for k, cells in self:
            if callable(condition) and condition(k, cells):
                out[k] = cells
        return out

    def tag_rows(
        self,
        keys: Iterable[TableKeyT],
        tags: Optional[Sequence[str]],
        default_factory: Optional[Callable[[TableKeyT, Sequence[str]], RowHeader]] = None,
    ) -> None:
        for key in keys:
            if key in self.bins:
                for tag in tags or []:
                    self.row_headers[key].tag(tag)
            elif default_factory:
                self.bins[key] = [Cell(0) for col in self.column_headers]
                self.row_headers[key] = default_factory(key, tags or [])

    def key_for_row(self, row_id: TableKeyT) -> Any:
        # default sorts by the row_id itself.
        return row_id

    def __iter__(self) -> Iterator[tuple[TableKeyT, Sequence[Cell]]]:
        try:
            order = sorted(self.bins.keys(), key=self.key_for_row)
        except TypeError:
            logger.error(",".join(str(s) for s in self.bins.keys()))
            raise
        for k in order:
            yield (k, self.bins[k])

    def __str__(self) -> str:
        out = []
        out.append("\t" + "\t".join(str(header) for header in self.column_headers))
        for k, cells in self:
            cells_text = "\t".join(str(cell) for cell in cells)
            out.append("\t".join([str(self.row_headers[k]), cells_text]))
        return "\n".join(out)


class TableByFrequencyStation(Table[tuple[int, int]]):
    async def populate(self, bin_size: int, num_bins: int) -> None:
        packets = await data.PACKET_WATCHER.packets_by_frequency_station(bin_size, num_bins)
        super()._populate(packets, bin_size)

    async def fill_active_state(self) -> None:
        for ix, column in enumerate(self.column_headers):
            when = column.when if column.index else None  # column 0 is "NOW", which triggers different active logic.
            active_by_freq: dict[int, network.StationAvailability] = {}
            active_by_sid: dict[int, network.StationAvailability] = {}
            active = await network.UPDATER.active_for_frame(when)
            for a in active:
                active_by_sid[a.station_id] = a
                for f in a.frequencies:
                    active_by_freq[f] = a
            for key, cells in self.bins.items():
                freq = key[0]
                cell = cells[ix]
                row_header = self.row_headers[key]
                station: network.StationAvailability | None = None
                if row_header.station_id:
                    station = active_by_sid.get(row_header.station_id, None)
                if not station:
                    station = active_by_freq.get(freq, None)
                    if station:
                        row_header.station_id = station.station_id
                    else:
                        row_header.station_id = network.STATIONS[freq].station_id

                if station:
                    if station.frequencies and freq in station.frequencies:
                        cell.tag("active")
                        row_header.tag("active")
                    match station.stratum:
                        case network.Strata.SELF.value:
                            row_header.tag("local")
                        case network.Strata.SQUITTER.value:
                            row_header.tag("network")
                        case None:
                            pass
                        case _:
                            row_header.tag("guess")

    def tag_frequencies(
        self,
        freqs: Sequence[int],
        tags: Optional[Sequence[str]],
        default_factory: Optional[Callable[[tuple[int, int], Sequence[str]], RowHeader]] = None,
    ) -> None:
        rows_to_tag = []
        absent_keys = []
        for freq in freqs:
            found = False
            for bin_key in self.bins:
                if freq == bin_key[0]:
                    rows_to_tag.append(bin_key)
                    found = True
            if not found:
                absent_keys.append((freq, network.STATIONS[freq].station_id))
        self.tag_rows(rows_to_tag + absent_keys, tags, default_factory)


class TableByBand(Table[int]):
    async def populate(self, bin_size: int, num_bins: int) -> None:
        packets = await data.PACKET_WATCHER.packets_by_band(bin_size, num_bins)
        super()._populate(packets, bin_size)


class TableByStation(Table[int]):
    async def populate(self, bin_size: int, num_bins: int) -> None:
        packets = await data.PACKET_WATCHER.packets_by_station(bin_size, num_bins)
        super()._populate(packets, bin_size)
        for key, rh in self.row_headers.items():
            rh.station_id = int(key)
            rh.label = f"#{key}. {network.STATIONS.get(key, 'unknown').station_name}"  # type: ignore[arg-type]

    def key_for_row(self, row_id: int) -> Any:
        # we need to sort by station ID. so, indirect lookup
        return self.row_headers[row_id].station_id or 0


class TableByAgent(Table[str]):
    async def populate(self, bin_size: int, num_bins: int) -> None:
        packets = await data.PACKET_WATCHER.packets_by_agent(bin_size, num_bins)
        super()._populate(packets, bin_size)


class TableByReceiver(Table[str]):
    async def populate(self, bin_size: int, num_bins: int) -> None:
        packets = await data.PACKET_WATCHER.packets_by_receiver(bin_size, num_bins)
        super()._populate(packets, bin_size)
