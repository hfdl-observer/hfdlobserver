# noui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
# flake8: noqa [W503]
from __future__ import annotations

import datetime
import logging

from typing import Any, Optional, Sequence

import hfdl_observer.baseui as baseui
import hfdl_observer.bus as bus
import hfdl_observer.data as data
import hfdl_observer.heatmapui as heatmapui
import hfdl_observer.network as network
import hfdl_observer.settings as settings
import hfdl_observer.util as util

import hfdlobserver
import webui


class NoDisplay(baseui.PrimaryDisplay):
    current_width = 256  # Not sure what to do for a static value

    def update_heatmap(self, heatmap_data: Sequence) -> None:
        for secondary in self.secondary_displays:
            secondary.update()


class HeatMap(heatmapui.HeatMap):
    def render_empty_map(self, head: str, width: int) -> Sequence:
        return ["no data"]

    def render_data_rows(self, source: heatmapui.AbstractHeatMapFormatter, cells_visible: int) -> Sequence[Sequence]:
        return []

    def render_column_headers(
        self, source: heatmapui.AbstractHeatMapFormatter, cells_visible: int, bin_str: str
    ) -> Sequence:
        return []


def create_secondary(config: dict) -> baseui.SecondaryObserverDisplay | None:
    SECONDARY_TYPES = {
        "web": webui.ObserverDisplay,
    }
    if not (klass := SECONDARY_TYPES.get(config["type"])):
        logging.warning(f"{config['type']} is not a valid Secondary Display; ignoring.")
        return None
    return klass(config=config)


def launch(handler: None | logging.Handler, debug: bool = True, quiet: bool = False, is_node: bool = False) -> None:
    hfdlobserver.setup_logging(handler, debug, quiet)

    cui_settings = settings.cui
    secondaries = []
    for entry in cui_settings.get("secondary_displays", []):
        if secondary := create_secondary(entry):
            secondaries.append(secondary)

    if secondaries:
        # Only set up a HeatMap and rendering loop if there's a secondary to render for.
        heatmap = HeatMap(config=cui_settings["ticker"])
        cumulative_line = baseui.CumulativeLine()
        forecaster = bus.RemoteURLRefresher(url="https://services.swpc.noaa.gov/products/noaa-scales.json", period=617)

        display = NoDisplay(heatmap, cumulative_line, forecaster)
        for secondary in secondaries:
            display.add_secondary(secondary)

        def observing(
            observer: hfdlobserver.HFDLObserverController,
            cumulative: network.CumulativePacketStats,
        ) -> None:
            heatmap.register(observer)
            cumulative_line.register(observer, cumulative)
            util.schedule(forecaster.run())
            for secondary in secondaries:
                secondary.register(observer)

    else:

        def observing(
            observer: hfdlobserver.HFDLObserverController,
            cumulative: network.CumulativePacketStats,
        ) -> None:
            pass

    hfdlobserver.observe(on_observer=observing, as_controller=not is_node)


if __name__ == "__main__":
    launch(None)
