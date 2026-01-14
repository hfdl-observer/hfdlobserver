# hfdl_observer/nano.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Optional

import pynng  # type: ignore[import-not-found]

import hfdl_observer.util as util

logger = logging.getLogger(__name__)


# backwards compatibility ish
QueueShutDown = getattr(asyncio, "QueueShutDown", asyncio.QueueEmpty)


Message = util.Message


class NanoSubscriber:
    url: str
    channel: str
    callbacks: list[tuple[Optional[Callable[[Message], bool]], Callable[[Message], None]]]

    def __init__(self, url: str, channel: str):
        self.channel = channel
        self.url = url

    def add_callback(
        self, callback: Callable[[Message], None], filter: Optional[Callable[[Message], bool]] = None
    ) -> None:
        self.callbacks.append((filter, callback))

    async def run(self) -> None:
        self.running = True
        while self.running:
            with pynng.Sub0(dial=self.url, recv_timeout=1000) as service:
                service.subscribe(self.channel.encode())
                while self.running:
                    try:
                        data = await service.arecv()
                    except pynng.Timeout:
                        pass
                    else:
                        if data:
                            target, subject, body = data.decode().split("|", 2)
                            payload = json.loads(body)
                            message = Message(target=target, subject=subject, payload=payload)
                            logger.debug(f"received {message}")
                            for filter, callback in self.callbacks:
                                if not filter or filter(message):
                                    util.call_soon(callback, message)

    async def stop(self) -> None:
        self._stop()

    def _stop(self) -> None:
        self.running = False


class NanoPublisher:
    url: str
    queue: asyncio.Queue[str]
    task: asyncio.Task | None = None

    def __init__(self, host: str, port: int):
        self.queue = asyncio.Queue()
        self.url = f"tcp://{host}:{port}"

    def start(self) -> None:
        if self.task is None:
            self.task = util.schedule(self.run())

    def stop(self) -> None:
        self.running = False

    def __del__(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    async def run(self) -> None:
        self.running = True
        while self.running:
            with pynng.Pub0(dial=self.url, send_timeout=1000) as service:
                while self.running:
                    try:
                        message = await asyncio.wait_for(self.queue.get(), 1)
                        await service.asend(message.encode())
                    except pynng.Timeout:
                        pass
                    except QueueShutDown:
                        self.running = False

    async def publish(self, message: Message) -> None:
        await self._publish(f"{message.target}|{message.subject}", json.dumps(message.payload))

    async def multi_publish(self, targets: list[str], subject: str, payload: Any) -> None:
        json_payload = json.dumps(payload)
        await asyncio.gather(
            *[self._publish(f"{target}|{subject}", json_payload) for target in targets], return_exceptions=True
        )

    async def _publish(self, channel: str, payload: str) -> None:
        if self.task is None:
            # logger.info('publisher not connected')
            self.start()
            logger.debug(f"publish c={channel} l={len(payload)}")
            await self.queue.put(f"{channel}|{payload}")


class NanoBroker:
    thread: Optional[threading.Thread] = None
    running: bool = False

    def __init__(self, host: str = "*", pub_port: int = 5559, sub_port: int = 5560) -> None:
        # yes, reversed
        self.pub_url = f"tcp://{host}:{sub_port}"
        self.sub_url = f"tcp://{host}:{pub_port}"

    def run(self) -> None:
        self.running = True
        while self.running:
            with pynng.Pub0(listen=self.pub_url) as xpub, pynng.Sub0(listen=self.sub_url, recv_timeout=300) as xsub:
                while self.running:
                    try:
                        xpub.send(xsub.recv())
                    except pynng.Timeout:
                        pass

    def start(self, daemon: bool = True) -> None:
        if not self.thread:
            self.thread = threading.Thread(target=self.run, daemon=daemon)
            self.thread.start()
