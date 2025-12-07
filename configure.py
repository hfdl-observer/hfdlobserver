#!/usr/bin/env python3
# configure.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
import pathlib
import sys

import requests
import yaml
from whiptail import Whiptail


def default_path(base, *keys):
    car, *cdr = keys
    d = base.setdefault(car, {})
    if d is None:
        d = base[car] = {}
    if cdr:
        return default_path(d, *cdr)
    assert d is not None, f"{keys} ... {base}"
    return d


active = requests.get("https://hfdl.observer/active.json").json()

ids = sorted(s["id"] for s in active["ground_stations"])
stations = {x: None for x in ids}
for station in active["ground_stations"]:
    stations[station["id"]] = station["name"]

settings = yaml.safe_load(pathlib.Path("settings.yaml.base").read_text())

w = Whiptail("Configure HFDL Observer", backtitle="A multi-headed dumphfdl receiver")

w.msgbox("""Welcome to HFDL Observer. Let's set up a simple configuration.

Web-888 can provide 13 streams of IQ data. HFDL Observer takes advantage of these to listen to as many useful HFDL frequencies as possible at any given time. In order to pick the right frequencies, you need to configure a prioritized list of station IDs.

You can either specify the list of IDs directly, or the installer can make a good(?) guess depending on the latitude and longitude of your Web-888 device.
""")

choice = w.menu("How would you like to rank stations?", ["Enter a list of IDs", "Determine from location"])
if choice[1] == 1:
    sys.exit()

station_list = []
if choice[0] == "Enter a list of IDs":
    entered = ""
    error = ""
    while True:
        w.msgbox(
            f"{error}The currently known stations are:\n"
            + "\n".join(f"{k} - {v}" for k, v in stations.items())
            + "\n\nEnter the list on the next page."
        )
        entered, code = w.inputbox("Enter the comma-separated list of Stations IDs", entered)
        if code == 1:
            sys.exit()
        try:
            station_list = [int(x) for x in entered.split(",")]
        except Exception:
            error = "There was something wrong with the list you entered. Use comma separated numbers.\n"
        else:
            if all(x in stations for x in station_list):
                break
            error = "One or more station IDs are invalid.\n"
else:
    lat = None
    long = None
    while True:
        s, code = w.inputbox("Enter the device's latitude (decimal. positive is north, negative is south)")
        if code == 1:
            sys.exit()
        try:
            lat = float(s)
            break
        except Exception:
            pass
    while True:
        s, code = w.inputbox("Enter the device's latitude (decimal. positive is east, negative is west)")
        if code == 1:
            sys.exit()
        try:
            long = float(s)
            break
        except Exception:
            pass
    import extras.guess_station_ranking as gss

    station_list = list(d[1] for d in gss.guess(lat, long))

default_path(settings, "observer", "conductor")["ranked_stations"] = station_list

entered, code = w.inputbox("[Optional] If you want to log packets locally, input a writeable directory here.")
if code == 1:
    sys.exit()
if entered:
    entered = entered.rstrip("/")
    default_path(settings, "receivers", "web888", "decoder")["packetlog"] = entered

entered, code = w.inputbox("[Optional] Enter your Airframes.io station ID")
if code == 1:
    sys.exit()
if entered:
    default_path(settings, "dumphfdl", "default")["station_id"] = entered


code = w.yesno("""
[Optional] Do you want to retrieve community updates of active frequencies? This helps to keep your station listening to the most accurate list of active frequencies. Your local station will update frequencies as it receives status updates ("squitters"), but using community sources can fill in holes and gaps. It is entirely optional.
""")

if code:
    default_path(settings, "observer", "tracker")["station_updates"] = [
        {"url": "https://hfdl.observer/active.json", "period": 61}
    ]

entered = ""
while True:
    entered, code = w.inputbox("[REQUIRED] Enter the <address>:<port> of the Web-888 device")
    if code == 1:
        sys.exit()
    try:
        address, port = entered.split(":")
        port = int(port)
    except Exception:
        w.msgbox("Please use the format <address>:<port>. Example: 'my.web888.example:1234'")
    else:
        default_path(settings, "receivers", "web888", "client").update(
            {
                "address": address,
                "port": port,
            }
        )
        break

out = pathlib.Path("settings.yaml")
extra = ""
if out.exists():
    out = pathlib.Path("settings.yaml.new")
    extra = "\n\nYou should merge this with your existing settings.yaml file before running."
else:
    extra = "\n\nYour HFDL Observer installation is configured for running via `hfdlobserver.sh`"

out.write_text(yaml.safe_dump(settings, default_flow_style=False))

w.msgbox(f"That's all the questions. Your basic configuration has been written to `{out}`.{extra}")
