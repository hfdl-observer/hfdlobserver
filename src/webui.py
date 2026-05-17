# webui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

from __future__ import annotations

import collections
import datetime
import json
import http.server
import logging
import pathlib
import re
import threading

from typing import Any, Callable, Sequence

import hfdl_observer.aircraft as aircraft
import hfdl_observer.env as env
import jsonui

logger = logging.getLogger(__name__)
start = datetime.datetime.now()
UNKNOWN = "-"


class ObserverDisplay(jsonui.ObserverDisplay):
    server: http.server.ThreadingHTTPServer | None = None
    url_handler: URLHandler
    stylesheet_path: pathlib.Path
    refresh_delay: int

    def __init__(self, *, config: dict):
        jsonui.ObserverDisplay.__init__(self, config=config)
        self.refresh_delay = int(config.get("refresh", 16))
        self.resource_path = pathlib.Path(__file__).parent.parent / "resources"
        stylesheet_name = config.get("stylesheet_path")
        if stylesheet_name:
            self.stylesheet_path = env.as_path(stylesheet_name)
        else:
            self.stylesheet_path = self.resource_path / "default.css"
        self.url_handler = URLHandler(config)
        if config.get("address"):
            self.run_server(config)

    @property
    def stylesheet_text(self) -> str:
        css_file = self.stylesheet_path
        text: str = css_file.read_text()
        return text

    def run_server(self, config: dict) -> None:
        if not self.server:

            class Handler(NaiveHandler):
                url_handler = self.url_handler
                source = self

            address = config["address"]
            port = int(config["port"])

            self.server = http.server.ThreadingHTTPServer((address, port), Handler)
            try:
                self.serverThread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.serverThread.start()
                logger.info("HTTP server started")
            except Exception as err:
                logger.error("could not start web server", exc_info=err)


FORECAST_TIPS = {
    ("recent", "r"): "Radio degradation previous 24h",
    ("recent", "s"): "Solar storm level previous 24h",
    ("recent", "g"): "Geomagnetic impacts previous 24h",
    ("current", "r"): "current radio degradation",
    ("current", "s"): "current solar storm level",
    ("current", "g"): "current geomagnetic impacts",
    ("tomorrow", "r"): "chance of minor/major radio blackout next 24h",
    ("tomorrow", "s"): "chance of S1 solar storm next 24h",
    ("tomorrow", "g"): "expected geomagnetic impact next 24h",
}


def simple_sanitize(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-zA-Z0-9 .,_-]", "", str(value))


class URLHandler:
    urls: dict[str, tuple[str, Callable]]

    @classmethod
    def static_file(cls, file: str | pathlib.Path, replacements: dict[str, str]) -> Callable:
        def f(source: ObserverDisplay) -> str:
            filepath = source.resource_path / file
            text = filepath.read_text()
            for find, replace in replacements.items():
                text = text.replace(find, replace)
            return text

        return f

    def __init__(self, config: dict):
        self.config = config
        aircraft_config = config.get("aircraft", {})

        self.urls = {
            "/": ("text/html; charset=utf-8", self.get_root),
            "/display.json": ("application/json", self.get_display_json_response),
            "/aircraft.json": ("application/json", self.get_aircraft_json_response),
            "/messages.json": ("application/json", self.get_messages_json_response),
            "/display.html": ("text/html; charset=utf-8", MainDisplayHTMLFormatter.render),
            # "/full.html": ("text/html; charset=utf-8", self.get_all_html_response),
            # "/stylesheet.css": ("text/css; charset=utf-8", URLHandler.static_file(self.source.stylesheet_path, {})),
        }
        if "maptiler_key" in config:
            self.urls["/globe.html"] = (
                "text/html; charset=utf-8",
                URLHandler.static_file(
                    "globe.html",
                    {
                        "{{maptiler_key}}": simple_sanitize(config["maptiler_key"]),
                        "{{home_long}}": simple_sanitize(float(aircraft_config.get("longitude", 0.00))),
                        "{{home_lat}}": simple_sanitize(float(aircraft_config.get("latitude", 0.00))),
                    },
                ),
            )
        else:
            logging.warning("No MapTiler key is configured; disabling URLs")

    def get_root(self, source: ObserverDisplay) -> str:
        return MainDisplayHTMLFormatter.render(source, LowerPaneHTMLFormatter.render(source))

    def get_display_json_response(self, source: ObserverDisplay) -> str:
        return json.dumps(source.current_state) if source else ""

    def get_messages_json_response(self, source: ObserverDisplay) -> str:
        return json.dumps(list(reversed(source.recent_packets))) if source else ""

    def get_aircraft_json_response(self, source: ObserverDisplay) -> str:
        if source and source.tracker:
            sorted_ac = [
                ac.asdict() for ac in sorted(source.tracker.aircraft_by_session.values(), key=lambda e: e.seen)
            ]
            return json.dumps(sorted_ac)
        else:
            return ""


class MainDisplayHTMLFormatter:
    @classmethod
    def format_forecast_element(cls, period: str, prefix: str, element: str | dict) -> str:
        tip = FORECAST_TIPS[(period, prefix)]
        if isinstance(element, str):
            return f'<li class="{prefix} default" title="{tip}">{prefix.upper()}{element}</li>\n'
        return f"""<li class="{prefix} {element["style"]}" title="{tip}">{prefix.upper()}{element["scale"]}</li>\n"""

    @classmethod
    def format_forecast(cls, forecast: dict | None) -> str:
        if not forecast:
            return ""
        parts = ['<ul class="forecast">\n']
        for period_name in ["recent", "current", "tomorrow"]:
            period = forecast[period_name]
            parts.append(f'<li class="{period_name}"><ul>')
            for part in "rsg":
                parts.append(cls.format_forecast_element(period_name, part, period[part]))
            parts.append("</ul></li>")
        parts.append("</ul>")
        return "\n".join(parts)

    @classmethod
    def format_rowheader(cls, rowheader: dict) -> str:
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
        spans.append('</th><!--th class="fakeheader"--></th>')
        return "".join(spans)

    @classmethod
    def format_rowcell(cls, cell: dict) -> str:
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

    @classmethod
    def format_rowtotal(cls, total: dict) -> str:
        return f"""<th class="rowfooter">{total["value"]}</th>"""

    @classmethod
    def format_counts(cls, counts: dict | None) -> str:
        if not counts:
            return ""
        parts = [
            f"""
            <div id="count_header">
            <span class="mode">{counts["mode"]}</span>
            <span class="size">{counts["bin_size"]}</span>
            </div><div id="count_table">
            <table class="counts {counts["mode"].replace(" ", "_")}">
            <thead><tr><th class="rowheader">...</th>
            """,
        ]
        for col in counts["headers"]:
            if col["value"] != "Total":  # hack for unknown reason.
                parts.append(f"""<th class="bin">{col["value"]}</th>""")
        parts.append('<th class="rowfooter">Total</th></tr></thead><tbody>')
        for row in counts["rows"]:
            row_data = row["data"]
            rtd = row["totals"]
            classes = [""]
            if not int(rtd.get("value")):
                classes.append("empty")
                continue
            class_str = f''' class="{" ".join(classes)}"''' if classes else ""
            parts.append(f"<tr{class_str}>")  # add flagging to hide by CSS.
            parts.append(cls.format_rowheader(row["header"]))
            for rd in row_data:
                parts.append(cls.format_rowcell(rd))
            parts.append(cls.format_rowtotal(rtd))
            parts.append("</tr>")
        parts.append("</tbody></table></div>")
        return "\n".join(parts)

    @classmethod
    def format_statusline(cls, status: dict | None) -> str:
        if not status:
            return ""
        parts = ['<ul class="statusline">']
        all_keys = [
            ("from_air", "packets received from aircraft"),
            ("from_ground", "packets received from ground stations"),
            ("with_position", "packets with position data"),
            ("no_position", "packets without position data"),
            ("squitters", "squitter packets"),
            ("target_freqs", "number of active frequencies observed"),
            ("active_freqs", "number of declared active frequencies"),
            ("untarget_freqs", "additional frequencies observed"),
            ("packets", "total number of packets received since HFDLObserver started"),
            ("last_day", "number of packets received in the last 24h"),
            ("last_week", "number of packets received in the last 7d"),
            ("sparkline", "relative reception of packets each day for last 7d"),
        ]
        for k, tip in all_keys:
            v = status[k]
            if v in ("", None):
                continue
            parts.append(f'<li class="{k}" title="{tip}">{v}</li>')
        parts.append("</ul>")
        return "\n".join(parts)

    @classmethod
    def render_topline(cls, topline: dict | None) -> str:
        if topline is None:
            topline = {}
        return f"""
        <ul class="topline">
        <li class="icon">{topline.get("icon", "")}</li>
        <li class="title">{topline.get("title", "HFDL Observer")}</li>
        <li class="forecast">{cls.format_forecast(topline.get("forecast"))}</li>
        <li class="uptime">{topline.get("uptime", "")}</li>
        <li class="jsonlink"><a href="display.json">JSON</a></li>
        </ul>
        """

    @classmethod
    def render(cls, source: ObserverDisplay, lower_pane: str = "") -> str:
        refresh = f'<meta http-equiv="refresh" content="{source.refresh_delay}">' if source.refresh_delay > 1 else ""
        state = source.current_state or {}
        head = f"""
<head>
<meta charset="UTF-8">
<title>HFDLObserver</title>
<style>{source.stylesheet_text}</style>
{refresh}
</head>
            """
        try:
            body = f"""
<body>
<div id="topline">{cls.render_topline(state.get("status"))}</div>
<div id="statusline"><ul class="statusline">{cls.format_statusline(state.get("totals"))}</ul></div>
<div id="counts">{cls.format_counts(state.get("counts"))}</div>
{lower_pane}
</body>
            """
        except KeyError:
            body = "<body><p>HFDLObserver is unavailable<p></body>"
        return f"<!doctype html><html>{head}{body}</html>"


class RecentAircraftHTMLFormatter:
    @classmethod
    def format_tooltip(cls, ac: aircraft.Aircraft) -> str:
        return f"""
<td class="tooltip_holder"><div class="tooltip">
    <div class="title"><b>Session</b>: {ac.session_hex}</div>
    <div class="body"><table class="extra">
        <tr><th>Flight</th><td>{ac.flight or UNKNOWN}</td></tr>
        <tr><th>Tail</th><td>{ac.r or UNKNOWN}</td></tr>
        <tr><th>ICAO</th><td>{ac.hex_id or UNKNOWN}</td></tr>
        <tr><th>Frequency</th><td>{ac.freq}</td></tr>
        <tr><th>RSSI</th><td>{ac.rssi:0.2f}</td></tr>
        <tr><th>Ground Station</th><td>{ac.gs or UNKNOWN}</td></tr>
        <tr><th>Received by</th><td>{ac.recv or UNKNOWN}</td></tr>
    </table></div>
</div></td>
"""

    @classmethod
    def format_aircraft_table_row(cls, ac: aircraft.Aircraft) -> str:
        columns = [
            # ('sess', ac.session_hex),
            ("acid", ac.best_effort_id),
            ("seen", ac.seen),
            ("rssi", f"{ac.rssi:0.2f}"),
            ("lat", f"{ac.lat:0.3f}" if ac.lat else UNKNOWN),
            ("lon", f"{ac.lon:0.3f}" if ac.lon else UNKNOWN),
            ("posage", ac.seen_pos if ac.seen_pos else UNKNOWN),
            ("head", f"{ac.calc_track:0.1f}" if ac.calc_track else UNKNOWN),
            ("r_dst", int(ac.r_dst) if ac.r_dst else UNKNOWN),
            ("r_dir", int(ac.r_dir) if ac.r_dir else UNKNOWN),
            ("num_msg", ac.messages),
            ("pktyp", f'<span class="{ac.type}">{ac.type.upper()}</span>' if ac.type else UNKNOWN),
        ]
        out = ["<tr class='has_tooltip'>", cls.format_tooltip(ac)]
        for klass, value in columns:
            out.append(f'<td class="{klass}">{value}</td>')
        out.append("</tr>")
        return "".join(out)

    @classmethod
    def format_aircraft_table(cls, all_aircraft: Sequence[aircraft.Aircraft]) -> str:
        ch = [
            "<th></th>",
            "<th>aircraft</th>",
            "<th>age</th>",
            "<th>rssi</th>",
            "<th>lat</th>",
            "<th>lon</th>",
            "<th>pos age</th>",
            "<th>head</th>",
            "<th>dist</th>",
            "<th>bearing</th>",
            "<th>count</th>",
            "<th>last</th>",
        ]
        out = [
            "<table class='data'>",
            "<thead>",
            f"<tr><td colspan='{len(ch)}' class='bar'><div>🛩️ Recent Aircraft</div>",
            "<div class='jsonlink'><a href='aircraft.json'>JSON</a></div></td></tr>",
            "<tr class='aircraft_header'>",
        ]
        out.extend(ch)
        out.extend(
            [
                "</tr></thead><tbody>",
            ]
        )
        for ac in all_aircraft:
            out.append(cls.format_aircraft_table_row(ac))
        out.append("</tbody></table>")
        return "\n".join(out)

    @classmethod
    def render(cls, source: ObserverDisplay) -> str:
        if not source.tracker:
            return ""
        out = []
        sorted_ac = sorted(source.tracker.tracked_aircraft, key=lambda e: e.seen)
        # sorted_ac = sorted(source.tracker.aircraft_by_session.values(), key=lambda e: e.seen)
        out.append(cls.format_aircraft_table(sorted_ac))
        return "\n".join(out)


class RecentMessagesHTMLFormatter:
    @classmethod
    def format_packet_table(cls, packets: Sequence[dict]) -> str:
        ch = [
            "<th>age</th>",
            "<th>freq</th>",
            "<th>ac</th>",
            "<th>leg</th>",
            "<th>type</th>",
            "<th>lat</th>",
            "<th>lon</th>",
            "<th>spdus</th>",
            "<th>search</th>",
            "<th>change</th>",
            "<th>hfdl off</th>",
            "<th>tx</th>",
            "<th>rx</th>",
        ]
        out = [
            "<table class='data'>",
            "<thead>",
            f"<tr><td colspan='{len(ch)}' class='bar'><div>📥 Recent Messages</div>",
            "<div class='jsonlink'><a href='messages.json'>JSON</a></div></td></tr>",
            "<tr class='messages_header'>",
        ]
        out.extend(ch)
        out.append("</tr></thead><tbody>")
        for pkt in packets:
            out.append(cls.format_packet_table_row(pkt))
        out.append("</tbody></table>")
        return "\n".join(out)

    @classmethod
    def format_packet_table_row(cls, pkt: dict) -> str:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        UNKNOWN = "-"
        perf = pkt.get("perf", {})
        columns = [
            ("when", int(now - pkt["ts"])),
            ("freq", pkt["freq"]),
            ("icao", pkt["r"] or pkt["icao"] or UNKNOWN),
            ("leg", pkt.get("leg", UNKNOWN)),
            ("type", pkt.get("type", UNKNOWN).replace(" data", "")),
            ("lat", f"{pkt['lat']:0.3f}" if pkt.get("lat") else UNKNOWN),
            ("lon", f"{pkt['lon']:0.3f}" if pkt.get("lon") else UNKNOWN),
            ("spdus", f"{perf.get('spdus_ok', UNKNOWN)}/{perf.get('spdus_missed', UNKNOWN)}"),
            ("search", f"{perf.get('freq_search_cur', UNKNOWN)},{perf.get('freq_search_prev', UNKNOWN)}"),
            ("change", perf.get("last_freq_change", UNKNOWN)),
            ("hfdl_off", f"{perf.get('hfdl_off_cur', UNKNOWN)},{perf.get('hfdl_off_prev', UNKNOWN)}"),
        ]
        okerr = {"tx": ("tx", UNKNOWN), "rx": ("rx", UNKNOWN)}
        if perf:
            for prefix in ["rx", "tx"]:
                status_counts: dict = collections.defaultdict(lambda: 0)
                for status in ["ok", "err"]:
                    counts: dict = collections.defaultdict(lambda: 0)
                    for rate in [300, 600, 1200, 1800]:
                        k = f"{prefix}_{status}_{rate}"
                        counts[k] += perf.get(k, 0)
                    tot = sum(counts.values())
                    status_counts[status] += tot
                okerr[prefix] = (prefix, f"{status_counts['ok']}/{status_counts['err']}")
        columns.extend(okerr.values())

        out = ["<tr>"]
        for klass, value in columns:
            out.append(f'<td class="{klass}">{value}</td>')
        out.append("</tr>")
        return "".join(out)

    @classmethod
    def render(cls, source: ObserverDisplay) -> str:
        packets = list(reversed(source.recent_packets))
        return cls.format_packet_table(packets)


class LowerPaneHTMLFormatter:
    @classmethod
    def render(self, source: ObserverDisplay) -> str:
        return f"""
<div class="hfdl_container">
    <div class="column" id="aircraft">{RecentAircraftHTMLFormatter.render(source)}</div>
    <div class="column" id="messages">{RecentMessagesHTMLFormatter.render(source)}</div>
</div>
        """


class NaiveHandler(http.server.BaseHTTPRequestHandler):
    source: ObserverDisplay | None = None
    url_handler: URLHandler

    def do_GET(self) -> None:
        logger.debug(f"GET {self.path}")
        try:
            handler: tuple[str, Callable] = self.url_handler.urls[self.path]
        except Exception:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"")
        else:
            mimetype, renderer = handler
            try:
                body = renderer(self.source).encode("utf-8")
            except Exception as err:
                logging.error(f"error on {self.path}:", exc_info=err)
                raise
            self.send_response(200)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    import logging
    import sys

    import hfdl_observer.hfdl as hfdl

    incoming = pathlib.Path(sys.argv[1]).read_text()
    state = json.loads(incoming)

    config = {"aircraft": {"latitude": 60, "longitude": -40}, "maptiler_key": "TEST_KEY"}
    observer_display = ObserverDisplay(config=config)
    observer_display.current_state = state
    tracker = observer_display.tracker
    assert tracker

    packets_raw = pathlib.Path(sys.argv[2]).read_text()
    for line in packets_raw.split("\n"):
        line = line.strip("\u0000")
        if not line:
            continue
        data = json.loads(line)
        packet = hfdl.HFDLPacketInfo(data)
        logging.warning(str(packet))
        tracker.update_session(packet)
        observer_display.on_hfdl(packet)

    # print(MainDisplayHTMLFormatter.render(observer_display, LowerPaneHTMLFormatter.render(observer_display)))
    print(observer_display.url_handler.urls["/globe.html"][1](observer_display))

    logging.warning(f"size of recent packets {len(observer_display.recent_packets)}")
    sorted_ac = [ac.asdict() for ac in sorted(tracker.aircraft_by_session.values(), key=lambda e: e.seen)]
    ac_json = json.dumps(sorted_ac)
    logging.warning(f"size of aircraft.json {len(ac_json)}")
    logging.warning(f"size of messages.json {len(json.dumps(list(reversed(observer_display.recent_packets))))}")
    JOIN = "\n\n"
    for k, ac in tracker.aircraft_by_session.items():
        assert k == ac.session_id, f"mismatch {k} != {ac.session_id}\n{JOIN.join(repr(p.packet) for p in ac.packets)}"
    num_sessions = len(tracker.aircraft_by_session.values())
    num_uniques = len(set(ac.session_id for ac in tracker.aircraft_by_session.values()))
    assert num_sessions == num_uniques
    assert len(sorted_ac) == len(tracker.aircraft_by_session)
