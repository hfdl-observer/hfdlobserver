# jsonui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

from __future__ import annotations

import datetime
import json
import http.server
import logging
import threading

from typing import Any, Sequence

import hfdl_observer.baseui as baseui
import hfdl_observer.heatmapui as heatmapui
import hfdl_observer.network as network
import hfdl_observer.util as util

logger = logging.getLogger(__name__)
start = datetime.datetime.now()


class ObserverDisplay(baseui.SecondaryObserverDisplay):
    status: None | dict = None
    totals: None | dict = None
    counts: None | dict = None
    # tty_bar: Optional[rich.table.Table] = None
    # tty: Optional[rich.table.Table] = None
    forecast: dict
    uptime_text: str
    day_count: int | None = None
    week_count: int | None = None
    spark_data: Sequence[int] | None = None
    current_state: dict

    def __init__(self, config: dict) -> None:
        self.uptime_text = "STARTING"
        self.current_state = {}
        self.forecast = {}
        self.setup_status()
        self.totals = {}
        self.setup_totals()
        self.update_status()
        self.run_server(config)
        # self.update_tty_bar()

    def update(self) -> None:
        self.update_status()
        out = {
            "status": self.status,
            "totals": self.totals if self.totals else None,
            "counts": self.counts,
        }
        # if self.tty:
        #     if self.tty_bar:
        #         t.add_row(self.tty_bar)
        #     t.add_row(self.tty)
        # if t.row_count:
        #     self.root.update(t)
        self.current_state = out

    def setup_status(self) -> None:
        self.status = {
            "icon": "📡",
            "title": "HFDL Observer",
            "forecast": self.forecast,
            "uptime": self.uptime_text,
        }

    def setup_totals(self) -> None:
        self.totals = {"caption": "Totals (since start)"}

    def update_status(self) -> None:
        if not hasattr(self, "uptime_text"):
            return
        uptime = datetime.datetime.now() - start
        uptime -= datetime.timedelta(0, 0, uptime.microseconds)
        self.uptime_text = f"{uptime}"
        self.setup_status()

    def update_cumulative(self, line: baseui.CumulativeLine, stats: network.CumulativePacketStats) -> None:
        actives = str(line.active) if line.active is not None else "?"
        targets = str(line.target_observed) if line.target_observed is not None else "?"
        untargets = f" +{line.bonus_observed}" if line.bonus_observed else ""

        self.totals = stats.as_dict()
        self.totals.update(
            {
                "target_freqs": targets,
                "active_freqs": actives,
                "untarget_freqs": untargets.strip(" +"),
                "last_day": self.day_count if self.day_count else None,
                "last_week": self.week_count if self.week_count else None,
                "sparkline": util.sparkline(self.spark_data) if self.spark_data else None,
            }
        )

    def update_heatmap(self, formatter: heatmapui.AbstractHeatMapFormatter, cells_visible: int, bin_str: str) -> None:
        source = formatter.source
        rows = []
        num_data_cells = 0
        for ix, (row_key, row_data) in enumerate(source):
            if not row_data:
                continue
            num_data_cells = max(num_data_cells, len(row_data))
            row_header = source.row_headers[row_key]
            row_header_cell = row_header.as_dict()
            row_header_cell["row_num"] = ix
            if row_header.station_id:
                station = network.STATIONS[row_header.station_id]
                row_header_cell["station"] = {
                    "abbreviation": network.STATION_ABBREVIATIONS[row_header.station_id],
                    "name": station.station_name,
                    "lat": station.latitude,
                    "long": station.longitude,
                }
            row = {"header": row_header_cell, "data": []}
            for row_cell in row_data:
                cell = row_cell.as_dict()
                cell_style = formatter.style(row_cell.value)  # slightly dubious, but it works.
                if "BASIC_CELL_STYLE" != cell_style:
                    cell["style"] = cell_style
                row["data"].append(cell)
            row["totals"] = {
                "value": sum(c.value or 0 for c in row_data),
            }
            rows.append(row)

        # column headers
        headers = [
            {"value": "NOW", "offset": 0},
        ]
        for i in range(1, num_data_cells):
            headers.append({"value": "", "offset": i})

        self.counts = {
            "mode": formatter.title,
            "bin_size": bin_str,
            "headers": headers,
            "rows": rows,
        }

    def update_counts(self, day_count: None | int, week_count: None | int, spark_data: Sequence[int]) -> None:
        self.day_count = day_count
        self.week_count = week_count
        self.spark_data = spark_data

    def update_forecast(self, forecast: dict) -> None:
        try:
            recent = forecast["-1"]
            current = forecast["0"]
            forecast1d = forecast["1"]

            def forecast_element(basis: dict) -> dict:
                return {"scale": basis["Scale"] or "-", "style": basis["Text"] or "default"}

            out = {
                "recent": {
                    "r": forecast_element(recent["R"]),
                    "s": forecast_element(recent["S"]),
                    "g": forecast_element(recent["G"]),
                },
                "current": {
                    "r": forecast_element(current["R"]),
                    "s": forecast_element(current["S"]),
                    "g": forecast_element(current["G"]),
                },
                "tomorrow": {
                    "r": f"{forecast1d['R']['MinorProb']}/{forecast1d['R']['MajorProb']}",
                    "s": forecast1d["S"]["Prob"],
                    "g": forecast_element(forecast1d["G"]),
                },
            }
        except Exception as err:
            logger.debug("ignoring forecaster error", exc_info=err)
        else:
            self.forecast = out

    # ### DANGEROUS STUFF
    server: http.server.ThreadingHTTPServer | None = None

    def handler_factory(self, *args: Any, **kwargs: Any) -> http.server.BaseHTTPRequestHandler:
        handler = NaiveHandler(*args, **kwargs)
        handler.source = self
        return handler

    def run_server(self, config: dict) -> None:
        if not self.server:
            # this = self

            class Handler(NaiveHandler):
                source = self
                refresh_delay: int = int(config.get("refresh", 16))

            address = config["address"]
            port = int(config["port"])

            self.server = http.server.ThreadingHTTPServer((address, port), Handler)
            try:
                self.serverThread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.serverThread.start()
                logger.info("HTTP server started")
            except Exception as err:
                logger.error("could not start web server", exc_info=err)


class HTMLFormatter:
    refresh_delay: int = 0

    def format_forecast_element(self, prefix: str, element: str | dict) -> str:
        if isinstance(element, str):
            return f'<li class="{prefix} default">{prefix.upper()}{element}</li>\n'
        return f"""<li class="{prefix} {element["style"]}">{prefix.upper()}{element["scale"]}</li>\n"""

    def format_forecast(self, forecast: dict) -> str:
        parts = ['<ul class="forecast">\n']
        for period_name in ["recent", "current", "tomorrow"]:
            period = forecast[period_name]
            parts.append(f'<li class="{period_name}"><ul>')
            for part in "rsg":
                parts.append(self.format_forecast_element(part, period[part]))
            parts.append("</ul></li>")
        parts.append("</ul>")
        return "\n".join(parts)

    def format_rowheader(self, rowheader: dict) -> str:
        classes = [f"tag_{t}" for t in rowheader["tags"] if t]
        classes.append("rowheader")
        thclass = f''' class="{" ".join(classes)}"''' if classes else ""
        spans = [
            f"""<th{thclass}>"""
            f"""<span class="value">{rowheader["value"]}</span>"""
        ]
        station = rowheader.get("station")
        if station:
            spans.append(f"""<span class="stnid">{rowheader["station_id"]}.</span>""")
            for key in ["abbreviation", "name", "lat", "long"]:
                if v := station.get(key):
                    spans.append(f"""<span class="{key}">{v}</span>""")
        spans.append('</th><th class="fakeheader"></th>')
        return "".join(spans)

    def format_rowcell(self, cell: dict) -> str:
        classes = [f"tag_{t}" for t in cell["tags"] if t]
        classes.append("bin")
        class_str = f''' class="{" ".join(classes)}"''' if classes else ""
        if int(cell["value"]):
            style = f''' style="color: black; background-color: {cell["style"]};"''' if "style" in cell else ""
            value = cell["value"]
        else:
            style = ""
            value = ""
        return f"""<td{class_str}{style}>{value}</td>"""

    def format_rowtotal(self, total: dict) -> str:
        return f"""<th class="fakefooter"></th><th class="rowfooter">{total["value"]}</th>"""

    def format_counts(self, counts: dict) -> str:
        if not counts:
            return ""
        parts = [
            f"""
            <div id="count_header">
            <span class="mode">{counts["mode"]}</span>
            <span class="size">{counts["bin_size"]}</span>
            </div><div id="count_table">
            <table class="counts {counts["mode"].replace(" ", "_")}">
            <thead><tr><th class="rowheader">...</th><th class="fakeheader"></th>
            """,
        ]
        for col in counts["headers"]:
            if col["value"] != "Total":  # hack for unknown reason.
                parts.append(f"""<th class="bin">{col["value"]}</th>""")
        parts.append('<th class="fakefooter"></th><th class="rowfooter">Total</th></tr></thead><tbody>')
        for row in counts["rows"]:
            row_data = row["data"]
            rtd = row["totals"]
            classes = [""]
            if not int(rtd.get("value")):
                classes.append("empty")
                continue
            class_str = f''' class="{" ".join(classes)}"''' if classes else ""
            parts.append(f"<tr{class_str}>")  # add flagging to hide by CSS.
            parts.append(self.format_rowheader(row["header"]))
            for rd in row_data:
                parts.append(self.format_rowcell(rd))
            parts.append(self.format_rowtotal(rtd))
            parts.append("</tr>")
        parts.append("</tbody></table></div>")
        return "\n".join(parts)

    def format_statusline(self, status: dict) -> str:
        parts = ['<ul class="statusline">']
        all_keys = [
            "from_air",
            "from_ground",
            "with_position",
            "no_position",
            "squitters",
            "target_freqs",
            "active_freqs",
            "untarget_freqs",
            "packets",
            "last_day",
            "last_week",
            "sparkline",
        ]
        for k in all_keys:
            v = status[k]
            if v in ("", None):
                continue
            parts.append(f'<li class="{k}">{v}</li>')
        parts.append("</ul>")
        return "\n".join(parts)

    def styles(self) -> str:
        return """
html {
    font-family: "Droid Sans Mono", "Roboto Mono", "Andale Mono", "Courier New", "Courier", mono, fixed;
    background-color: black;
    color: #a0a0a0;
}
.bin {
    min-width: 2em;
    text-align: center;
}
li {
    display: inline-block;
}
table span.stnid, table span.name, table span.abbreviation, table span.lat, table span.long, table span.frequency {
    display: none;
}
table.by_frequency span.abbreviation, table.by_frequency span.frequency {
    display: inline-block;
}
table.by_ground_station span.stnid, table.by_ground_station span.abbreviation {
    display: inline-block;
    font-size: smaller;
}
table.by_ground_station tbody th.rowheader span.value {
    display: none;
}
tr th span {
    display: inline-block;
    padding-left: 0.25ex;
    padding-right: 0.25ex;
}
table.by_frequency tr th span.abbreviation {
    font-size: smaller;
    color: #808080;
    float: left;
    text-align: left;
}
table.by_frequency tr th span.value {
    float: right;
    text-align: right;
}
#topline {
    background-color: #006000;
    color: white;
}
#statusline {
    background-color: #606060;
    color: white;
    text-align: right;
}
#count_header {
    background-color: #303030;
    color: #a0a0a0;
    font-weight: bold;
    padding: 0.5ex;
    text-align: right;
}
#count_table {
    overflow-x: scroll;
}
ul {
    padding: 2px;
    margin: 0px;
}
.uptime {
    float: right;
    padding-right: 1ex;
}
.forecast ul {
    margin-left: 1em;
    margin-right: 1em;
}
.forecast ul li {
    margin-top: -1px;
    margin-bottom: -1px;
}
.forecast .extreme {
    color: yellow;
    background-color: #440000;
}
.forecast .severe {
    color: black;
    background-color: #880000;
}
.forecast .strong {
    color: white;
    background-color: #804000;
}
.forecast .moderate {
    color: white;
    background-color: #a05000;
}
.forecast .minor {
    color: black;
    background-color: gold;
}
.forecast .default, .forecast .none {
    color: #a0a0a0;
    background-color: #202020;
}
.from_air:before {
    content: "⏬";
    padding-right: 2px;
}
.from_ground:before {
    content: "⏫";
    padding-right: 2px;
}
.with_position:before {
    content: "🌐";
    padding-right: 2px;
}
.no_position:before {
    content: "❔";
    padding-right: 2px;
}
.squitters:before {
    content: "📰";
    padding-right: 2px;
}
.target_freqs:before {
    content: "🔎";
    padding-right: 2px;
}
.active_freqs:before {
    content: "/";
    padding-right: 2px;
}
.untarget_freqs:before {
    content: "+";
    padding-right: 2px;
}
.packets:before {
    content: "📶";
    padding-right: 2px;
}
.rowheader, .fakeheader {
    width: 8em;
    min-width: 8em;
}
.rowheader {
    position: fixed;
    left: 0;
    top: auto;
    background-color: black;
    border: 1px solid black;
    white-space: nowrap;
}
.rowfooter, .fakefooter {
    width: 4em;
    min-width: 4em;
}
.rowfooter {
    position: fixed;
    right: 0;
    top: auto;
    background-color: black;
    border: 1px solid black;
}
.bin {
    margin: 1px;
    border: 1px solid black;
}
table {
    border: 0px;
    border-collapse: collapse;
}
.tag_active.bin:empty {
    padding: 0px;
    border: 0px;
}
.tag_active.bin:empty:before {
    content: "";
    display: block;
    height: 0px;
    background-color: black;
    border-top: 1px dashed #808080;
    margin: 0px;
    padding: 0px;
}
.tag_untargetted.bin {
    background-color: #201414;
}
.tag_targetted.bin {
    background-color: #142014;
}
#jsonlink {
    font-size: small;
    border-radius: 1ex;
    background-color: #50a050;
    float: right;
    margin-right: 1pc;
    margin-left: 1pc;
    padding-left: 1ex;
    padding-right: 1ex;
    margin-top: 0.33ex;
}
#jsonlink a, #jsonlink a:link {
    color: white;
    text-decoration: none;
}
        """

    def render_topline(self, topline: dict) -> str:
        return f"""
        <ul class="topline">
        <li class="icon">{topline["icon"]}</li>
        <li class="title">{topline["title"]}</li>
        <li class="forecast">{self.format_forecast(topline["forecast"])}</li>
        <li class="uptime">{topline["uptime"]}</li>
        <li id="jsonlink"><a href="display.json">JSON</a></li>
        </ul>
        """

    def render(self, state: dict) -> str:
        status = state["totals"]
        statusline = f"""<ul class="statusline">{self.format_statusline(status)}</ul>"""
        refresh = f'<meta http-equiv="refresh" content="{self.refresh_delay}">' if self.refresh_delay > 1 else ""
        head = f"""
        <head>
        <title>HFDLObserver</title>
        <style>{self.styles()}</style>
        {refresh}
        </head>
        """
        body = f"""
        <body>
        <div id="topline">{self.render_topline(state["status"])}</div>
        <div id="statusline">{statusline}</div>
        <div id="counts">{self.format_counts(state["counts"])}</div>
        </body>
        """
        return f"<html>{head}{body}</html>"


class NaiveHandler(http.server.BaseHTTPRequestHandler, HTMLFormatter):
    source: ObserverDisplay | None = None

    def do_GET(self) -> None:
        url_lookup = {
            "/display.json": ("application/json", self.get_json_response),
            "/display.html": ("text/html; charset=utf-8", self.get_html_response),
            "/": ("text/html", self.get_html_response),
        }
        logger.info(f"GET {self.path}")
        try:
            handler = url_lookup[self.path]
        except Exception:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"")
        else:
            body = handler[1]().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", handler[0])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def get_json_response(self) -> str:
        return json.dumps(self.source.current_state) if self.source else ""

    def get_html_response(self) -> str:
        return self.render(self.source.current_state) if self.source else ""


if __name__ == "__main__":
    import pathlib

    incoming = (pathlib.Path.home() / "Downloads/display.json").read_text()
    state = json.loads(incoming)
    formatter = HTMLFormatter()
    print(formatter.render(state))
