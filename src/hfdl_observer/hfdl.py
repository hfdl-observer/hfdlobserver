# hfdl_observer/hfdl.py
# copyright 2024 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import datetime
import logging
from typing import Any, Optional, Union

HFDL_CHANNEL_WIDTH: int = 2400  # hz
HFDL_FRAME_TIME = 32


logger = logging.getLogger()


class HFDLPacketInfo:
    packet: dict[str, Any]
    timestamp: int
    frequency: int
    ground_station: dict[str, Any]
    station: Optional[str]
    bitrate: Optional[int]
    skew: Optional[float]
    frame_slot: Optional[int]
    snr: float
    src: dict[str, Any]
    dst: dict[str, Any]

    def __init__(self, packet: dict[str, Any]):
        # Not at all a full extraction of a packet.
        packet = packet.get("hfdl", packet)  # in case it's not unwrapped.
        self.packet = packet
        self.timestamp = packet["t"]["sec"]
        self.frequency = packet["freq"] // 1000
        self.bitrate = packet.get("bitrate")
        self.skew = packet.get("freq_skew")
        self.frame_slot = packet.get("slot")
        self.snr = packet["sig_level"] - packet["noise_level"]
        app_data = packet.get("spdu", packet.get("lpdu", {}))
        self.src = app_data.get("src", {})
        self.dst = app_data.get("dst", {})
        self.station = packet.get("station")
        if self.is_downlink:
            self.ground_station = self.dst
        elif self.is_uplink:
            self.ground_station = self.src
        else:
            self.ground_station = {}

    @property
    def is_uplink(self) -> bool:
        return self.src.get("type") == "Ground station"

    @property
    def is_downlink(self) -> bool:
        return self.dst.get("type") == "Ground station"

    @property
    def is_squitter(self) -> bool:
        return True if self.packet.get("spdu") else False

    @property
    def when(self) -> datetime.datetime:
        return datetime.datetime.utcfromtimestamp(self.timestamp)

    cpdlc_pos = "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.pos.data.lat_lon".split(".")
    cpdlc_alt = "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.alt_pos.pos.data.lat_lon".split(".")

    def get(self, path: Union[str, list[str]], data: Optional[dict] = None, default: Any = None) -> Optional[Any]:
        data = self.packet if data is None else data
        path = str(path).split(".") if "." in path else (path if isinstance(path, list) else [str(path)])
        car, *cdr = path
        try:
            node = data[car]
        except (KeyError, AttributeError, IndexError):
            return default
        if cdr:
            return self.get(cdr, data=node, default=default)
        return node

    def decode_pos(self, lat: dict | str | float, lon: dict | str | float) -> tuple[float, float]:
        def decode(p: dict | str | float) -> float:
            if isinstance(p, dict):
                direction = -1 if p.get("dir") in ["west", "south"] else 1
                return direction * float(p.get("deg", 0))
            return float(p)

        return (decode(lat), decode(lon))

    @property
    def position(self) -> Optional[tuple[float, float]]:
        # position could be in several places...
        # all in "hfdl.lpdu.hfnpdu"
        # "pos"
        # "acars.arinc622.adsc.tags.<list>.basic_report"
        # "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.pos.data.lat_lon"
        # "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.alt_pos.pos.data.lat_lon"

        try:
            hfnpdu = self.packet.get("lpdu", {}).get("hfnpdu")
            if hfnpdu:
                pos = hfnpdu.get("pos")
                if pos:
                    return self.decode_pos(pos["lat"], pos["lon"])
                for tag in self.get(["acars", "arinc622", "adsc", "tags"], hfnpdu) or []:
                    pos = tag.get("basic_report")
                    if pos:
                        return self.decode_pos(pos["lat"], pos["lon"])
                for p in [self.cpdlc_pos, self.cpdlc_alt]:
                    pos = self.get(p, hfnpdu)
                    if pos:
                        return self.decode_pos(pos["lat"], pos["lon"])
        except KeyError:
            pass

        return None

    def __str__(self) -> str:
        direction = "FROM" if self.is_uplink else "TO"
        if self.packet:
            subtype = "spdu" if self.packet.get("spdu") else ("lpdu" if self.packet.get("lpdu") else "other")
        else:
            subtype = "unknown"
        station = self.station or ""
        gs = self.ground_station.get("name", None)
        if gs is None:
            gs = self.ground_station.get("name")
            if not gs:
                gs_id = self.ground_station.get("id")
                gs = f"#{gs_id}" if gs_id else "unknown"
        gs = gs.split(",", 1)[0]
        return f"<HFDL/{subtype} {station}@{self.timestamp} {self.frequency}kHz ({self.snr:.1f}dB) {direction} {gs}>"
