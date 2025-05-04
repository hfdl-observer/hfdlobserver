# cui.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
# flake8: noqa [W503]

import asyncio
import collections
import functools
import datetime
import logging

from typing import Any, Generic, Callable, Coroutine, Iterable, Optional, Sequence, TypeVar, Union

import rich.console
import rich.highlighter
import rich.layout
import rich.live
import rich.logging
import rich.markdown
import rich.segment
import rich.style
import rich.table
import rich.text

import hfdl_observer.bus as bus
import hfdl_observer.heat as heat
import hfdl_observer.hfdl as hfdl
import hfdl_observer.manage as manage
import hfdl_observer.network as network
import hfdl_observer.settings as settings
import hfdl_observer.util as util

import hfdlobserver

logger = logging.getLogger(__name__)
start = datetime.datetime.now()
SCREEN_REFRESH_RATE = 2
MAP_REFRESH_PERIOD = 32.0 / 19.0  # every HFDL slot
MAP_REFRESH_DELTA = datetime.timedelta(seconds=MAP_REFRESH_PERIOD)

PANE_BAR = rich.style.Style.parse('bright_white on bright_black')
PANE_BAR_REVERSED = rich.style.Style.parse('bright_black on bright_white')
SUBDUED_TEXT = rich.style.Style.parse('grey50 on black')
NORMAL_TEXT = rich.style.Style.parse('white on black')
PROMINENT_TEXT = rich.style.Style.parse('bright_white on black')
BASIC_CELL_STYLE = rich.style.Style.parse('bright_black on black')
BOLD = rich.style.Style.parse('bold')
ON_DARK_GREEN = rich.style.Style.parse('on dark_green')
FORECAST_STYLEMAP = {
    "extreme": rich.style.Style.parse("yellow1 on dark_red"),
    "severe": rich.style.Style.parse("black on red1"),
    "strong": rich.style.Style.parse("black on dark_orange"),
    "moderate": rich.style.Style.parse("black on orange1"),
    "minor": rich.style.Style.parse("black on gold1"),
    "none": rich.style.Style.parse("white on bright_black"),
    None: rich.style.Style.parse("white on bright_black"),
}
ROW_HEADER_WIDTH = 17


CellText = tuple[str | None, str | rich.style.Style | None]


class ObserverDisplay:
    status: Optional[rich.table.Table] = None
    totals: Optional[rich.table.Table] = None
    counts: Optional[rich.table.Table] = None
    tty_bar: Optional[rich.table.Table] = None
    tty: Optional[rich.table.Table] = None
    forecast: rich.text.Text
    uptime_text: rich.text.Text
    totals_text: rich.text.Text
    garbage: collections.deque[rich.table.Table]
    keyboard: util.Keyboard

    def __init__(
        self,
        console: rich.console.Console,
        heatmap: 'HeatMap',
        keyboard: util.Keyboard,
        cumulative_line: 'CumulativeLine',
        forecaster: bus.RemoteURLRefresher,
    ) -> None:
        self.garbage = collections.deque()
        self.console = console
        self.heatmap = heatmap
        self.cumulative_line = cumulative_line
        self.root = rich.layout.Layout("HFDL Observer")
        self.heatmap.display = self
        self.cumulative_line.display = self
        self.uptime_text = rich.text.Text("STARTING")
        self.forecast = rich.text.Text('(space weather unavailable)')
        self.setup_status()
        self.totals_text = rich.text.Text('', style=NORMAL_TEXT)
        self.setup_totals()
        self.update_status()
        self.update_tty_bar()
        self.keyboard = self.setup_keyboard(keyboard)
        forecaster.watch_event('response', self.on_forecast)

    def update(self) -> None:
        t = rich.table.Table.grid(expand=True, pad_edge=False, padding=(0, 0))
        if self.status:
            t.add_row(self.status)
        if self.totals:
            t.add_row(self.totals)
        if self.counts:
            t.add_row(self.counts)
        if self.tty:
            if self.tty_bar:
                t.add_row(self.tty_bar)
            t.add_row(self.tty)
        if t.row_count:
            self.root.update(t)

    def setup_status(self) -> None:
        if self.status:
            self.garbage.append(self.status)
        table = rich.table.Table.grid(expand=True)
        table.add_column()
        table.add_column(justify="center")
        table.add_column(justify="right")
        text = rich.text.Text()
        text.append(' 📡 ')
        text.append('HFDL Observer', style=BOLD)
        table.add_row(text, self.forecast, self.uptime_text, style=ON_DARK_GREEN)
        self.status = table

    def setup_totals(self) -> None:
        if self.totals:
            self.garbage.append(self.totals)
        table = rich.table.Table.grid(expand=True)
        table.add_column()  # title
        table.add_column(justify='right')   # Grand Total
        table.add_row(
            rich.text.Text(" Totals (since start)", style='bold bright_white'),
            self.totals_text,
            style='white on black'
        )
        self.totals = table

    def keyboard_help(self) -> str:
        parts = [
            'Keyboard Commands',
            '[,] - previous display mode',
            '[.] - next display mode',
            '[-] - bin size -60s',
            '[+] - bin size +60s',
        ]
        for k, m in enumerate(self.heatmap.all_modes.keys()):
            parts.append(f'[{k+1}] - {m} display')
        parts.append('[h] - this help')
        return '\n'.join(parts)

    def setup_keyboard(self, keyboard: util.Keyboard) -> util.Keyboard:
        keyboard.add_mapping('.', self.next_heatmap_mode)
        keyboard.add_mapping(',', self.previous_heatmap_mode)
        keyboard.add_mapping('-', self.smaller_bins)
        keyboard.add_mapping('+', self.larger_bins)
        keyboard.add_mapping('_', self.smaller_bins)
        keyboard.add_mapping('=', self.larger_bins)
        keyboard.add_mapping('q', exit)
        keyboard.add_mapping('Q', exit)
        keyboard.add_mapping('h', lambda _: logger.info(self.keyboard_help()))
        keyboard.add_mapping('H', lambda _: logger.info(self.keyboard_help()))
        keyboard.add_mapping('w', self.toggle_flexible_width)
        keyboard.add_mapping('W', self.toggle_flexible_width)
        for k, m in enumerate(self.heatmap.all_modes.keys()):
            keyboard.add_mapping(str(k+1), functools.partial(self.heatmap.select_display_mode, m))
        return keyboard

    def update_status(self) -> None:
        if not hasattr(self, 'uptime_text'):
            return
        uptime = datetime.datetime.now() - start
        uptime -= datetime.timedelta(0, 0, uptime.microseconds)
        self.uptime_text.plain = f'UP {uptime}'

    def update_tty_bar(self) -> None:
        if self.tty_bar:
            self.garbage.append(self.tty_bar)
        table = rich.table.Table.grid(expand=True)
        table.add_row(' 📰 Log', style=PANE_BAR)
        self.tty_bar = table

    def update_totals(self, cumulative: network.CumulativePacketStats) -> None:
        actives = str(self.cumulative_line.active) if self.cumulative_line.active is not None else '?'
        targets = str(self.cumulative_line.target_observed) if self.cumulative_line.target_observed is not None else '?'
        untargets = f' +{self.cumulative_line.bonus_observed}' if self.cumulative_line.bonus_observed else ''
        self.totals_text.plain = (
            f"⏬{cumulative.from_air} ⏫{cumulative.from_ground}  "
            f"|  🌐{cumulative.with_position} ❔{cumulative.no_position}  "
            f"|  📰{cumulative.squitters}  "
            f"|  🔎{targets}/{actives}{untargets}  "
            f"|  📶{cumulative.packets}  "
        )

    def update_log(self, ring: collections.deque) -> None:
        # WARNING: do not use any logger from within this method.
        if self.tty:
            self.garbage.append(self.tty)

        available_space = (
            self.current_height
            - (self.counts.row_count if self.counts else 0)
            - (self.status.row_count if self.status else 0)
            - (self.tty_bar.row_count if self.tty_bar else 0)
            - (self.totals.row_count if self.totals else 0)
            # - 1  # trailing blank
        )

        if available_space > 0:
            table = rich.table.Table.grid(expand=True)
            entries = list(ring)[-available_space:]
            for row in entries:
                table.add_row(row)
            self.tty = table
        else:
            self.tty = None

    def update_counts(self, table: rich.table.Table) -> None:
        if table.row_count:
            if self.counts is not None:
                self.garbage.append(self.counts)
            self.counts = table

    def on_forecast(self, forecast: Any) -> None:
        try:
            recent = forecast['-1']
            current = forecast['0']
            forecast1d = forecast['1']
            text = self.forecast
            text.plain = ''
            text.append(f'R{recent["R"]["Scale"] or "-"}', style=FORECAST_STYLEMAP[recent["R"]["Text"]])
            text.append('|')
            text.append(f'S{recent["S"]["Scale"] or "-"}', style=FORECAST_STYLEMAP[recent["S"]["Text"]])
            text.append('|')
            text.append(f'G{recent["G"]["Scale"] or "-"}', style=FORECAST_STYLEMAP[recent["G"]["Text"]])
            text.append('  ')
            text.append(f'R{current["R"]["Scale"] or "-"}', style=FORECAST_STYLEMAP[current["R"]["Text"]])
            text.append('|')
            text.append(f'S{current["S"]["Scale"] or "-"}', style=FORECAST_STYLEMAP[current["S"]["Text"]])
            text.append('|')
            text.append(f'G{current["G"]["Scale"] or "-"}', style=FORECAST_STYLEMAP[current["G"]["Text"]])
            text.append('  ')
            text.append(f'R{forecast1d["R"]["MinorProb"]}/{forecast1d["R"]["MajorProb"]}', FORECAST_STYLEMAP["none"]),
            text.append('|')
            text.append(f'S{forecast1d["S"]["Prob"]}', FORECAST_STYLEMAP["none"]),
            text.append('|')
            text.append(f'G{forecast1d["G"]["Scale"] or "-"}', FORECAST_STYLEMAP[forecast1d["G"]["Text"]]),
        except Exception as err:
            logger.debug('ignoring forecaster error', exc_info=err)

    @property
    def current_width(self) -> int:
        return self.console.options.size.width

    @property
    def current_height(self) -> int:
        return self.console.options.size.height or 25

    def clear_table(self, table: Optional[rich.table.Table]) -> None:
        # dubious, attempt to voodoo patch a possible memory leak in Rich
        with self.root._lock:
            if table is not None:
                if hasattr(table.rows, 'clear'):
                    table.rows.clear()
                if hasattr(table.columns, 'clear'):
                    table.columns.clear()

    def on_render(self) -> None:
        while len(self.garbage) > 0:
            try:
                garbage = self.garbage.popleft()
            except IndexError:
                break
            self.clear_table(garbage)

    def next_heatmap_mode(self, *_: Any) -> None:
        modes = list(self.heatmap.all_modes.keys())
        ix = modes.index(self.heatmap.current_mode)
        ix = (ix + 1) % len(modes)
        new_mode = modes[ix]
        self.heatmap.select_display_mode(new_mode)

    def previous_heatmap_mode(self, *_: Any) -> None:
        modes = list(self.heatmap.all_modes.keys())
        ix = modes.index(self.heatmap.current_mode)
        ix = (ix - 1 + len(modes)) % len(modes)
        new_mode = modes[ix]
        self.heatmap.select_display_mode(new_mode)

    def larger_bins(self, *_: Any) -> None:
        bin_size = self.heatmap.bin_size
        self.heatmap.set_bin_size(bin_size + 60)

    def smaller_bins(self, *_: Any) -> None:
        bin_size = self.heatmap.bin_size
        self.heatmap.set_bin_size(bin_size - 60)

    def toggle_flexible_width(self, *_: Any) -> None:
        self.heatmap.toggle_flexible_width()


class CumulativeLine:
    display: ObserverDisplay
    target_observed: Optional[int] = None
    bonus_observed: Optional[int] = None
    active: Optional[int] = None

    def register(self, observer: hfdlobserver.HFDLObserverController, totals: network.CumulativePacketStats) -> None:
        self.cumulative = totals
        totals.watch_event('update', self.on_update)
        observer.watch_event('active', self.on_active)
        observer.watch_event('observing', self.on_observing)

    def on_update(self, _: Any) -> None:
        if self.display:
            self.display.update_totals(self.cumulative)

    def on_observing(self, observed: tuple[Sequence[int], Sequence[int]]) -> None:
        targetted, untargetted = observed
        self.target_observed = len(targetted)
        self.bonus_observed = len(untargetted)

    def on_active(self, active_frequencies: Sequence[int]) -> None:
        self.active = len(active_frequencies)


STROKES: dict[int, None | str] = collections.defaultdict(lambda: None)
STROKES.update({0: '┇', 5: '¦'})


TableSourceT = TypeVar('TableSourceT', bound='heat.Table')


@functools.cache
def bin_symbol(amount: int) -> str:
    # there are 13 slots per 32 second frame.
    # Assuming 1 minute bins and 1 packet per slot on average:
    # we should not expect more than 25 packets per minute
    # However, other bin sizes are possible as well so we have as many single character symbols as practical.
    if amount == 0:
        return '·'
    if amount < 10:
        return str(amount)
    if amount < 36:
        return chr(87 + amount)
    if amount < 62:
        return chr(29 + amount)
    return '✽'


@functools.cache
def bin_style(amount: int, maximum: int) -> rich.style.Style:
    rgb = util.spectrum_colour(amount, maximum)
    return rich.style.Style(bgcolor=f'rgb({",".join(str(i) for i in rgb)})', color='black')


class AbstractHeatMapFormatter(Generic[TableSourceT]):
    source: TableSourceT
    strokes = STROKES
    flexible_width: bool = True
    title: str

    @functools.cached_property
    def max_count(self) -> int:
        if not self.source.bins:
            return 0
        return max(
            max(c.value if c else 0 for c in row or [0]) for row in self.source.bins.values()  # type: ignore
        )

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

    def style(self, amount: int) -> rich.style.Style:
        return bin_style(amount, max(25, self.max_count))

    def cumulative(self, row: Sequence[heat.Cell], cell_width: int) -> CellText:
        return (f'{sum(cell.value for cell in row): >{cell_width}}', None)

    def column_headers(self, root_str: str, width: int, cells_visible: int) -> list[CellText]:
        column_size = self.column_size
        columns: list[CellText] = [
            (f" 📊 per {root_str}           "[:18], None),
            (f'NOW{" " * (column_size - 3)}', None)
        ]
        for i in range(1, cells_visible):
            title = self.strokes[i % 10] or ' '
            columns.append((f'{title: ^{column_size}}', None))
        remainder = width - sum(len(c[0]) for c in columns if c[0] is not None)
        columns.append(((' ' * remainder) if remainder > 0 else None, None))
        return columns

    def row_header(
        self, header: heat.RowHeader, row: Sequence[heat.Cell]
    ) -> CellText:
        raise NotImplementedError()

    def cell(
        self, index: int, cell: heat.Cell, row_header: heat.RowHeader
    ) -> CellText:
        style: Union[rich.style.Style, str] = BASIC_CELL_STYLE
        stroke = self.strokes[index % 10]
        if cell.value:
            style = self.style(cell.value)
            text = self.symbol(cell.value)
        else:
            text = stroke or "·"
        return (f'{text: ^{self.column_size}}', style)

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
    title = 'by frequency'

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

        self.source.tag_rows(self.targetted, ['targetted'], default_factory=rowheader_factory)
        self.source.tag_rows(self.untargetted, ['untargetted'], default_factory=rowheader_factory)

        current_freqs = await network.UPDATER.current_freqs()
        self.source.tag_rows(current_freqs, ['active'], default_factory=rowheader_factory if self.show_all_active else None)
        await self.source.fill_active_state()

    def row_header(
        self, header: heat.RowHeader, row: Sequence[heat.Cell]
    ) -> CellText:
        infix = ''
        style = NORMAL_TEXT
        if header.station_id:
            infix = network.STATION_ABBREVIATIONS[header.station_id]
        if header.is_tagged('targetted') or any(cell.is_tagged('targetted') for cell in row):
            if any(cell.value for cell in row):
                symbol = '▣'  # ▣🞔□⬚
                style = PROMINENT_TEXT
            else:
                symbol = '🞔'
        elif header.is_tagged('active') or any(cell.is_tagged('active') for cell in row):
            symbol = '□'
        else:
            symbol = '⬚'
            infix = infix.lower()
            style = SUBDUED_TEXT
        if not self.show_targetting:
            symbol = ' '
        stratum = ' '
        if self.show_confidence:
            if header.is_tagged('local'):
                stratum = '●'  # ◉
            elif header.is_tagged('network'):
                stratum = '◐'  # ◒⊙⬓
            elif header.is_tagged('guess'):
                stratum = '○'
        return (f'{symbol}{infix: >9}{header.label: >6} {stratum} ', style)

    def row(self, row_id: Union[str, int], row_data: Sequence[heat.Cell], width: int) -> list[CellText]:
        if (
            self.show_quiet
            or (self.show_all_active and self.source.row_headers[row_id].is_tagged('active'))
            or any(cell.value for cell in row_data)
        ):
            return super().row(row_id, row_data, width)
        return []

    def cell(
        self, index: int, cell: heat.Cell, row_header: heat.RowHeader
    ) -> CellText:
        style: Union[rich.style.Style, str] = BASIC_CELL_STYLE
        stroke = self.strokes[index % 10]
        column_size = self.column_size
        if cell.value:
            style = self.style(cell.value)
            text = f'{self.symbol(cell.value): ^{column_size}}'
        elif self.show_active_line and cell.is_tagged('active'):
            if row_header.is_tagged('targetted'):
                line = '─'
            else:
                line = '⠒'
            text = f'{(stroke or line):{line}^{column_size}}'
        else:
            text = f'{(stroke or "·"): ^{column_size}}'
        return (text, style)


class HeatMapByBandFormatter(AbstractHeatMapFormatter[heat.TableByBand]):
    title = 'by MHz band'

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
            self.source.tag_rows(bands, ['band'], default_factory=rowheader_factory)

    def row_header(
        self, header: heat.RowHeader, row: Sequence[heat.Cell]
    ) -> CellText:
        if any(cell.value for cell in row):
            style = PROMINENT_TEXT
        else:
            style = NORMAL_TEXT
        return (f' {header.label: >13} MHz ', style)


class HeatMapByStationFormatter(AbstractHeatMapFormatter[heat.TableByStation]):
    title = 'by ground station'

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

    def row_header(
        self, header: heat.RowHeader, row: Sequence[heat.Cell]
    ) -> CellText:
        if any(cell.value for cell in row):
            style = PROMINENT_TEXT
        else:
            style = NORMAL_TEXT
        return (f' {header.label.split(",", 1)[0].strip()[:ROW_HEADER_WIDTH]: >{ROW_HEADER_WIDTH}} ', style)


class HeatMapByAgentFormatter(AbstractHeatMapFormatter[heat.TableByAgent]):
    title = 'by agent'

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

    def row_header(
        self, header: heat.RowHeader, row: Sequence[heat.Cell]
    ) -> CellText:
        if any(cell.value for cell in row):
            style = PROMINENT_TEXT
        else:
            style = NORMAL_TEXT
        return (f' {header.label: >{ROW_HEADER_WIDTH}} ', style)


class HeatMapByReceiverFormatter(AbstractHeatMapFormatter[heat.TableByReceiver]):
    title = 'by receiver'

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

    def row_header(
        self, header: heat.RowHeader, row: Sequence[heat.Cell]
    ) -> CellText:
        if any(cell.value for cell in row):
            style = PROMINENT_TEXT
        else:
            style = NORMAL_TEXT
        label = header.label[-ROW_HEADER_WIDTH:]
        return (f' {label: >{ROW_HEADER_WIDTH}} ', style)


class HeatMap:
    config: dict
    last_render_time: datetime.datetime = util.now()
    bin_size: int = 60
    data_source: Callable[[int], Coroutine[Any, Any, AbstractHeatMapFormatter[Any]]]
    display: ObserverDisplay
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
            'frequency': self.by_frequency,
            'band': self.by_band,
            'station': self.by_station,
            'agent': self.by_agent,
            'receiver': self.by_receiver,
        }
        self.select_display_mode(mode)
        self.set_bin_size(config.get('bin_size', 60))
        self.set_flexible_width(util.tobool(self.config.get('flexible_width', False)))
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
            raise ValueError(f'display mode not supported: {mode}') from None
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
            util.tobool(self.config.get('show_all_active', False)),
            util.tobool(self.config.get('show_active_line', True)),
            util.tobool(self.config.get('show_confidence', True)),
            util.tobool(self.config.get('show_targetting', True)),
            util.tobool(self.config.get('show_quiet', True)),
        )
        await source.fetch()
        return source

    async def by_band(self, num_bins: int) -> AbstractHeatMapFormatter:
        source = HeatMapByBandFormatter(
            self.bin_size,
            num_bins,
            self.flexible_width,
            util.tobool(self.config.get('show_all_bands', True))
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
        observer.watch_event('packet', self.on_hfdl)
        observer.watch_event('observing', self.on_observing)

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
            logger.debug(f'not in a task? {err}')
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
            logger.debug(f'render deferred. Next render time {next_render_time}')

    def celltexts_to_text(self, texts: list[CellText], style: Optional[rich.style.Style] = None) -> rich.text.Text:
        elements: list[tuple[str, str | rich.style.Style | None]] = []
        for celltext in texts:
            text, textstyle = (celltext[0] if celltext[0] else '   ', celltext[1])
            if elements and elements[-1][1] == textstyle:   # if the styles are the same, they can be merged.
                elements[-1] = (elements[-1][0] + text, textstyle)
            else:
                elements.append((text, textstyle))
        result = rich.text.Text(style=style or '')
        for element in elements:
            result.append(*element)
        return result

    @functools.cached_property
    def reserved_width(self) -> int:
        return (
            1  # left/header padding
            + ROW_HEADER_WIDTH
            + 1  # header-padding
            + 1  # right padding
        )

    def render_column_headers(self, source: AbstractHeatMapFormatter, cells_visible: int, bin_str: str) -> rich.text.Text:
        columns: list[CellText] = []
        title = source.title
        title_len = len(title) + 1

        # walk backwards through the column headers until there's enough room for the mode title.
        for column_text in reversed(source.column_headers(bin_str, self.display.current_width, cells_visible)):
            if title_len < 0:
                columns.append(column_text)
            else:
                title_len -= len(column_text[0]) if column_text[0] is not None else 0
                if title_len < 0:
                    padding_needed = max(0, -title_len - 1)
                    padded_title = (" " * padding_needed) + title
                    title_text = (f'🭪{padded_title}', PANE_BAR_REVERSED)
                    columns.append(title_text)
        columns.reverse()

        return self.celltexts_to_text(columns)

    def render_data_rows(self, source: AbstractHeatMapFormatter, cells_visible: int) -> list[tuple[rich.text.Text | str, str | None]]:
        rows: list[tuple[rich.text.Text | str, str | None]] = []
        any_rows = False
        for key, row in source.rows():
            any_rows = True
            visible_row = row[:cells_visible]
            cells = source.row(key, visible_row, self.display.current_width)
            if cells:
                row_text = self.celltexts_to_text(cells, None)
                rows.append((row_text, "white on black"))
        if not any_rows:
            rows.append((" Awaiting data...", None))
        return rows

    async def render(self) -> None:
        # Hypothetically, we could represent each bin/ on each row as a "cell" Rich's table display. This would be
        # slightly less complex on code in this module. Rich is a good framework, but it is generic and does a lot of
        # extra work to figure out the layout when it's already statically known. Instead, this code (and its helpers)
        # combines fragments into the smallest number of Text segments possible, and represents each row on the table
        # with a single Text. Not ideal, but the concept of Cells is retained as it its flexibility
        #
        if not hasattr(self, 'display') or util.is_shutting_down():
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
            bin_str = f'{self.bin_size}s'
        else:
            bin_str = 'minute'
        source = await self.data_source(possible_bins)
        if not source.is_empty:
            self.last_render_time = util.now()
            cells_visible = source.cells_visible(body_width)
            table = rich.table.Table.grid()

            header_text = self.render_column_headers(source, cells_visible, bin_str)
            table.add_row(header_text, style=PANE_BAR)

            # logger.info(f'{body_width} // {source.cells_visible(body_width)} @ {source.column_size} = {source.cells_visible(body_width) * source.column_size}')

            for row_text, row_style in self.render_data_rows(source, cells_visible):
                if row_style:
                    table.add_row(row_text, style=row_style)
                else:
                    table.add_row(row_text)

            # for reasons I don't understand, source.source will not get garbage collected. Since we're done with the
            # source, it can be del'd, but it still feels dirty. del'ing `source` alone is insufficient.
            del source.source
        else:
            table = rich.table.Table.grid(expand=True)
            head = f" 📊 per {bin_str}"
            table.add_row(f"{head: <{self.display.current_width}}", style=PANE_BAR)
            table.add_row(" Awaiting data...")
        del source
        self.display.update_counts(table)

    async def run(self) -> None:
        try:
            while not util.is_shutting_down():
                await self.render()  # maybe_render might be better if performance is impacted.
                await asyncio.sleep(self.refresh_period)
        except Exception as exc:
            logger.error('error while rendering UI', exc_info=exc)
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


class ConsoleRedirector(rich.console.Console):
    ring: collections.deque
    output: Optional[Callable[[collections.deque], None]] = None

    def print(self, something: Any) -> None:  # type: ignore   # shut up, mypy.
        if self.output is None:
            super().print(something)
        else:
            lines = self.render_lines(something)
            self.ring.extend(rich.segment.Segments(line) for line in lines)
            self.output(self.ring)

    def ensure_size(self, size: int) -> None:
        if self.ring.maxlen and size > self.ring.maxlen:
            self.ring = collections.deque(self.ring, size)

    @classmethod
    def create(cls, size: int) -> 'ConsoleRedirector':
        that = cls()
        that.ring = collections.deque(maxlen=size)
        return that


class RichLive(rich.live.Live):
    pre_refresh: Optional[Sequence[Callable]] = None
    post_refresh: Optional[Sequence[Callable]] = None

    def refresh(self) -> None:
        with self._lock:
            for callback in self.pre_refresh or []:
                callback()
            try:
                super().refresh()
            except AssertionError as err:
                logger.debug(f'ignoring {err}')
            for callback in self.post_refresh or []:
                callback()

def exit(*_: Any) -> None:
    logger.info('exit requested')
    util.thread_local.shutdown_event.set()


def screen(loghandler: Optional[logging.Handler], debug: bool = True) -> None:
    cui_settings = settings.cui
    console = rich.console.Console()
    console.clear()
    logging_console = ConsoleRedirector.create(max(console.options.size.height or 50, 50))
    logging_handler = rich.logging.RichHandler(
        console=logging_console,
        show_time=True,
        highlighter=rich.highlighter.NullHighlighter(),
        enable_link_path=False,
    )
    heatmap = HeatMap(cui_settings['ticker'])
    cumulative_line = CumulativeLine()
    keyboard = util.Keyboard(1.0)

    forecaster = bus.RemoteURLRefresher('https://services.swpc.noaa.gov/products/noaa-scales.json', 617)

    display = ObserverDisplay(console, heatmap, keyboard, cumulative_line, forecaster)

    # setup logging
    logging_console.output = display.update_log
    FORMAT = "%(message)s"
    handlers: list[logging.Handler] = [logging_handler]
    if loghandler:
        handlers.append(loghandler)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format=FORMAT,
        datefmt="[%X]",
        handlers=handlers,
        force=True
    )
    display_updater = bus.PeriodicCallback(
        1.0 / SCREEN_REFRESH_RATE,
        [display.update_status, display.update],
        False
    )

    def observing(
        observer: hfdlobserver.HFDLObserverController,
        cumulative: network.CumulativePacketStats,
    ) -> None:
        heatmap.register(observer)
        cumulative_line.register(observer, cumulative)
        util.schedule(forecaster.run())
        util.schedule(display_updater.run())
        util.schedule(keyboard.run())
        keyboard.add_mapping('r', lambda _: observer.maybe_describe_receivers(force=True))
        keyboard.add_mapping('R', lambda _: observer.maybe_describe_receivers(force=True))

    with RichLive(
        display.root, refresh_per_second=SCREEN_REFRESH_RATE, console=console, transient=True, screen=True,
        redirect_stderr=False, redirect_stdout=False, vertical_overflow="crop",
    ) as live:
        live.pre_refresh = [  # type: ignore[attr-defined]
            # display.update_status,
            # display.update,
        ]
        live.post_refresh = [  # type: ignore[attr-defined]
            display.on_render,
        ]
        hfdlobserver.observe(on_observer=observing)
        console.clear_live()
        console.clear()

if __name__ == '__main__':
    screen(None)
