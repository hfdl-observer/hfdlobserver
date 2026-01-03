# hfdl_observer/baseui.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import functools
import logging

from typing import Any, Optional, Sequence

import hfdlobserver
import hfdl_observer.heatmapui as heatmapui
import hfdl_observer.network as network
import hfdl_observer.util as util

logger = logging.getLogger(__name__)


class BaseObserverDisplay:
    heatmap: heatmapui.HeatMap
    keyboard: util.Keyboard

    def keyboard_help(self) -> str:
        parts = [
            "Keyboard Commands",
            "[,] - previous display mode",
            "[.] - next display mode",
            "[-] - bin size -60s",
            "[+] - bin size +60s",
        ]
        for k, m in enumerate(self.heatmap.all_modes.keys()):
            parts.append(f"[{k + 1}] - {m} display")
        parts.append("[h] - this help")
        return "\n".join(parts)

    def setup_keyboard(self, keyboard: util.Keyboard) -> util.Keyboard:
        keyboard.add_mapping(".", self.next_heatmap_mode)
        keyboard.add_mapping(",", self.previous_heatmap_mode)
        keyboard.add_mapping("-", self.smaller_bins)
        keyboard.add_mapping("+", self.larger_bins)
        keyboard.add_mapping("_", self.smaller_bins)
        keyboard.add_mapping("=", self.larger_bins)
        keyboard.add_mapping("q", exit)
        keyboard.add_mapping("Q", exit)
        keyboard.add_mapping("h", lambda _: logger.info(self.keyboard_help()))
        keyboard.add_mapping("H", lambda _: logger.info(self.keyboard_help()))
        keyboard.add_mapping("w", self.toggle_flexible_width)
        keyboard.add_mapping("W", self.toggle_flexible_width)
        for k, m in enumerate(self.heatmap.all_modes.keys()):
            keyboard.add_mapping(str(k + 1), functools.partial(self.heatmap.select_display_mode, m))
        return keyboard

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

    def update_totals(self, cumulative: network.CumulativePacketStats) -> None:
        raise NotImplementedError()


class CumulativeLine:
    display: BaseObserverDisplay
    target_observed: Optional[int] = None
    bonus_observed: Optional[int] = None
    active: Optional[int] = None
    cumulative: network.CumulativePacketStats = network.CumulativePacketStats()

    def register(self, observer: hfdlobserver.HFDLObserverController, totals: network.CumulativePacketStats) -> None:
        self.cumulative = totals
        totals.watch_event("update", self.on_update)
        observer.watch_event("active", self.on_active)
        observer.watch_event("observing", self.on_observing)

    def on_update(self, _: Any) -> None:
        if self.display:
            self.display.update_totals(self.cumulative)

    def on_observing(self, observed: tuple[Sequence[int], Sequence[int]]) -> None:
        targetted, untargetted = observed
        self.target_observed = len(targetted)
        self.bonus_observed = len(untargetted)

    def on_active(self, active_frequencies: Sequence[int]) -> None:
        self.active = len(active_frequencies)
