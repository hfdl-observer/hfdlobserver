# hfdl_observer/heatmapui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import collections
import datetime
import functools
import logging

from typing import Any, Callable, Coroutine, Generic, Iterable, Optional, Sequence, TypeVar, Union

import hfdlobserver
import hfdl_observer.heat as heat
import hfdl_observer.hfdl as hfdl
import hfdl_observer.manage as manage
import hfdl_observer.network as network
import hfdl_observer.util as util


STROKES: dict[int, None | str] = collections.defaultdict(lambda: None)
STROKES.update({0: "┇", 5: "¦"})
ROW_HEADER_WIDTH = 17
MAP_REFRESH_PERIOD = 32.0 / 19.0  # every HFDL slot
MAP_REFRESH_DELTA = datetime.timedelta(seconds=MAP_REFRESH_PERIOD)


TableSourceT = TypeVar("TableSourceT", bound="heat.Table")
CellText = tuple[str | None, str | None]
logger = logging.getLogger(__name__)


@functools.cache
def bin_style(amount: int, maximum: int) -> str:
    rgb = util.spectrum_colour(amount, maximum)
    return f"rgb({', '.join(str(x) for x in rgb)})"


@functools.cache
def bin_symbol(amount: int) -> str:
    # there are 13 slots per 32 second frame.
    # Assuming 1 minute bins and 1 packet per slot on average:
    # we should not expect more than 25 packets per minute
    # However, other bin sizes are possible as well so we have as many single character symbols as practical.
    if amount == 0:
        return "·"
    if amount < 10:
        return str(amount)
    if amount < 36:
        return chr(87 + amount)
    if amount < 62:
        return chr(29 + amount)
    return "✽"


class HeatMapConsumer:
    current_width: int

    def update_heatmap(self, heatmap_data: Sequence) -> None:
        raise NotImplementedError()


class AbstractHeatMapFormatter(Generic[TableSourceT]):
    source: TableSourceT
    flexible_width: bool = True
    title: str

    @functools.cached_property
    def max_count(self) -> int:
        if not self.source.bins:
            return 0
        return max(max(c.value if c else 0 for c in row or [heat.Cell(0)]) for row in self.source.bins.values())

    @functools.cached_property
    def column_size(self) -> int:
        if self.flexible_width:
            count = self.max_count
            if count > 0:
                return len(str(count)) + 2
        return 3

    def cells_visible(self, width: int) -> int:
        return max(0, (width // self.column_size) - 1)

    @property
    def is_empty(self) -> bool:
        return len(self.source.bins) == 0

    def symbol(self, amount: int) -> str:
        if self.flexible_width:
            return str(amount)
        return bin_symbol(amount)

    def style(self, amount: int) -> str:
        return bin_style(amount, max(25, self.max_count))

    def cumulative(self, row: Sequence[heat.Cell], cell_width: int) -> CellText:
        return (f"{sum(cell.value for cell in row): >{cell_width}}", None)

    def column_headers(self, root_str: str, width: int, cells_visible: int) -> list[CellText]:
        column_size = self.column_size
        columns: list[CellText] = [
            (f" 📊 per {root_str}           "[:18], None),
            (f"NOW{' ' * (column_size - 3)}", None),
        ]
        for i in range(1, cells_visible):
            title = STROKES[i % 10] or " "
            columns.append((f"{title: ^{column_size}}", None))
        remainder = width - sum(len(c[0]) for c in columns if c[0] is not None)
        columns.append(((" " * remainder) if remainder > 0 else None, None))
        return columns

    def row_header(self, header: heat.RowHeader, row: Sequence[heat.Cell]) -> CellText:
        raise NotImplementedError()

    def cell(self, index: int, cell: heat.Cell, row_header: heat.RowHeader) -> CellText:
        style = "BASIC_CELL_STYLE"
        stroke = STROKES[index % 10]
        if cell.value:
            style = self.style(cell.value)
            text = self.symbol(cell.value)
        else:
            text = stroke or "·"
        return (f"{text: ^{self.column_size}}", style)

    def row(self, row_id: Union[str, int], row_data: Sequence[heat.Cell], width: int) -> list[CellText]:
        row_header = self.source.row_headers[row_id]
        cells = [self.row_header(row_header, row_data)]
        cells.extend(self.cell(ix, cell, row_header) for ix, cell in enumerate(row_data))
        remainder = width - sum(len(c[0]) for c in cells if c[0] is not None)
        cells.append(self.cumulative(row_data, remainder))
        return cells

    def rows(self) -> Iterable[tuple[Union[int, str], Sequence[heat.Cell]]]:
        return list(row for row in self.source)


class HeatMapByFrequencyFormatter(AbstractHeatMapFormatter[heat.TableByFrequency]):
    title = "by frequency"

    def __init__(
        self,
        bin_size: int,
        num_bins: int,
        flexible_width: bool,
        targetted: Sequence[int],
        untargetted: Sequence[int],
        all_active: bool,
        show_active_line: bool,
        show_confidence: bool,
        show_targetting: bool,
        show_quiet: bool,
    ) -> None:
        self.bin_size = bin_size
        self.flexible_width = flexible_width
        self.show_all_active = all_active
        self.show_active_line = show_active_line
        self.show_confidence = show_confidence
        self.show_targetting = show_targetting
        self.show_quiet = show_quiet
        self.targetted = targetted
        self.untargetted = untargetted
        self.num_bins = num_bins

    async def fetch(self) -> None:
        def rowheader_factory(key: Union[int, str], tags: Sequence[str]) -> heat.RowHeader:
            return heat.RowHeader(str(key), station_id=network.STATIONS[key].station_id, tags=tags)

        self.source = heat.TableByFrequency()
        await self.source.populate(self.bin_size, self.num_bins)

        self.source.tag_rows(self.targetted, ["targetted"], default_factory=rowheader_factory)
        self.source.tag_rows(self.untargetted, ["untargetted"], default_factory=rowheader_factory)

        current_freqs = await network.UPDATER.current_freqs()
        self.source.tag_rows(
            current_freqs, ["active"], default_factory=rowheader_factory if self.show_all_active else None
        )
        await self.source.fill_active_state()

    def row_header(self, header: heat.RowHeader, row: Sequence[heat.Cell]) -> CellText:
        infix = ""
        style = "NORMAL_TEXT"
        if header.station_id:
            infix = network.STATION_ABBREVIATIONS[header.station_id]
        if header.is_tagged("targetted") or any(cell.is_tagged("targetted") for cell in row):
            if any(cell.value for cell in row):
                symbol = "▣"  # ▣🞔□⬚
                style = "PROMINENT_TEXT"
            else:
                symbol = "🞔"
        elif header.is_tagged("active") or any(cell.is_tagged("active") for cell in row):
            symbol = "□"
        else:
            symbol = "⬚"
            infix = infix.lower()
            style = "SUBDUED_TEXT"
        if not self.show_targetting:
            symbol = " "
        stratum = " "
        if self.show_confidence:
            if header.is_tagged("local"):
                stratum = "●"  # ◉
            elif header.is_tagged("network"):
                stratum = "◐"  # ◒⊙⬓
            elif header.is_tagged("guess"):
                stratum = "○"
        return (f"{symbol}{infix: >9}{header.label: >6} {stratum} ", style)

    def row(self, row_id: Union[str, int], row_data: Sequence[heat.Cell], width: int) -> list[CellText]:
        if (
            self.show_quiet
            or (self.show_all_active and self.source.row_headers[row_id].is_tagged("active"))
            or any(cell.value for cell in row_data)
        ):
            return super().row(row_id, row_data, width)
        return []

    def cell(self, index: int, cell: heat.Cell, row_header: heat.RowHeader) -> CellText:
        style = "BASIC_CELL_STYLE"
        stroke = STROKES[index % 10]
        column_size = self.column_size
        if cell.value:
            style = self.style(cell.value)
            text = f"{self.symbol(cell.value): ^{column_size}}"
        elif self.show_active_line and cell.is_tagged("active"):
            if row_header.is_tagged("targetted"):
                line = "─"
            else:
                line = "⠒"
            text = f"{(stroke or line):{line}^{column_size}}"
        else:
            text = f"{(stroke or '·'): ^{column_size}}"
        return (text, style)


class HeatMapByBandFormatter(AbstractHeatMapFormatter[heat.TableByBand]):
    title = "by MHz band"

    def __init__(
        self,
        bin_size: int,
        num_bins: int,
        flexible_width: bool,
        all_bands: bool,
    ) -> None:
        self.bin_size = bin_size
        self.flexible_width = flexible_width
        self.num_bins = num_bins
        self.all_bands = all_bands

    async def fetch(self) -> None:
        self.source = heat.TableByBand()
        await self.source.populate(self.bin_size, self.num_bins)

        def rowheader_factory(key: Union[int, str], tags: Sequence[str]) -> heat.RowHeader:
            return heat.RowHeader(str(key), tags=tags)

        if self.all_bands:
            bands: set[int] = set()
            for allocated in network.STATIONS.assigned().values():
                bands.update(int(f // 1000) for f in allocated)
            self.source.tag_rows(bands, ["band"], default_factory=rowheader_factory)

    def row_header(self, header: heat.RowHeader, row: Sequence[heat.Cell]) -> CellText:
        if any(cell.value for cell in row):
            style = "PROMINENT_TEXT"
        else:
            style = "NORMAL_TEXT"
        return (f" {header.label: >13} MHz ", style)


class HeatMapByStationFormatter(AbstractHeatMapFormatter[heat.TableByStation]):
    title = "by ground station"

    def __init__(
        self,
        bin_size: int,
        num_bins: int,
        flexible_width: bool,
    ) -> None:
        self.bin_size = bin_size
        self.flexible_width = flexible_width
        self.num_bins = num_bins

    async def fetch(self) -> None:
        self.source = heat.TableByStation()
        await self.source.populate(self.bin_size, self.num_bins)

    def row_header(self, header: heat.RowHeader, row: Sequence[heat.Cell]) -> CellText:
        if any(cell.value for cell in row):
            style = "PROMINENT_TEXT"
        else:
            style = "NORMAL_TEXT"
        return (f" {header.label.split(',', 1)[0].strip()[:ROW_HEADER_WIDTH]: >{ROW_HEADER_WIDTH}} ", style)


class HeatMapByAgentFormatter(AbstractHeatMapFormatter[heat.TableByAgent]):
    title = "by agent"

    def __init__(
        self,
        bin_size: int,
        num_bins: int,
        flexible_width: bool,
    ) -> None:
        self.bin_size = bin_size
        self.flexible_width = flexible_width
        self.num_bins = num_bins

    async def fetch(self) -> None:
        self.source = heat.TableByAgent()
        await self.source.populate(self.bin_size, self.num_bins)

    def row_header(self, header: heat.RowHeader, row: Sequence[heat.Cell]) -> CellText:
        if any(cell.value for cell in row):
            style = "PROMINENT_TEXT"
        else:
            style = "NORMAL_TEXT"
        return (f" {header.label: >{ROW_HEADER_WIDTH}} ", style)


class HeatMapByReceiverFormatter(AbstractHeatMapFormatter[heat.TableByReceiver]):
    title = "by receiver"

    def __init__(
        self,
        bin_size: int,
        num_bins: int,
        flexible_width: bool,
        proxies: list[manage.ReceiverProxy],
    ) -> None:
        self.bin_size = bin_size
        self.flexible_width = flexible_width
        self.proxies = proxies
        self.num_bins = num_bins

    async def fetch(self) -> None:
        self.source = heat.TableByReceiver()
        await self.source.populate(self.bin_size, self.num_bins)

    def row_header(self, header: heat.RowHeader, row: Sequence[heat.Cell]) -> CellText:
        if any(cell.value for cell in row):
            style = "PROMINENT_TEXT"
        else:
            style = "NORMAL_TEXT"
        label = header.label[-ROW_HEADER_WIDTH:]
        return (f" {label: >{ROW_HEADER_WIDTH}} ", style)


class HeatMap:
    config: dict
    last_render_time: datetime.datetime = util.now()
    bin_size: int = 60
    data_source: Callable[[int], Coroutine[Any, Any, AbstractHeatMapFormatter[Any]]]
    display: HeatMapConsumer
    refresh_period = 64  # two frames.
    task: Optional[asyncio.Task] = None
    targetted_frequencies: Sequence[int]
    untargetted_frequencies: Sequence[int]
    current_mode: str
    deferred_render_task: None | asyncio.Handle | asyncio.Task = None
    renderer_available: asyncio.Event
    flexible_width: bool = False

    def __init__(self, config: dict) -> None:
        self.config = config
        mode = self.config.get("display_mode", "frequency")
        self.all_modes = {
            "frequency": self.by_frequency,
            "band": self.by_band,
            "station": self.by_station,
            "agent": self.by_agent,
            "receiver": self.by_receiver,
        }
        self.select_display_mode(mode)
        self.set_bin_size(config.get("bin_size", 60))
        self.set_flexible_width(util.tobool(self.config.get("flexible_width", False)))
        self.refresh_period = max(1, min(self.refresh_period, self.bin_size))
        self.targetted_frequencies = []
        self.untargetted_frequencies = []
        self.renderer_available = asyncio.Event()
        self.renderer_available.set()

    def set_bin_size(self, bin_size: int) -> None:
        self.bin_size = min(3600, max(60, int(bin_size)))
        self.maybe_render()

    def set_flexible_width(self, flexible_width: bool) -> None:
        self.flexible_width = flexible_width
        self.maybe_render()

    def select_display_mode(self, mode: str, *_: Any) -> None:
        self.current_mode = mode
        try:
            self.data_source = self.all_modes[mode]
        except KeyError:
            raise ValueError(f"display mode not supported: {mode}") from None
        self.maybe_render()

    def toggle_flexible_width(self) -> None:
        self.set_flexible_width(not self.flexible_width)

    async def by_frequency(self, num_bins: int) -> AbstractHeatMapFormatter:
        source = HeatMapByFrequencyFormatter(
            self.bin_size,
            num_bins,
            self.flexible_width,
            self.targetted_frequencies,
            self.untargetted_frequencies,
            util.tobool(self.config.get("show_all_active", False)),
            util.tobool(self.config.get("show_active_line", True)),
            util.tobool(self.config.get("show_confidence", True)),
            util.tobool(self.config.get("show_targetting", True)),
            util.tobool(self.config.get("show_quiet", True)),
        )
        await source.fetch()
        return source

    async def by_band(self, num_bins: int) -> AbstractHeatMapFormatter:
        source = HeatMapByBandFormatter(
            self.bin_size, num_bins, self.flexible_width, util.tobool(self.config.get("show_all_bands", True))
        )
        await source.fetch()
        return source

    async def by_station(self, num_bins: int) -> AbstractHeatMapFormatter:
        source = HeatMapByStationFormatter(self.bin_size, num_bins, self.flexible_width)
        await source.fetch()
        return source

    async def by_agent(self, num_bins: int) -> AbstractHeatMapFormatter:
        source = HeatMapByAgentFormatter(self.bin_size, num_bins, self.flexible_width)
        await source.fetch()
        return source

    async def by_receiver(self, num_bins: int) -> AbstractHeatMapFormatter:
        source = HeatMapByReceiverFormatter(
            self.bin_size, num_bins, self.flexible_width, list(self.observer.proxies.values())
        )
        await source.fetch()
        return source

    def register(self, observer: hfdlobserver.HFDLObserverController) -> None:
        self.observer = observer
        observer.watch_event("packet", self.on_hfdl)
        observer.watch_event("observing", self.on_observing)

    def on_hfdl(self, packet: hfdl.HFDLPacketInfo) -> None:
        if not self.task:
            self.start()
        self.maybe_render()

    def on_observing(self, observed: tuple[list[int], list[int]]) -> None:
        self.targetted_frequencies, self.untargetted_frequencies = observed
        if not self.task:
            self.start()

    def maybe_render(self) -> None:
        try:
            asyncio.current_task()
        except Exception as err:
            logger.debug(f"not in a task? {err}")
            return
        next_render_time = self.last_render_time + MAP_REFRESH_DELTA
        if self.deferred_render_task is None:
            if next_render_time <= util.now():
                self.deferred_render_task = util.schedule(self.render())
            else:

                async def delayed_render() -> None:
                    await asyncio.sleep(MAP_REFRESH_PERIOD)
                    await self.render()

                self.deferred_render_task = util.schedule(delayed_render())
        else:
            logger.debug(f"render deferred. Next render time {next_render_time}")

    @functools.cached_property
    def reserved_width(self) -> int:
        return (
            1  # left/header padding
            + ROW_HEADER_WIDTH
            + 1  # header-padding
            + 1  # right padding
        )

    async def render(self) -> None:
        # Hypothetically, we could represent each bin/ on each row as a "cell" Rich's table display. This would be
        # slightly less complex on code in this module. Rich is a good framework, but it is generic and does a lot of
        # extra work to figure out the layout when it's already statically known. Instead, this code (and its helpers)
        # combines fragments into the smallest number of Text segments possible, and represents each row on the table
        # with a single Text. Not ideal, but the concept of Cells is retained as it its flexibility
        #
        if not hasattr(self, "display") or util.is_shutting_down():
            self.deferred_render_task = None
            return
        await self.renderer_available.wait()
        self.renderer_available.clear()
        self.deferred_render_task = None
        try:
            await self._render()
        finally:
            self.renderer_available.set()

    async def _render(self) -> None:
        body_width = self.display.current_width - self.reserved_width  # (3 + 9 + 6 + 4 + 1)
        possible_bins = body_width // 3
        if self.bin_size > 60:
            bin_str = f"{self.bin_size}s"
        else:
            bin_str = "minute"
        source = await self.data_source(possible_bins)
        table: list = []
        if not source.is_empty:
            self.last_render_time = util.now()
            cells_visible = source.cells_visible(body_width)

            header_rows = self.render_column_headers(source, cells_visible, bin_str)
            table.extend(header_rows)

            # noqa logger.info(f'{body_width} // {source.cells_visible(body_width)} @ {source.column_size} = {source.cells_visible(body_width) * source.column_size}')
            table.extend(self.render_data_rows(source, cells_visible))

            # for reasons I don't understand, source.source will not get garbage collected. Since we're done with the
            # source, it can be del'd, but it still feels dirty. del'ing `source` alone is insufficient.
            del source.source
        else:
            head = f" 📊 per {bin_str}"
            table.extend(self.render_empty_map(head, self.display.current_width))
        del source
        self.display.update_heatmap(table)

    async def run(self) -> None:
        try:
            while not util.is_shutting_down():
                await self.render()  # maybe_render might be better if performance is impacted.
                await asyncio.sleep(self.refresh_period)
        except Exception as exc:
            logger.error("error while rendering UI", exc_info=exc)
            raise

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if not self.task:
            if loop:
                self.task = loop.create_task(self.run())
            else:
                self.task = util.schedule(self.run())

    def render_empty_map(self, head: str, width: int) -> Sequence:
        raise NotImplementedError

    def render_data_rows(self, source: AbstractHeatMapFormatter, cells_visible: int) -> Sequence[Sequence]:
        raise NotImplementedError

    def render_column_headers(self, source: AbstractHeatMapFormatter, cells_visible: int, bin_str: str) -> Sequence:
        raise NotImplementedError
