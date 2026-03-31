# webui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

from __future__ import annotations

import collections
import datetime
import json
import http.server
import logging
import pathlib
import threading

from typing import Sequence

import hfdl_observer.aircraft as aircraft
import hfdl_observer.env as env
import jsonui

logger = logging.getLogger(__name__)
start = datetime.datetime.now()
UNKNOWN = "-"


class ObserverDisplay(jsonui.ObserverDisplay):
    server: http.server.ThreadingHTTPServer | None = None

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if config.get("address"):
            self.run_server(config)

    def run_server(self, config: dict) -> None:
        if not self.server:
            # this = self
            stylesheet_name = config.get("stylesheet_path")
            if stylesheet_name:
                display_stylesheet_path = env.as_path(stylesheet_name)
            else:
                display_stylesheet_path = pathlib.Path(__file__).parent.parent / "default.css"

            class Handler(NaiveHandler):
                source = self
                refresh_delay: int = int(config.get("refresh", 16))
                stylesheet_path: pathlib.Path = display_stylesheet_path

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
    stylesheet_path: pathlib.Path

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
        spans.append('</th><!--th class="fakeheader"--></th>')
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
        return f"""<!--th class="fakefooter"></th--><th class="rowfooter">{total["value"]}</th>"""

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
        css_file = self.stylesheet_path
        if not css_file:
            css_file = pathlib.Path(__file__).parent.parent / "default.css"
        text: str = css_file.read_text()
        return text

    def render_topline(self, topline: dict) -> str:
        return f"""
        <ul class="topline">
        <li class="icon">{topline["icon"]}</li>
        <li class="title">{topline["title"]}</li>
        <li class="forecast">{self.format_forecast(topline["forecast"])}</li>
        <li class="uptime">{topline["uptime"]}</li>
        <li class="jsonlink"><a href="display.json">JSON</a></li>
        </ul>
        """

    def render_display(self, source: ObserverDisplay) -> str:
        state = source.current_state
        status = state["totals"]
        statusline = f"""<ul class="statusline">{self.format_statusline(status)}</ul>"""
        refresh = f'<meta http-equiv="refresh" content="{self.refresh_delay}">' if self.refresh_delay > 1 else ""
        head = f"""
<head>
<meta charset="UTF-8">
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
        return f"<!doctype html><html>{head}{body}</html>"

    def format_tooltip(self, ac: aircraft.Aircraft) -> str:
        return f'''
<td class="tooltip_holder"><div class="tooltip">
    <div class="title"><b>Session</b>: {ac.session_hex}</div>
    <div class="body"><table class="extra">
        <tr><th>Flight</th><td>{ac.flight or UNKNOWN}</td></tr>
        <tr><th>Tail</th><td>{ac.r or UNKNOWN}</td></tr>
        <tr><th>ICAO</th><td>{ac.hex_id or UNKNOWN}</td></tr>
        <tr><th>Frequency</th><td>{ac.session_id[1]}</td></tr>
        <tr><th>RSSI</th><td>{ac.rssi:0.2f}</td></tr>
    </table></div>
</div></td>
'''

    def format_aircraft_table_row(self, ac: aircraft.Aircraft) -> str:
        columns = [
            # ('sess', ac.session_hex),
            ('acid', ac.best_effort_id),
            ('seen', ac.seen),
            ('rssi', f"{ac.rssi:0.2f}"),
            ('lat', f"{ac.lat:0.3f}" if ac.lat else UNKNOWN),
            ('lon', f"{ac.lon:0.3f}" if ac.lon else UNKNOWN),
            ('posage', ac.seen_pos if ac.seen_pos else UNKNOWN),
            ('head', f"{ac.calc_track:0.1f}" if ac.calc_track else UNKNOWN),
            ('r_dst', int(ac.r_dst) if ac.r_dst else UNKNOWN),
            ('r_dir', int(ac.r_dir) if ac.r_dir else UNKNOWN),
            ('num_msg', ac.messages),
            ('pktyp', f'<span class="{ac.type}">{ac.type.upper()}</span>' if ac.type else UNKNOWN),
        ]
        out = [
            "<tr class='has_tooltip'>",
            self.format_tooltip(ac)
        ]
        for klass, value in columns:
            out.append(f'<td class="{klass}">{value}</td>')
        out.append("</tr>")
        return "".join(out)

    def format_aircraft_table(self, all_aircraft: Sequence[aircraft.Aircraft]) -> str:
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
        out.extend([
            "</tr></thead><tbody>",
        ])
        for ac in all_aircraft:
            out.append(self.format_aircraft_table_row(ac))
        out.append("</tbody></table>")
        return "\n".join(out)

    def render_aircraft(self, source: ObserverDisplay) -> str:
        if not source.tracker:
            return ""
        out = []
        sorted_ac = sorted(source.tracker.aircraft_by_session.values(), key=lambda e: e.seen)
        out.append(self.format_aircraft_table(sorted_ac))
        return "\n".join(out)

    def format_packet_table(self, packets: Sequence[dict]) -> str:
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
            out.append(self.format_packet_table_row(pkt))
        out.append("</tbody></table>")
        return "\n".join(out)

    def format_packet_table_row(self, pkt: dict) -> str:
        now = datetime.datetime.now(datetime.UTC).timestamp()
        UNKNOWN = "-"
        perf = pkt.get("perf", {})
        columns = [
            ('when', int(now - pkt["ts"])),
            ('freq', pkt["freq"]),
            ('icao', pkt["r"] or pkt["icao"] or UNKNOWN),
            ('leg', pkt.get("leg", UNKNOWN)),
            ('type', pkt.get("type", UNKNOWN).replace(" data", "")),
            ('lat', f"{pkt['lat']:0.3f}" if pkt.get("lat") else UNKNOWN),
            ('lon', f"{pkt['lon']:0.3f}" if pkt.get("lon") else UNKNOWN),
            ('spdus', f"{perf.get('spdus_ok', UNKNOWN)}/{perf.get('spdus_missed', UNKNOWN)}"),
            ('search', f"{perf.get('freq_search_cur', UNKNOWN)},{perf.get('freq_search_prev', UNKNOWN)}"),
            ('change', perf.get("last_freq_change", UNKNOWN)),
            ('hfdl_off', f"{perf.get('hfdl_off_cur', UNKNOWN)},{perf.get('hfdl_off_prev', UNKNOWN)}"),
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

    def render_messages(self, source: ObserverDisplay) -> str:
        packets = list(reversed(source.recent_packets))
        return self.format_packet_table(packets)

    def render_all(self, source: ObserverDisplay) -> str:
        state = source.current_state
        status = state["totals"]
        statusline = f"""<ul class="statusline">{self.format_statusline(status)}</ul>"""
        refresh = f'<meta http-equiv="refresh" content="{self.refresh_delay}">' if self.refresh_delay > 1 else ""
        head = f"""
<head>
<meta charset="UTF-8">
<title>HFDLObserver</title>
<style>{self.styles()}</style>
{refresh}
</head>
        """
        body = f"""
<body>
<div id="topline">{self.render_topline(state["status"])}</div>
<div id="statusline" class="bar">{statusline}</div>
<div id="counts">{self.format_counts(state["counts"])}</div>
<div class="hfdl_container">
    <div class="column" id="aircraft">{self.render_aircraft(source)}</div>
    <div class="column" id="messages">{self.render_messages(source)}</div>
</div>
</body>
        """
        return f"<!doctype html><html>{head}{body}</html>"


class NaiveHandler(http.server.BaseHTTPRequestHandler, HTMLFormatter):
    source: ObserverDisplay | None = None

    def do_GET(self) -> None:
        url_lookup = {
            "/display.json": ("application/json", self.get_display_json_response),
            "/display.html": ("text/html; charset=utf-8", self.get_display_html_response),
            "/": ("text/html; charset=utf-8", self.get_all_html_response),
            "/full.html": ("text/html; charset=utf-8", self.get_all_html_response),
            "/aircraft.json": ("application/json", self.get_aircraft_json_response),
            "/messages.json": ("application/json", self.get_messages_json_response),
        }
        logger.debug(f"GET {self.path}")
        try:
            handler = url_lookup[self.path]
        except Exception:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"")
        else:
            try:
                body = handler[1]().encode("utf-8")
            except Exception as err:
                logging.error(f"error on {self.path}:", exc_info=err)
                raise
            self.send_response(200)
            self.send_header("Content-Type", handler[0])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def get_display_json_response(self) -> str:
        return json.dumps(self.source.current_state) if self.source else ""

    def get_display_html_response(self) -> str:
        return self.render_display(self.source) if self.source else ""

    def get_all_html_response(self) -> str:
        return self.render_all(self.source) if self.source else ""

    def get_messages_json_response(self) -> str:
        return json.dumps(list(reversed(self.source.recent_packets))) if self.source else ""

    def get_aircraft_json_response(self) -> str:
        if self.source and self.source.tracker:
            sorted_ac = [
                ac.asdict() for ac in sorted(self.source.tracker.aircraft_by_session.values(), key=lambda e: e.seen)
            ]
            return json.dumps(sorted_ac)
        else:
            return ""


if __name__ == "__main__":
    import logging
    import sys

    import hfdl_observer.hfdl as hfdl

    incoming = pathlib.Path(sys.argv[1]).read_text()
    state = json.loads(incoming)

    tracker = aircraft.AircraftTracker({"latitude": 60, "longitude": -40})
    fake = ObserverDisplay({"latitude": 60, "longitude": -40})
    fake.current_state = state
    fake.tracker = tracker

    packets_raw = pathlib.Path(sys.argv[2]).read_text()
    for line in packets_raw.split('\n'):
        line = line.strip('\u0000')
        if not line:
            continue
        data = json.loads(line)
        packet = hfdl.HFDLPacketInfo(data)
        logging.warning(str(packet))
        tracker.update_session(packet)
        fake.on_hfdl(packet)

    formatter = HTMLFormatter()
    formatter.stylesheet_path = pathlib.Path(__file__).parent.parent / "default.css"
    print(formatter.render_all(fake))

    logging.warning(f"size of recent packets {len(fake.recent_packets)}")
    sorted_ac = [ac.asdict() for ac in sorted(tracker.aircraft_by_session.values(), key=lambda e: e.seen)]
    ac_json = json.dumps(sorted_ac)
    logging.warning(f"size of aircraft.json {len(ac_json)}")
    logging.warning(f"size of messages.json {len(json.dumps(list(reversed(fake.recent_packets))))}")
    for k, ac in tracker.aircraft_by_session.items():
        assert k == ac.session_id, f"mismatch {k} != {ac.session_id}\n{'\n\n'.join(repr(p.packet) for p in ac.packets)}"
    num_sessions = len(tracker.aircraft_by_session.values())
    num_uniques = len(set(ac.session_id for ac in tracker.aircraft_by_session.values()))
    assert num_sessions == num_uniques
    assert len(sorted_ac) == len(tracker.aircraft_by_session)
