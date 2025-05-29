# hfdl_observer/zero.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import json
import logging
import multiprocessing
import multiprocessing.synchronize
import threading

from typing import Any, Optional

import zmq
import zmq.asyncio

import hfdl_observer.util as util

Context = zmq.asyncio.Context
logger = logging.getLogger(__name__)


def get_thread_context() -> Context:
    if not hasattr(util.thread_local, 'zmq_context'):
        context = util.thread_local.zmq_context = Context.instance()
        context.setsockopt(zmq.LINGER, 0)
    return util.thread_local.zmq_context  # type: ignore


Message = util.Message


class AbstractZeroBroker:
    host: str
    pub_port: int
    sub_port: int

    def __init__(self, host: str = '*', pub_port: int = 5559, sub_port: int = 5560, **extra: Any) -> None:
        self.host = host
        self.pub_port = pub_port
        self.sub_port = sub_port

    def start(self, daemon: bool = True) -> None:
        raise NotImplementedError(self.__class__.__name__)


class ZeroBroker(AbstractZeroBroker):
    def __init__(
        self,
        event: multiprocessing.synchronize.Event | threading.Event,
        host: str = '*',
        pub_port: int = 5559,
        sub_port: int = 5560,
        **kwargs: Any
    ) -> None:
        super().__init__(host, pub_port, sub_port, **kwargs)
        self.initialised = event

    def running(self) -> None:
        self.initialised.set()

    def run(self) -> None:
        context = zmq.Context()
        context.setsockopt(zmq.LINGER, 0)

        # Socket facing clients
        xpub = context.socket(zmq.XPUB)
        xpub.bind(f"tcp://{self.host}:{self.sub_port}")

        # Socket facing services
        xsub = context.socket(zmq.XSUB)
        xsub.bind(f"tcp://{self.host}:{self.pub_port}")

        try:
            self.initialised.set()
            zmq.proxy(xpub, xsub)
        finally:
            xpub.close(0)
            xsub.close(0)
            context.term()


class ThreadingZeroBroker(ZeroBroker):
    thread: None | threading.Thread = None
    initialised: threading.Event

    def __init__(self, host: str = '*', pub_port: int = 5559, sub_port: int = 5560, **kwargs: Any) -> None:
        super().__init__(threading.Event(), host, pub_port, sub_port, **kwargs)

    def start(self, daemon: bool = True) -> None:
        if not self.thread:
            self.thread = threading.Thread(target=self.run, daemon=daemon)
            self.thread.start()


class MultiprocessingZeroBroker(AbstractZeroBroker):
    # zmq has the unfortunate "habit" (that's really a design choice) that when it gets any sort of error, it crashes
    # its whole process, including Python, without any possibility to recover. Using multiprocessing means we can still
    # use pyzmq, and keep the broker under control of the main process, but can also recover from the above crash.
    process: multiprocessing.Process
    initialised: multiprocessing.synchronize.Event
    thread: None | threading.Thread = None

    def __init__(self, host: str = '*', pub_port: int = 5559, sub_port: int = 5560, **kwargs: Any) -> None:
        super().__init__(host, pub_port, sub_port, **kwargs)
        self.initialised = multiprocessing.Event()

    def start(self, daemon: bool = True) -> None:
        self.daemon = daemon
        if not self.thread:
            self.thread = threading.Thread(target=self.run, daemon=daemon)
            self.thread.start()

    def run(self) -> None:
        broker_logger = logging.getLogger(__name__)
        while not util.shutdown_event.is_set():
            broker_logger.info(f'preparing {self}')
            broker = ZeroBroker(self.initialised, self.host, self.pub_port, self.sub_port)
            self.process = multiprocessing.Process(
                target=broker.run,
                args=(self.initialised, self.host, self.pub_port, self.sub_port),
                daemon=self.daemon
            )
            broker_logger.info(f'starting {self.process}')
            self.process.start()
            broker_logger.info(f'waiting for {self.process}')
            self.process.join()
            broker_logger.info(f'{self.process} ended. will restart')
        self.process.terminate()


class ZeroSubscriber:
    url: str
    channel: str
    context: Context
    socket: Optional[zmq.Socket] = None

    def __init__(self, url: str, channel: str, context: Optional[zmq.asyncio.Context] = None):
        self.context = get_thread_context()
        self.channel = channel
        self.url = url

    async def run(self) -> None:
        if self.socket:
            return
        self.running = True
        self.socket = self.context.socket(zmq.SUB)
        self.socket.connect(self.url)
        self.socket.setsockopt(zmq.SUBSCRIBE, self.channel.encode())

        try:
            while self.running and self.socket and not self.socket.closed and not util.is_shutting_down():
                try:
                    events = await self.socket.poll(timeout=5_000)  # millis?
                    if events and self.running:
                        parts = await self.socket.recv_multipart()
                    else:
                        continue
                except asyncio.TimeoutError:
                    continue
                header, body = parts
                payload = json.loads(body.decode())
                target, subject = header.decode().split('|', 1)
                try:
                    sender = payload.get('sender', None)
                    payload = payload.get('payload', payload)
                except AttributeError:
                    logger.info(f'no sender found in {payload!r}')
                    sender = None
                message = Message(target=target, subject=subject, payload=payload, sender=sender)
                self.receive(message)
        except asyncio.CancelledError:
            pass
        finally:
            logger.debug(f'no longer subscribed to {self.url}/{self.channel}')
            util.call_soon(self._stop)

    def receive(self, message: Message) -> None:
        logger.debug(f'received {message}')

    def _stop(self) -> None:
        self.running = False
        if self.socket is not None and not self.socket.closed:
            logger.warning(f'will close socket for {self}')
            self.socket.disconnect(self.url)
            # self.socket.close(0)
            self.socket = None

    def __del__(self) -> None:
        self._stop()


class ZeroPublisher:
    host: str
    port: int
    channel: str
    socket: Optional[zmq.Socket] = None

    def __init__(self, host: str, port: int, context: Optional[zmq.asyncio.Context] = None):
        self.context = get_thread_context()
        self.host = host
        self.port = port

    def start(self) -> None:
        if self.socket is None:
            self.socket = self.context.socket(zmq.PUB)
            self.socket.connect(f'tcp://{self.host}:{self.port}')

    def stop(self) -> None:
        if self.socket is not None:
            logger.warning(f'will close socket for {self}')
            self.socket.close(0)
            self.socket = None

    def available(self) -> bool:
        return self.socket is not None

    async def publish(self, message: Message) -> None:
        payload = {
            'payload': message.payload,
            'sender': message.sender,
        }
        await self._publish(f'{message.target}|{message.subject}', json.dumps(payload))

    async def multi_publish(self, targets: list[str], subject: str, payload: Any) -> None:
        json_payload = json.dumps(payload)
        await asyncio.gather(
            *[self._publish(f'{target}|{subject}', json_payload) for target in targets],
            return_exceptions=True
        )

    async def _publish(self, channel: str, payload: str) -> None:
        encoded_channel = channel.encode()
        encoded_payload = payload.encode()
        if self.socket is None:
            try:
                self.start()
                logger.info('pub socket started')
            except OSError as err:
                logger.info(f'publisher error {err}')
        if self.socket is not None:
            logger.debug(f'publish c={channel} l={len(encoded_payload)}')
            await self.socket.send_multipart([encoded_channel, encoded_payload])

    def __del__(self) -> None:
        if self.socket is not None:
            self.stop()
