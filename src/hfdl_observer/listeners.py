# hfdl_observer/listeners.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import asyncio.protocols
import collections
import functools
import json
import logging
from typing import Any, Callable, Union

import hfdl_observer.bus
import hfdl_observer.data
import hfdl_observer.hfdl
import hfdl_observer.util as util

logger = logging.getLogger(__name__)


class HFDLPacketConsumer:
    filters: list[Callable[[str], bool]]
    callbacks: list[Callable[[hfdl_observer.hfdl.HFDLPacketInfo], None]]

    def __init__(
        self, filters: list[Callable[[str], bool]], callbacks: list[Callable[[hfdl_observer.hfdl.HFDLPacketInfo], None]]
    ) -> None:
        self.filters = filters or []
        self.callbacks = callbacks if callbacks is not None else []

    def matches(self, packet_str: str) -> bool:
        for filter in self.filters:
            if filter(packet_str):
                return True
        return False

    def consume(self, packet_str: str, packet: hfdl_observer.hfdl.HFDLPacketInfo) -> None:
        if self.matches(packet_str):
            for callback in self.callbacks:
                util.call_soon(callback, packet)

    @classmethod
    def any_in(cls, *terms: str) -> Callable[[str], bool]:
        return lambda s: any(x in s for x in terms)

    @classmethod
    def all_in(cls, *terms: str) -> Callable[[str], bool]:
        return lambda s: all(x in s for x in terms)


class UDPProtocol(asyncio.protocols.BaseProtocol):
    consumers: list[HFDLPacketConsumer]

    def __init__(self, hfdl_consumers: list[HFDLPacketConsumer]):
        self.buffers: dict = collections.defaultdict(lambda: "")
        self.consumers = hfdl_consumers

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def connection_lost(self, exc: Union[None, Exception]) -> None:
        logger.warn(exc)

    def datagram_received(self, data: bytearray, addr: Any) -> None:
        message = data.decode()
        self.buffers[addr] += message
        *head, tail = self.buffers[addr].split("\n")

        for line in head:
            line = line.strip()
            if not line.startswith("{"):
                logger.debug(f"dropping garbage: {line}")
                continue
            try:
                packet_data = json.loads(line)
            except json.JSONDecodeError as err:
                logger.warn(f"dropping garbage: {line}", exc_info=err)
            else:
                packet = hfdl_observer.hfdl.HFDLPacketInfo(packet_data)
                logger.info(f"packet {packet}")
                # self.on_hfdl(packet)
                for consumer in self.consumers:
                    util.call_soon(consumer.consume, line, packet)
        if tail and len(tail) < 65536:  # primitive/naive stuffing check.
            self.buffers[addr] = tail
        else:
            del self.buffers[addr]


class HFDLListener(hfdl_observer.bus.EventNotifier):
    running: bool = False

    def __init__(self, settings: dict) -> None:
        self.settings = settings

    async def run(self, hfdl_consumers: list[HFDLPacketConsumer]) -> None:
        logger.debug("running HFDL UDP listener")
        self.transport, self.protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: UDPProtocol(hfdl_consumers),
            local_addr=(self.settings["address"], self.settings["port"]),
        )
        try:
            while self.running and not util.is_shutting_down():
                await asyncio.sleep(1)
        finally:
            try:
                self.transport.close()
            except RuntimeError:
                pass
        logger.debug("HFDL UDP listener done")

    def start(self, hfdl_consumers: list[HFDLPacketConsumer]) -> None:
        try:
            self.settings["address"]
            self.settings["port"]
        except KeyError:
            logger.warn("Missing HFDL Listener configuration, not starting one.")
        else:
            self.running = True
            util.schedule(self.run(hfdl_consumers))

    def stop(self) -> None:
        if self.transport:
            self.transport.close()
            self.running = False

    @property
    def listener(self) -> hfdl_observer.data.ListenerConfig:
        config = hfdl_observer.data.ListenerConfig()
        config.proto = "udp"  # all that is supported
        config.address = self.settings["address"]
        config.port = self.settings["port"]
        return config

    @functools.cached_property
    def connection_info(self) -> dict:
        address = self.settings["address"]
        if address == "0.0.0.0" or address == "*":  # nosec # 0.0.0.0 is only used to trigger a search.
            address = self.settings.get("advertised_address", None)
            if not address:
                logger.warning("attempting to discover a visible IP address. This may explode.")
                import socket

                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("254.254.254.254", 80))  # this address is in an unroutable and unusable space.
                address = s.getsockname()[0]
                s.close()
                logger.warning(f"found {address}. Setting advertised_address is preferred.")
        return {
            "protocol": "udp",
            "address": address,
            "port": self.settings["port"],
        }
