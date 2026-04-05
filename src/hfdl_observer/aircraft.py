# hfdl_observer/aircraft.py
# copyright 2026 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

from __future__ import annotations

import dataclasses
import datetime
import functools
import itertools
import logging

from typing import Sequence

import hfdl_observer.bus as bus
import hfdl_observer.hfdl as hfdl
import hfdl_observer.util as util


@dataclasses.dataclass
class Aircraft:
    session_id: tuple[int, int, int]  # gs_id, freq(kHz), gs_slot
    # represents a very minimal readsb-like aircraft object.
    # hex_id: ICAO number, unless prefixed with '~' where it represents an alternate identifier (tail or flight number)
    # this object does not support the "~" usage.
    hex_id: str | None = None
    type: str = "unknown"
    flight: str | None = None
    r: str | None = None  # tail number/registration
    # calc_track. track over ground (deg). We use true track or magnetic heading.
    calc_track: None | int = None
    lat: float | None = None
    lon: float | None = None
    r_dst: float | None = None
    r_dir: float | None = None
    # number of messages recorded. This may extend beyond the retention horizon
    messages: int = 0
    # rssi: received strength (dBFS)
    rssi: float | None = None
    # t: aircraft type. requires registration db or lucky parsing of embedded acars.
    t: str | None = None  # not supported
    # nic: Navigation Integrity Category
    # nic: int | None = None  # not supported
    # rc: Radius of Containment, meters
    # rc: int | None = None  # not supported
    # mlat: fields from MLAT
    # mlat: list | tuple = tuple()  # not supported
    # tisb: fields from TIS-B
    # tisb: list | tuple = tuple()  # not supported
    # squawk: 4 octal digit representation of Mode A squawk
    # squawk: str | None = None  # not supported
    # calculated:
    # seen: how many seconds ago the last received messages was
    # seen_pos: how many seconds ago the last message with position information was received
    # extras:
    # _seen_ts: timestamp of the last received message
    _seen_ts: float = 0  # underlying timestamp
    # _seen_pos_ts: timestamp of the last message with position information
    _seen_pos_ts: float | None = None
    # freq: frequency of the last packet
    freq: int = 0
    # gs: ground station of last packet
    gs: str | None = None
    recv: str | None = None

    def update(self, packet: hfdl.HFDLPacketInfo, home_lat: float | None, home_lon: float | None) -> Aircraft:
        self.packets.append(packet)
        self.messages += 1
        self.freq = packet.frequency
        self.gs = packet.ground_station["name"]
        self.rssi = packet["sig_level"]
        self.recv = packet.station
        seen = packet.timestamp
        self.hex_id = packet.icao
        self._seen_ts = seen
        position = packet.position
        if (
            position
            and (position[0] != position[1] or position[0])  # not 0, 0
            and -91 < position[0] < 91  # not 180, 180
            and -181 < position[1] < 181
        ):
            self._seen_pos_ts = seen
            self.lat, self.lon = position

        kind = packet.acars["arinc622.msg_type"]
        if kind:
            self.type = kind
        else:
            self.type = packet.structural_type

        self.flight = packet.flight or self.flight

        adsc_tags = packet.adsc_tags

        valid_track = False
        if adsc_tags:
            # "hfdl.lpdu.hfnpdu.acars.arinc622.adsc.tags.<list>.flight_id.flight_id"
            self.type = "adsc"
            for valid_key, hdg_key in [
                ("earth_ref_data.true_trk_valid", "earth_ref_data.true_trk_deg"),
                ("intermediate_projection.true_trk_valid", "intermediate_projection.true_trk_deg"),
                ("air_ref_data.true_hdg_valid", "air_ref_data.true_hdg_deg"),
            ]:
                if packet.first_adsc_tag(valid_key) and (track := packet.first_adsc_tag(hdg_key)) is not None:
                    self.calc_track = track
                    valid_track = True
                    break
        if position and not valid_track:
            self.calc_track = None

        tail = packet.tail
        if tail:
            self.r = tail

        if all(x is not None for x in [self.lat, self.lon, home_lat, home_lon]):
            # "or 0" silliness is for mypy. The above line will ensure it's not None, but mypy doesn't understand that.
            self.r_dst = int(util.distance((home_lat or 0, home_lon or 0), (self.lat or 0, self.lon or 0)))
            self.r_dir = int(util.bearing((home_lat or 0, home_lon or 0), (self.lat or 0, self.lon or 0)))

        return self

    @property
    def best_effort_id(self) -> str:
        possibilities = [self.flight, self.r, self.hex_id, "unknown"]
        return next(e for e in possibilities if e)

    @functools.cached_property
    def packets(self) -> list:
        return []

    @property
    def seen(self) -> int:
        return int(datetime.datetime.now().timestamp() - self._seen_ts)

    @property
    def seen_pos(self) -> int | None:
        return int(datetime.datetime.now().timestamp() - self._seen_pos_ts) if self._seen_pos_ts else None

    @property
    def session_hex(self) -> str:
        if self.session_id == (-1, -1, -1):
            return "00000000"
        gs, fq, sl = [hex(e)[2:] for e in self.session_id]
        return f"{gs.zfill(2)}{fq.zfill(4)}{sl.zfill(2)}"

    def asdict(self) -> dict:
        out = dataclasses.asdict(self)
        out["seen"] = self.seen
        out["seen_pos"] = self.seen_pos
        return out


class AircraftTracker:
    aircraft_by_session: dict[tuple[int, int, int], Aircraft]
    aircraft_by_icao: dict[str, Aircraft]
    aircraft_by_tail: dict[str, Aircraft]
    aircraft_by_flight: dict[str, Aircraft]
    home_lat: float | None = None
    home_lon: float | None = None
    horizon: int = 3600
    gate: int = 256  # number of aircraft objects before preening starts

    def __init__(self, config: dict) -> None:
        self.aircraft_by_session = {}
        self.aircraft_by_icao = {}
        self.aircraft_by_tail = {}
        self.aircraft_by_flight = {}
        self.home_lat = config.get("latitude")
        self.home_lon = config.get("longitude")
        self.horizon = config.get("horizon", 3600)
        self.gate = config.get("max_tracked", 1024)

    def register(self, observer: bus.EventNotifier) -> None:
        logging.info("registering aircraft tracker")
        observer.watch_event("packet", self.on_hfdl)

    def preen_aircraft(self) -> None:
        all_d: list[dict] = [
            self.aircraft_by_session,
            self.aircraft_by_tail,
            self.aircraft_by_icao,
            self.aircraft_by_flight,
        ]
        for d in all_d:
            if len(d) > self.gate:
                outdated_session_ids = [k for k, ac in list(d.items()) if ac.seen > self.horizon]
                for sid in outdated_session_ids:
                    del d[sid]

    def purge_session(self, session_id: tuple[int, int, int]) -> None:
        if session_id in self.aircraft_by_session:
            del self.aircraft_by_session[session_id]

    def aircraft_from_packet(self, packet: hfdl.HFDLPacketInfo) -> None | Aircraft:
        ac: None | Aircraft = None
        if packet.flight:
            ac = self.aircraft_by_flight.get(packet.flight)
        if not ac and packet.icao:
            ac = self.aircraft_by_icao.get(packet.icao)
        if not ac and packet.tail:
            ac = self.aircraft_by_tail.get(packet.tail)
        # if we've migrated a session, remove the old session ID, so there aren't duplicates.
        if ac and packet.session_id != ac.session_id:
            self.purge_session(ac.session_id)
            if packet.session_id and packet.session_id[2] not in (0xFF, -1):
                ac.session_id = packet.session_id  # probably redundant
        return ac

    def update_session(self, packet: hfdl.HFDLPacketInfo) -> None:
        self.preen_aircraft()
        session_id = packet.session_id
        if not session_id or session_id == (None, None, None):
            # probably an SPDU. have to ignore this packet for aircraft considerations.
            position = packet.position
            if position:
                if (
                    (position[0] != position[1] or position[0])  # not 0, 0
                    and -91 < position[0] < 91  # not 180, 180
                    and -181 < position[1] < 181
                ):
                    logging.error(f"discarding position from {packet.packet} (reason 2)")
                logging.error(f"discarding position from {packet.packet} (reason 1)")
            return
        # deal with some logon cases. logoff will take care of itself when the ID is reused.
        if packet.is_logon:
            # remove any lingering other aircraft, but continue.
            self.purge_session(session_id)
        if packet.is_logon_resume or session_id[2] == 0xFF:
            # the session_id will be 255(unknown), so the session can only be reestablished by reference to previous ac
            # info
            ac = self.aircraft_from_packet(packet)
            if ac:
                # resume using the old ID
                session_id = ac.session_id
            else:
                # Create a new Aircraft, but don't give it a valid session ID, nor add it to the main dict.
                # Instead, it will sit in the icao/tail/flight lookups.
                # When a real ID is assigned, this packet's data will become available to the now-tracked aircraft.
                session_id = None
                ac = Aircraft(session_id=(-1, -1, -1))
        else:
            ac = self.aircraft_by_session.get(session_id)
        if session_id:
            if ac:
                # Need to do some sanity checking to make sure this is the *correct* session. If some other identifying
                # property is present and different, then we have missed a LOGOOUT/LOGON pair. Not a surprise.
                if (
                    (ac.flight and packet.flight and packet.flight != ac.flight)
                    or (ac.r and packet.tail and packet.tail != ac.r)
                    or (ac.hex_id and packet.icao and packet.icao != ac.hex_id)
                ):
                    self.purge_session(session_id)
                    ac = None
            if not ac:
                ac = self.aircraft_from_packet(packet)
                if ac:
                    ac.session_id = session_id
                else:
                    ac = Aircraft(session_id=session_id)
                self.aircraft_by_session[session_id] = ac
        if ac:  # mypy nonsense, at this point, ac should always be non-None
            ac.update(packet, self.home_lat, self.home_lon)
            # associate it with the other ways of identifying an aircraft
            if ac.hex_id:
                self.aircraft_by_icao[ac.hex_id] = ac
            if ac.flight:
                self.aircraft_by_flight[ac.flight] = ac
            if ac.r:
                self.aircraft_by_icao[ac.r] = ac

    def on_hfdl(self, packet: hfdl.HFDLPacketInfo) -> None:
        util.call_soon(self.update_session, packet)
        # self.update_session(packet)

    @property
    def tracked_aircraft(self) -> Sequence[Aircraft]:
        out = list(self.aircraft_by_session.values())
        all_sources = [
            list(self.aircraft_by_flight.values()),
            list(self.aircraft_by_tail.values()),
            list(self.aircraft_by_icao.values()),
        ]
        for ac in itertools.chain(*all_sources):
            if ac not in out:
                out.append(ac)
        return out


if __name__ == "__main__":
    import sys
    import pathlib
    import json

    def aircraft_table_row(aircraft: Aircraft) -> str:
        columns = [
            ("sess", aircraft.session_hex),
            ("acid", aircraft.best_effort_id),
            ("seen", aircraft.seen),
            ("num_msg", aircraft.messages),
            ("rssi", f"{aircraft.rssi:0.2f}"),
            ("lat", f"{aircraft.lat:0.3f}" if aircraft.lat else "n/a"),
            ("lon", f"{aircraft.lon:0.3f}" if aircraft.lon else "n/a"),
            ("head", f"{aircraft.calc_track:0.1f}" if aircraft.calc_track else "n/a"),
            ("r_dst", int(aircraft.r_dst) if aircraft.r_dst else "n/a"),
            ("r_dir", int(aircraft.r_dir) if aircraft.r_dir else "n/a"),
            ("pktyp", aircraft.type if aircraft.type else ""),
        ]
        out = ["<tr>"]
        for klass, value in columns:
            out.append(f"<td class='{klass}'>{value}</td>")
        out.append("</tr>")
        return "".join(out)

    def aircraft_table(aircraft: Sequence[Aircraft]) -> str:
        out = ["<table>"]
        for ac in aircraft:
            out.append(aircraft_table_row(ac))
        out.append("</table>")
        return "\n".join(out)

    inpath = pathlib.Path(sys.argv[1])
    intext = inpath.read_text()
    tracker = AircraftTracker({"latitude": 60, "longitude": -40})
    for line in intext.split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line.strip("\u0000"))
        except Exception:
            print(line)
            print(ord(line[0]))
            raise
        packet = hfdl.HFDLPacketInfo(data)
        logging.warning(str(packet))
        tracker.update_session(packet)
    print("<html><body>")
    sorted_ac = sorted(tracker.tracked_aircraft, key=lambda e: e.seen)
    print(aircraft_table(sorted_ac))
    print("</body></html>")
