# hfdl_observer/settings.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import itertools
import json
import os
import pathlib
from typing import Any, MutableMapping, Union

import yaml

import hfdl_observer.env as env
import hfdl_observer.util as util

base_path: pathlib.Path = pathlib.Path(os.getcwd())
registry: MutableMapping[str, dict] = {}


def dereference(ref_key: str, type_key: str, data: MutableMapping, *configs: MutableMapping) -> MutableMapping:
    try:
        key = data[ref_key]
    except KeyError:
        return data
    defaults = [config.get(type_key, {}).get(key, {}) for config in configs]
    return util.DeepChainMap({k: v for k, v in data.items() if k != ref_key}, *defaults)


def dereference_output(output: MutableMapping, *configs: MutableMapping) -> MutableMapping:
    try:
        key = output["output"]
    except KeyError:
        return output
    defaults = [config.get("outputs", {}).get(key, {}) for config in configs]
    return util.DeepChainMap({k: v for k, v in output.items() if k != "output"}, *defaults)


def dereference_dumphfdl(dumphfdl: MutableMapping, *configs: MutableMapping) -> MutableMapping:
    try:
        key = dumphfdl["dumphfdl"]
    except KeyError:
        return dumphfdl

    bases = [config.get("dumphfdl", {}).get(key, {}) for config in configs]
    complex_outputs: dict[str, list[MutableMapping]] = {}
    resolved_outputs = []
    source_outputs = [dumphfdl] + bases
    for output in itertools.chain.from_iterable(s["output"] for s in source_outputs if "output" in s):
        try:
            key = output["output"]
        except KeyError:
            resolved_outputs.append(output)
        else:
            complex_outputs.setdefault(key, []).append(output)
    for k, mappings in complex_outputs.items():
        chained = util.DeepChainMap(*mappings) if len(mappings) > 1 else mappings[0]
        resolved_outputs.append(dereference_output(chained, *configs))
    outputs = {"output": resolved_outputs}

    declared = {k: v for k, v in dumphfdl.items() if k != "output"}
    defaults = [{k: v for k, v in base.items() if k != "output"} for base in bases]
    return util.DeepChainMap(outputs, declared, *defaults)


def dereference_decoder(decoder: MutableMapping, *configs: MutableMapping) -> MutableMapping:
    if "dumphfdl" in decoder:
        return dereference_dumphfdl(decoder, *configs)
    return decoder


def dereference_receiver(receiver: MutableMapping, *configs: MutableMapping, level: int = 0) -> MutableMapping:
    # print(f'dereference receiver {receiver.get('name', 'anon')} = {receiver.get('receiver', 'n/a')}')
    try:
        key = receiver["receiver"]
    except KeyError:
        return receiver
    if level > 3:  # really naive way to break cycles. But really, you shouldn't be doing this much nesting.
        return receiver
    templates = [config.get("receivers", {}).get(key, {}) for config in configs]
    sources = [receiver] + templates
    _decoder = util.DeepChainMap(*[s["decoder"] for s in sources if "decoder" in s])
    decoder = {"decoder": dereference_decoder(_decoder, *configs)}

    declared = {k: v for k, v in receiver.items() if k != "decoder"}
    templated = [dereference_receiver(template, *configs, level=level + 1) for template in templates]

    return util.DeepChainMap(decoder, declared, *templated)


def dereference_receivers(receivers: list, *configs: MutableMapping) -> list:
    return [dereference_receiver(receiver, *configs) for receiver in receivers]


def chained(key: str, *configs: MutableMapping) -> MutableMapping:
    sources: list[MutableMapping] = [config[key] for config in configs if key in config]
    if len(sources) > 1:
        return util.DeepChainMap(*sources)
    elif sources:
        return sources[0]
    else:
        return {}


class DeprecatedSettingsError(Exception):
    pass


def load(filepath: Union[str, pathlib.Path]) -> MutableMapping:
    path = pathlib.Path(filepath)
    if not path.is_absolute():
        path = pathlib.Path(os.getcwd()) / path

    global base_path
    base_path = path.parent
    env.base_path = base_path

    global registry
    registry = yaml.safe_load(path.read_text())
    if "config" in registry:
        raise DeprecatedSettingsError()
    for parent_key in ["observer", "node"]:
        parent = registry.get(parent_key, {})
        if "local_receivers" in parent:
            parent["local_receivers"] = dereference_receivers(parent["local_receivers"], registry, defaults)
        else:
            parent["local_receivers"] = dereference_receivers(
                defaults[parent_key]["local_receivers"], registry, defaults
            )
    _globals = globals()
    for key in ["observer", "cui", "node", "viewer", "aggregator"]:
        _globals[key] = chained(key, registry, defaults)
    return registry


defaults: dict[str, Any] = {
    "observer": {
        "conductor": {
            "type": "DiverseConductor",
            # "ranked_stations": [],
            # "ignored_frequencies": []
        },
        "hfdl_listener": {
            "address": "127.0.0.1",
            "port": 5540,
            # "advertised_address": ...,
        },
        "tracker": {
            "save_delay": 8,
            "state": "stations.state",
            "station_files": ["systable.conf"],
            "station_updates": [{"url": "https://hfdl.observer/active.json", "period": 61}],
        },
        "messaging": {
            "host": "0.0.0.0",
            "pub_port": 5559,
            "sub_port": 5560,
        },
        "local_receivers": [{"receiver": "web888", "name": f"web888-{i:02}"} for i in range(1, 14)],
    },
    "cui": {
        "ticker": {
            "bin_size": 60,
            "display_mode": "frequency",
            "show_all_active": False,
            "show_active_line": True,
            "show_confidence": True,
            "show_targetting": False,
            "flexible_width": False,
        }
    },
    "node": {"local_receivers": [], "messaging": {"host": "0.0.0.0", "pub_port": 5559, "sub_port": 5560}},
    "viewer": {"messaging": {"host": "0.0.0.0", "pub_port": 5559, "sub_port": 5560}},
    "dumphfdl": {
        "default": {
            "quiet": True,
            "decoder_path": "dumphfdl",
            "shoulder": 0.8,
            "system_table": "systable.conf",
            "system_table_save": "systable_updated.conf",
            "output": [
                {"output": "hfdl_observer"},
            ],
        }
    },
    "outputs": {
        "hfdl_observer": {"format": "json", "protocol": "udp", "address": "hfdl.observer", "port": 5542},
        "acars_router": {"format": "json", "protocol": "udp", "address": "acars_router.local", "port": 5556},
        "acarshub": {"format": "json", "protocol": "udp", "address": "acarshub.local", "port": 5556},
        "readsb": {"format": "basestation", "protocol": "tcp", "address": "readsb.local", "port": 30009},
    },
    "receivers": {
        "web888": {
            "type": "Web888ExecReceiver",
            # "channel_width", 12000
            "client": {
                "address": "web-888.local",
                "port": 8073,
                "type": "KiwiClientProcess",
                "settle_time": 1,
                "quiet": False,
                "username": "kiwi:hfdlobserver",
                "agc_files": {
                    "*": "agc.yaml",
                    "2": "agc-02M.yaml",
                    "3": "agc-03M.yaml",
                    "4": "agc-04M.yaml",
                    "5": "agc-05M.yaml",
                    "6": "agc-06M.yaml",
                    "8": "agc-08M.yaml",
                    "10": "agc-10M.yaml",
                    "11": "agc-11M.yaml",
                    "13": "agc-13M.yaml",
                    "15": "agc-15M.yaml",
                    "17": "agc-17M.yaml",
                    "21": "agc-21M.yaml",
                },
            },
            "decoder": {"type": "IQDecoderProcess", "dumphfdl": "default"},
        },
        "pipe888": {"type": "Web888PipeReceiver", "client": {"type": "KiwiClient"}, "decoder": {"type": "IQDecoder"}},
        "dummy": {"type": "DummyReceiver"},
        "airspyhf": {
            "type": "DirectReceiver",
            "decoder": {
                "type": "SoapySDRDecoder",
                "settle_time": 1,
                "sample-rates": [912000, 768000, 650000, 456000, 384000, 228000, 192000],
                "dumphfdl": "default",
                "soapysdr": {"driver": "airspyhf", "gain": None},
            },
        },
        "rx888mk2": {
            "type": "DirectReceiver",
            "decoder": {
                "type": "RX888mk2Decoder",
                "settle_time": 1,
                "sample-rates": [2000000, 4000000, 8000000],
                "dumphfdl": "default",
                "soapysdr": {"driver": "SDDC"},
                "gain": 89,
            },
        },
        "rspdx+sdrplay": {
            "type": "DirectReceiver",
            "decoder": {
                "type": "SoapySDRDecoder",
                "settle_time": 9,
                "sample-rates": [
                    62500,
                    96000,
                    125000,
                    192000,
                    250000,
                    384000,
                    500000,
                    768000,
                    1000000,
                    [2000000, 3000000],
                ],
                "dumphfdl": "default",
                "soapysdr": {"driver": "sdrplay"},
                "device-settings": {
                    "rfnotch_ctrl": False,
                    "dabnotch_ctrl": False,
                    "agc_setpoint": -14,
                    "biasT_ctrl": False,
                    "rfgain_sel": 0,
                },
            },
        },
        "rsp1a+sdrplay": {
            "type": "DirectReceiver",
            "decoder": {
                "type": "SoapySDRDecoder",
                "settle_time": 10,
                "sample-rates": [1300000, 1536000, 2048000, [2000000, 5500000]],
                "dumphfdl": "default",
                "soapysdr": {"driver": "sdrplay"},
                "device-settings": {
                    "rfnotch_ctrl": False,
                    "dabnotch_ctrl": False,
                    "agc_setpoint": -14,
                    "biasT_ctrl": False,
                    "rfgain_sel": 0,
                },
            },
        },
        "rsp1a+miri": {
            "type": "DirectReceiver",
            "decoder": {
                "type": "SoapySDRDecoder",
                "sample-rates": [1300000, 1536000, 2048000, 4000000, 5000000],
                "dumphfdl": "default",
                "soapysdr": {"driver": "soapyMiri", "bufflen": 65536, "buffers": 64, "asyncBuffs": 32},
                "device-settings": {"flavour": "SDRplay", "biastee": False},
            },
        },
    },
}
observer: MutableMapping = {}
cui: MutableMapping = {}
node: MutableMapping = {}
viewer: MutableMapping = {}
aggregator: MutableMapping = {}


def deepchainmap_representer(dumper: yaml.SafeDumper, dcm: util.DeepChainMap) -> yaml.nodes.MappingNode:
    return dumper.represent_dict(dcm.dict())


def get_dumper() -> type[yaml.SafeDumper]:
    safe_dumper = yaml.SafeDumper
    safe_dumper.add_representer(util.DeepChainMap, deepchainmap_representer)
    return safe_dumper


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        filename = sys.argv[1]
    else:
        filename = "custom.yaml"
    loaded = load(filename)
    resolved = dict(loaded)
    for key in ["observer", "cui", "node", "viewer"]:
        resolved[key] = chained(key, loaded, defaults)
    print(yaml.dump(loaded, Dumper=get_dumper()))
    # out = yaml.safe_dump([d.dict() for d in observer['local_receivers']])
    # print(out)
    print(json.dumps(dict(resolved), indent=4, default=lambda o: o.dict() if hasattr(o, "dict") else list(o)))
