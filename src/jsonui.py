# jsonui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

from __future__ import annotations

import collections
import datetime
import logging

from typing import Sequence

import hfdl_observer.aircraft as aircraft
import hfdl_observer.baseui as baseui
import hfdl_observer.bus as bus
import hfdl_observer.heatmapui as heatmapui
import hfdl_observer.hfdl as hfdl
import hfdl_observer.network as network
import hfdl_observer.util as util

logger = logging.getLogger(__name__)
start = datetime.datetime.now()


class ObserverDisplay(baseui.SecondaryObserverDisplay):
    status: None | dict = None
    totals: None | dict = None
    counts: None | dict = None
    forecast: dict
    uptime_text: str
    day_count: int | None = None
    week_count: int | None = None
    spark_data: Sequence[int] | None = None
    current_state: dict
    tracker: aircraft.AircraftTracker | None = None
    recent_packets: collections.deque[dict]
    horizon: int = 3600

    def __init__(self, config: dict) -> None:
        ac_config = config.get("aircraft")
        if ac_config:
            self.tracker = aircraft.AircraftTracker(ac_config)
        self.recent_packets = collections.deque()
        self.uptime_text = "STARTING"
        self.current_state = {}
        self.forecast = {}
        self.setup_status()
        self.totals = {}
        self.setup_totals()
        self.update_status()
        self.horizon = config.get("horizon", 3600)

    def update(self) -> None:
        self.update_status()
        out = {
            "status": self.status,
            "totals": self.totals if self.totals else None,
            "counts": self.counts,
        }
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

    def preen_packets(self) -> None:
        horizon = datetime.datetime.now().timestamp() - self.horizon
        while self.recent_packets and self.recent_packets[0].get("ts", 0) < horizon:
            self.recent_packets.popleft()

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

    def register(self, observer: bus.EventNotifier) -> None:
        if self.tracker:
            self.tracker.register(observer)
        observer.watch_event("packet", self.on_hfdl)

    def on_hfdl(self, packet: hfdl.HFDLPacketInfo) -> None:
        self.preen_packets()
        p = packet.simplified_dict()
        if p:
            self.recent_packets.append(p)
