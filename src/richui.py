# richui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
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

import hfdl_observer.baseui as baseui
import hfdl_observer.bus as bus
import hfdl_observer.data as data
import hfdl_observer.heat as heat
import hfdl_observer.heatmapui as heatmapui
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


STYLES = {
    "PANE_BAR": rich.style.Style.parse("bright_white on bright_black"),
    "PANE_BAR_REVERSED": rich.style.Style.parse("bright_black on bright_white"),
    "SUBDUED_TEXT": rich.style.Style.parse("grey50 on black"),
    "NORMAL_TEXT": rich.style.Style.parse("white on black"),
    "PROMINENT_TEXT": rich.style.Style.parse("bright_white on black"),
    "BASIC_CELL_STYLE": rich.style.Style.parse("bright_black on black"),
    "STATUS_LINE": rich.style.Style.parse("on dark_green"),
}
BOLD = rich.style.Style.parse("bold")
FORECAST_STYLEMAP = {
    "extreme": rich.style.Style.parse("yellow1 on dark_red"),
    "severe": rich.style.Style.parse("black on red1"),
    "strong": rich.style.Style.parse("black on dark_orange"),
    "moderate": rich.style.Style.parse("black on orange1"),
    "minor": rich.style.Style.parse("black on gold1"),
    "none": rich.style.Style.parse("white on bright_black"),
    None: rich.style.Style.parse("white on bright_black"),
}


CellText = tuple[str | None, str | rich.style.Style | None]


class ObserverDisplay(baseui.BaseObserverDisplay, heatmapui.HeatMapConsumer):
    status: Optional[rich.table.Table] = None
    totals: Optional[rich.table.Table] = None
    counts: Optional[rich.table.Table] = None
    tty_bar: Optional[rich.table.Table] = None
    tty: Optional[rich.table.Table] = None
    forecast: rich.text.Text
    uptime_text: rich.text.Text
    totals_text: rich.text.Text
    garbage: collections.deque[rich.table.Table]
    day_count: int | None = None
    week_count: int | None = None
    spark_data: Sequence[int] | None = None

    def __init__(
        self,
        console: rich.console.Console,
        heatmap: "HeatMap",
        keyboard: util.Keyboard,
        cumulative_line: baseui.CumulativeLine,
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
        self.forecast = rich.text.Text("(space weather unavailable)")
        self.setup_status()
        self.totals_text = rich.text.Text("", style=STYLES["NORMAL_TEXT"])
        self.setup_totals()
        self.update_status()
        self.update_tty_bar()
        self.keyboard = self.setup_keyboard(keyboard)
        forecaster.watch_event("response", self.on_forecast)

    def update(self) -> None:
        t = rich.table.Table.grid(expand=True, pad_edge=False, padding=(0, 0))
        if self.status:
            t.add_row(self.status)
        if len(self.totals_text):
            t.add_row(self.totals)
        else:
            self.update_totals(self.cumulative_line.cumulative)
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
        text.append(" 📡 ")
        text.append("HFDL Observer", style=BOLD)
        table.add_row(text, self.forecast, self.uptime_text, style=STYLES["STATUS_LINE"])
        self.status = table

    def setup_totals(self) -> None:
        if self.totals:
            self.garbage.append(self.totals)
        table = rich.table.Table.grid(expand=True)
        table.add_column()  # title
        table.add_column(justify="right")  # Grand Total
        table.add_row(
            rich.text.Text(" Totals (since start)", style="bold bright_white"), self.totals_text, style="white on black"
        )
        self.totals = table

    def update_status(self) -> None:
        if not hasattr(self, "uptime_text"):
            return
        uptime = datetime.datetime.now() - start
        uptime -= datetime.timedelta(0, 0, uptime.microseconds)
        self.uptime_text.plain = f"UP {uptime}"

    def update_tty_bar(self) -> None:
        if self.tty_bar:
            self.garbage.append(self.tty_bar)
        table = rich.table.Table.grid(expand=True)
        table.add_row(" 📰 Log", style=STYLES["PANE_BAR"])
        self.tty_bar = table

    def update_totals(self, cumulative: network.CumulativePacketStats) -> None:
        util.schedule(self._update_totals(cumulative))

    async def _update_totals(self, cumulative: network.CumulativePacketStats) -> None:
        await self.refresh_counts()

        actives = str(self.cumulative_line.active) if self.cumulative_line.active is not None else "?"
        targets = str(self.cumulative_line.target_observed) if self.cumulative_line.target_observed is not None else "?"
        untargets = f" +{self.cumulative_line.bonus_observed}" if self.cumulative_line.bonus_observed else ""
        texts = [
            f"⏬{cumulative.from_air} ⏫{cumulative.from_ground}",
            f"🌐{cumulative.with_position} ❔{cumulative.no_position}",
            f"📰{cumulative.squitters}",
            f"🔎{targets}/{actives}{untargets}",
            f"📶{cumulative.packets}",
        ]
        if self.day_count:
            texts[-1] += f" ♦ {self.day_count}"
        if self.week_count:
            texts[-1] += f" ♦ {self.week_count}"
        if self.spark_data:
            texts[-1] += f" {util.sparkline(self.spark_data)}"
        self.totals_text.plain = f"{' ⎮ '.join(texts)} "

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

    def update_heatmap(self, spans: Sequence[rich.text.Text]) -> None:
        if spans:
            table = rich.table.Table.grid(expand=True)
            for span in spans:
                table.add_row(span)
        self.counts = table

    async def refresh_counts(self) -> None:
        self.day_count = await data.PACKET_WATCHER.count_packets_since(datetime.timedelta(days=1))
        self.week_count = await data.PACKET_WATCHER.count_packets_since(datetime.timedelta(days=7))
        self.spark_data = await data.PACKET_WATCHER.daily_counts(7)

    def on_forecast(self, forecast: Any) -> None:
        try:
            recent = forecast["-1"]
            current = forecast["0"]
            forecast1d = forecast["1"]
            text = self.forecast
            text.plain = ""
            text.append(f"R{recent['R']['Scale'] or '-'}", style=FORECAST_STYLEMAP[recent["R"]["Text"]])
            text.append("⎮")
            text.append(f"S{recent['S']['Scale'] or '-'}", style=FORECAST_STYLEMAP[recent["S"]["Text"]])
            text.append("⎮")
            text.append(f"G{recent['G']['Scale'] or '-'}", style=FORECAST_STYLEMAP[recent["G"]["Text"]])
            text.append("  ")
            text.append(f"R{current['R']['Scale'] or '-'}", style=FORECAST_STYLEMAP[current["R"]["Text"]])
            text.append("⎮")
            text.append(f"S{current['S']['Scale'] or '-'}", style=FORECAST_STYLEMAP[current["S"]["Text"]])
            text.append("⎮")
            text.append(f"G{current['G']['Scale'] or '-'}", style=FORECAST_STYLEMAP[current["G"]["Text"]])
            text.append("  ")
            (text.append(f"R{forecast1d['R']['MinorProb']}/{forecast1d['R']['MajorProb']}", FORECAST_STYLEMAP["none"]),)
            text.append("⎮")
            (text.append(f"S{forecast1d['S']['Prob']}", FORECAST_STYLEMAP["none"]),)
            text.append("⎮")
            (text.append(f"G{forecast1d['G']['Scale'] or '-'}", FORECAST_STYLEMAP[forecast1d["G"]["Text"]]),)
        except Exception as err:
            logger.debug("ignoring forecaster error", exc_info=err)

    @property
    def current_width(self) -> int:
        return self.console.options.size.width

    @current_width.setter
    def current_width(self, _: int) -> None:
        pass

    @property
    def current_height(self) -> int:
        return self.console.options.size.height or 25

    def clear_table(self, table: Optional[rich.table.Table]) -> None:
        # dubious, attempt to voodoo patch a possible memory leak in Rich
        with self.root._lock:
            if table is not None:
                if hasattr(table.rows, "clear"):
                    table.rows.clear()
                if hasattr(table.columns, "clear"):
                    table.columns.clear()

    def on_render(self) -> None:
        while len(self.garbage) > 0:
            try:
                garbage = self.garbage.popleft()
            except IndexError:
                break
            self.clear_table(garbage)


@functools.cache
def map_style(style: str) -> rich.style.Style | None:
    if not style:
        return None
    if style.startswith("rgb("):
        rgb = tuple(int(x) for x in style[4:-1].split(","))
        return rich.style.Style(bgcolor=f"rgb({','.join(str(i) for i in rgb)})", color="black")
    return STYLES[style]


def transition(
    first_style: rich.style.Style | None, second_style: rich.style.Style | None
) -> tuple[str, rich.style.Style | None]:
    char = "▐"
    style = rich.style.Style(
        color=second_style.bgcolor if second_style else None,
        bgcolor=first_style.bgcolor if first_style else None,
    )
    return (char, style)


class HeatMap(heatmapui.HeatMap):
    def celltexts_to_text(self, texts: list[heatmapui.CellText], style: Optional[rich.style.Style] = None) -> Sequence:
        elements: list[tuple[str, rich.style.Style | None]] = []
        for celltext in texts:
            text, textstyle = (celltext[0] if celltext[0] else "   ", celltext[1])
            if elements and elements[-1][1] == textstyle:  # if the styles are the same, they can be merged.
                # if self.flexible_width and elements and elements[-1][0].endswith(" ") and text.startswith(" "):
                #     text = text[1:]
                elements[-1] = (elements[-1][0] + text, map_style(textstyle))
            else:
                # aborted attempt to use half blocks for greater data density. It doesn't help readability, and has
                # additional layout quirks.
                # if self.flexible_width and elements and elements[-1][0].endswith(" ") and text.startswith(" "):
                #     text = text[1:]
                #     elements[-1] = (elements[-1][0][:-1], elements[-1][1])
                #     elements.append(transition(elements[-1][1], map_style(textstyle)))
                elements.append((text, map_style(textstyle)))
        result = rich.text.Text(style=style or "")
        for element in elements:
            result.append(*element)
        return [result]

    def render_column_headers(
        self, source: heatmapui.AbstractHeatMapFormatter, cells_visible: int, bin_str: str
    ) -> Sequence[rich.text.Text]:
        columns: list[heatmapui.CellText] = []
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
                    columns.append((f"🭪{padded_title}", "PANE_BAR_REVERSED"))
        columns.reverse()

        return self.celltexts_to_text(columns, STYLES["PANE_BAR"])

    def render_data_rows(
        self, source: heatmapui.AbstractHeatMapFormatter, cells_visible: int
    ) -> list[tuple[rich.text.Text | str, str | None]]:
        rows: list[tuple[rich.text.Text | str, str | None]] = []
        any_rows = False
        for key, row in source.rows():
            any_rows = True
            visible_row = row[:cells_visible]
            cells = source.row(key, visible_row, self.display.current_width)
            if cells:
                row_text = self.celltexts_to_text(cells, None)
                rows.extend(row_text)
        if not any_rows:
            rows.append((" Awaiting data...", "NORMAL_TEXT"))
        return rows


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
    def create(cls, size: int) -> "ConsoleRedirector":
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
                logger.debug(f"ignoring {err}")
            for callback in self.post_refresh or []:
                callback()


def exit(*_: Any) -> None:
    logger.info("exit requested")
    util.shutdown_event.set()


def screen(loghandler: Optional[logging.Handler], debug: bool = True, quiet: bool = False) -> None:
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
    heatmap = HeatMap(cui_settings["ticker"])
    cumulative_line = baseui.CumulativeLine()
    keyboard = util.Keyboard(1.0)

    forecaster = bus.RemoteURLRefresher("https://services.swpc.noaa.gov/products/noaa-scales.json", 617)

    display = ObserverDisplay(console, heatmap, keyboard, cumulative_line, forecaster)

    # setup logging
    logging_console.output = display.update_log
    FORMAT = "%(message)s"
    handlers: list[logging.Handler] = [logging_handler]
    if loghandler:
        handlers.append(loghandler)
    logging.basicConfig(
        level=logging.WARNING if quiet else (logging.DEBUG if debug else logging.INFO),
        format=FORMAT,
        datefmt="[%X]",
        handlers=handlers,
        force=True,
    )
    display_updater = bus.PeriodicCallback(1.0 / SCREEN_REFRESH_RATE, [display.update_status, display.update], False)

    def observing(
        observer: hfdlobserver.HFDLObserverController,
        cumulative: network.CumulativePacketStats,
    ) -> None:
        heatmap.register(observer)
        cumulative_line.register(observer, cumulative)
        util.schedule(forecaster.run())
        util.schedule(display_updater.run())
        util.schedule(keyboard.run())
        keyboard.add_mapping("r", lambda _: observer.maybe_describe_receivers(force=True))
        keyboard.add_mapping("R", lambda _: observer.maybe_describe_receivers(force=True))

    with RichLive(
        display.root,
        refresh_per_second=SCREEN_REFRESH_RATE,
        console=console,
        transient=True,
        screen=True,
        redirect_stderr=False,
        redirect_stdout=False,
        vertical_overflow="crop",
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


if __name__ == "__main__":
    screen(None)
