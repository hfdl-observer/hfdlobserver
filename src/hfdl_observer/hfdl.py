# hfdl_observer/hfdl.py
# copyright 2024 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
from __future__ import annotations

import datetime
import functools
import json
import logging

from typing import Any, Optional, Sequence

HFDL_CHANNEL_WIDTH: int = 2400  # hz
HFDL_FRAME_TIME = 32


logger = logging.getLogger()


@functools.cache
def path_split(path: str | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(path).split(".")) if isinstance(path, str) else path


def get_by_path(source: dict, path: str | tuple[str, ...] | list[str], default: Any = None) -> Any:
    _path = path_split(path) if isinstance(path, str) else path
    return _get_by_path(source, _path, default)


def _get_by_path(source: dict, path: tuple[str, ...] | list[str], default: Any) -> Any:
    car, *cdr = path
    try:
        node = source[car]
    except (KeyError, AttributeError, IndexError):
        return default
    if cdr and node:
        return _get_by_path(node, cdr, default)
    return node


class PathingWrapper:
    backing_dict: dict

    def __init__(self, backing: dict):
        self.backing_dict = backing

    def __getitem__(self, path: str | tuple[str, ...], default: Any = None) -> Any:
        return get_by_path(self.backing_dict, path, default)

    def get(self, path: str | tuple[str, ...], default: Any = None) -> Any:
        return get_by_path(self.backing_dict, path, default)

    def __bool__(self) -> bool:
        return True if self.backing_dict else False


class HFDLPacketInfo(PathingWrapper):
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
    received: float

    def __init__(self, packet: dict[str, Any]):
        # Not at all a full extraction of a packet.
        packet = packet.get("hfdl", packet)  # in case it's not unwrapped.
        super().__init__(backing=packet)
        self.packet = packet
        self.received = datetime.datetime.now().timestamp()
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

    def decode_pos(self, lat: dict | str | float, lon: dict | str | float) -> tuple[float, float] | None:
        def decode(p: dict | str | float) -> float:
            if isinstance(p, dict):
                direction = -1 if p.get("dir") in ["west", "south"] else 1
                return direction * float(p.get("deg", 0))
            return float(p)

        if lat is None and lon is None:
            return None
        return (decode(lat), decode(lon))

    @property
    def position(self) -> Optional[tuple[float, float]]:
        # position could be in several places...
        # all in "hfdl.lpdu.hfnpdu"
        # "pos.lat|lon"
        # "acars.arinc622.adsc.tags.<list>.basic_report.lat|lon"
        # "acars.arinc622.adsc.tags.<list>.alt_range_event.lat|lon"
        # "acars.arinc622.adsc.tags.<list>.fixed_projection.lat|lon"
        # "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.pos.data.lat_lon"
        # "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.alt_pos.pos.data.lat_lon"

        try:
            hfnpdu = self.hfnpdu
            if hfnpdu:
                pos = hfnpdu.get("pos")
                if pos:
                    return self.decode_pos(pos["lat"], pos["lon"])
                for tag in self.adsc_tags or []:
                    for parent in ["basic_report", "alt_range_event", "fixed_projection"]:
                        pos = tag.get(parent)
                        if pos:
                            decoded = self.decode_pos(pos["lat"], pos["lon"])
                            if decoded:
                                return decoded
                for p in [
                    "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.pos.data.lat_lon",
                    "acars.arinc622.cpdlc.atc_uplink_msg.atc_uplink_msg_element_id.data.alt_pos.pos.data.lat_lon",
                ]:
                    pos = hfnpdu[p]
                    if pos:
                        decoded = self.decode_pos(pos["lat"], pos["lon"])
                        if decoded:
                            return decoded
        except KeyError:
            return None
        return None

    @functools.cached_property
    def lpdu(self) -> PathingWrapper:
        out = self["lpdu"]
        return PathingWrapper(out or {})

    @functools.cached_property
    def spdu(self) -> PathingWrapper:
        out = self["spdu"]
        return PathingWrapper(out or {})

    @functools.cached_property
    def hfnpdu(self) -> PathingWrapper:
        out = self.lpdu["hfnpdu"]
        return PathingWrapper(out or {})

    @functools.cached_property
    def acars(self) -> PathingWrapper:
        out = self.hfnpdu["acars"]
        return PathingWrapper(out or {})

    @functools.cached_property
    def adsc_tags(self) -> Sequence[dict]:
        if self.acars:
            return self.acars["arinc622.adsc.tags"] or []
        return tuple()

    @functools.cached_property
    def icao(self) -> str | None:
        if not self.lpdu:
            return None
        hex_id: str | None = ""
        for key in ["ac_info.icao", "src.ac_info.icao", "dst.ac_info.icao"]:
            if hex_id := self.lpdu[key]:
                return str(hex_id)
        return None

    @functools.cached_property
    def flight(self) -> str | None:
        flight_id: str | None = self.first_adsc_tag("flight_id.flight_id")
        if flight_id:
            return flight_id.strip(". ")
        for key in ["flight_id", "acars.flight"]:
            flight_id = self.hfnpdu[key]
            if flight_id:
                return flight_id.strip(". ")
        return None

    @functools.cached_property
    def tail(self) -> str | None:
        acars = self.acars
        if acars:
            for key in [
                "reg",
                "miam.single_transfer.miam_core.data.aircraft_id",
                "miam.miam.single_transfer.miam_core.ack.aircraft_idarinc622.air_addr",
            ]:
                value: str | None = acars[key]
                if value:
                    return value.strip(" .")  # best guess
        return None

    @functools.cached_property
    def is_logoff(self) -> bool:
        type_name: str | None = self.lpdu["type.name"]
        return type_name == "Logoff request"

    @functools.cached_property
    def is_logon(self) -> bool:
        type_name: str | None = self.lpdu["type.name"]
        return type_name == "Logon confirm"

    @functools.cached_property
    def is_logon_resume(self) -> bool:
        type_name: str | None = self.lpdu["type.name"]
        return type_name == "Logon resume"

    @functools.cached_property
    def session_id(self) -> tuple[int, int, int] | None:
        # builds a tuple of GS, login ID (for the GS) to allow tracking of flights when the only information is
        # stateful. This has a slight disadvantage in the case if a login ID is reused quickly enough. This could
        # be mitigated by tracking the LOGON and LOGOFF messages, but there's no guarantee those will be received.
        major = self.dst if self.is_downlink else self.src
        if self.is_logon:
            ac_id = self.lpdu["assigned_ac_id"]
        else:
            minor = self.src if self.is_downlink else self.dst
            ac_id = minor.get("id")
        if major and ac_id is not None:
            login: tuple[int, int, int] = (int(major["id"]), self.frequency, int(ac_id))
            if None not in login:
                return login
        return None

    @functools.cached_property
    def session_hex(self) -> str:
        if self.session_id:
            gs, fq, sl = [hex(e)[2:] for e in self.session_id]
            return f"({gs.zfill(2)}{fq.zfill(4)}{sl.zfill(2)})"
        return ""

    @functools.cached_property
    def structural_type(self) -> str:
        if self.spdu:
            return "spdu"
        hfnpdu = self.hfnpdu
        if hfnpdu:
            for structural_kind, name in [
                ("acars.arinc622.cpdlc", "cpdlc"),
                ("acars.arinc622.adsc", "adsc"),
                ("acars.miam", "miam"),
                ("acars", "acars"),
                ("pdu_stats", "perf"),
            ]:
                if hfnpdu[structural_kind]:
                    return name
            return "hfnpdu"
        return "lpdu"

    @functools.cached_property
    def best_effort_id(self) -> str | None:
        possibilities = [e for e in [self.flight, self.tail, self.icao] if e]
        return possibilities[0] if possibilities else None

    def first_adsc_tag(self, path: str | tuple[str, ...]) -> Any:
        for tag in self.adsc_tags:
            if (val := get_by_path(tag, path)) is not None:
                return val
        return None

    def simplified_dict(self) -> dict | None:
        if not (lpdu := self.lpdu):
            return None

        out: dict = {
            "received": self.received,
            "ts": self.timestamp,
            "station": self.station,
            "freq": self.frequency,
            "cpdlc": "",  # for now, I'm not sure what they actually want here.
            "icao": self.icao,
            "r": self.tail,
        }
        if self.position:
            out["lat"] = self.position[0]
            out["lon"] = self.position[1]
        if self.ground_station:
            out["gs"] = self.ground_station.get("name", str(self.ground_station["id"]))

        out["perf"] = perf = {}
        out["callsign"] = self.best_effort_id
        hfnpdu = self.hfnpdu
        if hfnpdu:
            out["type"] = hfnpdu["type.name"]
            stats = hfnpdu["pdu_stats"]
            if stats:
                for src, dst in [
                    ("mpdus_delivered_cnt", "tx_"),
                    ("mpdus_rx_err_cnt", "rx_err_"),
                    ("mpdus_rx_ok_cnt", "rx_ok_"),
                    ("mpdus_tx_err_cnt", "tx_err_"),
                    ("mpdus_tx_ok_cnt", "tx_ok_"),
                ]:
                    if mpdus := stats.get(src):
                        for bps in [300, 600, 1200, 1800]:
                            if (value := mpdus.get(f"{bps}bps")) is not None:
                                perf[f"{dst}{bps}"] = value
                if (spdus_missed := stats.get("spdus_missed_cnt")) is not None:
                    perf["spdus_missed"] = spdus_missed
                if (spdus_ok := stats.get("spdus_rx_ok_cnt")) is not None:
                    perf["spdus_ok"] = spdus_ok
            if active_freq := hfnpdu["frequency.freq"]:
                perf["active_freq"] = active_freq
            if (hfdl_off_cur := hfnpdu["hfdl_disabled_duration.cur_leg"]) is not None:
                perf["hdfl_off_cur"] = hfdl_off_cur
            if (hfdl_off_prev := hfnpdu["hfdl_disabled_duration.prev_leg"]) is not None:
                perf["hdfl_off_prev"] = hfdl_off_prev
            if leg := hfnpdu["flight_leg_num"]:
                perf["flight_leg"] = leg
            if (search := hfnpdu["freq_search_cnt"]) is not None:
                perf["freq_search_cur"] = search.get("cur_leg", 0)
                perf["freq_search_prev"] = search.get("prev_leg", 0)
            if lfc := hfnpdu["last_freq_change_cause.descr"]:
                perf["last_freq_change"] = lfc
        else:
            out["type"] = lpdu["type.name"]
        return out

    def __str__(self) -> str:
        # direction = "FROM" if self.is_uplink else "TO"
        direction = "◀▬" if self.is_uplink else "▬▶"
        if self.packet:
            sub = self.structural_type
            # subtype = "spdu" if self.packet.get("spdu") else ("lpdu" if self.packet.get("lpdu") else "other")
        else:
            sub = "unknown"
        station = self.station or ""
        gs = self.ground_station.get("name", None)
        if gs is None:
            gs = self.ground_station.get("name")
            if not gs:
                gs_id = self.ground_station.get("id")
                gs = f"#{gs_id}" if gs_id else "unknown"
        gs = gs.split(",", 1)[0]
        _id = f" {self.best_effort_id}" if self.best_effort_id else ""
        if not _id:
            if self.session_id:
                _id = self.session_hex
            else:
                direction = "FROM" if self.is_uplink else "TO"
        return f"<HFDL/{sub} {station}@{self.timestamp} {self.frequency}kHz ({self.snr:.1f}dB){_id} {direction} {gs}>"

    @classmethod
    def from_raw(cls, raw_packet: str) -> HFDLPacketInfo | None:
        line = raw_packet.strip()
        if not line.startswith("{"):
            logger.debug(f"dropping garbage: {line}")
            return None
        try:
            packet_data = json.loads(line)
        except json.JSONDecodeError as err:
            logger.warn(f"dropping garbage: {line}", exc_info=err)
        else:
            packet = cls(packet_data)
            logger.info(f"packet {packet}")
            return packet
        return None
