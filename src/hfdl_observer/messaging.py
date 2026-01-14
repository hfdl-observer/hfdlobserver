# hfdl_observer/messaging.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import functools
import logging
import weakref
from typing import Callable

import hfdl_observer.util as util
import hfdl_observer.zero as zero

logger = logging.getLogger(__name__)
Message = util.Message


class RemotePublisher(zero.ZeroPublisher):
    pass


class RemoteSubscriber(zero.ZeroSubscriber):
    task: asyncio.Task
    actual_receiver: Callable[[Message], None] | None = None

    def set_callback(self, callback: Callable[[Message], None]) -> None:
        self.actual_receiver = callback

    def receive(self, message: Message) -> None:
        if self.actual_receiver is not None:
            self.actual_receiver(message)
        else:
            super().receive(message)

    def start(self) -> asyncio.Task:
        self.task = util.schedule(self.run())
        return self.task


class RemoteBroker:
    def __init__(self, config: dict) -> None:
        if not config:
            # with no config, this becomes a dummy
            self.configured = False
            return
        self.host = config.get("host", "localhost")
        self.pub_port = config.get("pub_port", 5559)
        self.sub_port = config.get("sub_port", 5560)
        self.context = zero.get_thread_context()
        self._publisher = self.publisher()
        self.configured = True

    def subscriber(self, target: str) -> RemoteSubscriber:
        url = f"tcp://{self.host}:{self.sub_port}"
        logger.debug(f"subscriber {url}/{target}")
        return RemoteSubscriber(url, target, context=self.context)

    def publisher(self) -> RemotePublisher:
        logger.debug(f"publisher {self.host}:{self.pub_port}")
        return RemotePublisher(self.host, self.pub_port, context=self.context)

    async def publish(self, message: Message) -> None:
        if not hasattr(self, "_publisher"):  # race condition if we're asked to publish before we're initialized.
            return None
        try:
            logger.debug(f"queuing {message}")
            await self._publisher.publish(message)
        except AttributeError:
            logger.debug(f"dropping {message}")
        return None


class AbstractSubscriber:
    def dispatch_message(self, message: Message) -> None:
        raise NotImplementedError(self.__class__.__name__)


class GenericSubscriber(AbstractSubscriber):
    def __init__(self) -> None:
        # this ties the cache to the instance. If decorating directly, instances are not necessarily GCd. ever.
        self.get_message_handler = functools.cache(self.get_message_handler)  # type: ignore[method-assign]

    def get_message_handler(self, name: str) -> None | Callable:
        return getattr(self, f"on_remote_{name.strip()}", None)

    def dispatch_message(self, message: Message) -> None:
        handler = self.get_message_handler(message.subject)
        if callable(handler):
            logger.debug(f"dispatching {message} via {handler}")
            handler(message)
        else:
            pass
            # logger.debug(f'ignoring on_remote_{message.subject.strip()}')


class Subscription:
    target: str
    subject: str
    subscriber: weakref.ReferenceType[AbstractSubscriber]

    def __init__(self, subscriber: AbstractSubscriber, target: str = "", subject: str = "") -> None:
        self.subscriber = weakref.ref(subscriber)
        self.target = target
        self.subject = subject

    def receive(self, message: Message) -> bool:
        subscriber = self.subscriber()
        if subscriber is not None:
            subscriber.dispatch_message(message)
        return subscriber is not None

    def is_subscribed(self, target: str, subject: str) -> bool:
        return (self.target == "" or target.startswith(self.target)) and (
            self.subject == "" or subject.startswith(self.subject)
        )


class _Broker:
    remote_broker: RemoteBroker | None = None
    remote_subscriber: RemoteSubscriber | None = None
    subscribers: list[Subscription]

    def __init__(self) -> None:
        self.subscribers = []

    def set_remote_broker(self, remote_broker: RemoteBroker) -> None:
        self.remote_broker = remote_broker
        self.remote_subscriber = remote_broker.subscriber("")
        self.remote_subscriber.set_callback(self.receive)
        # TODO start it up. Is this all I need? I don't need an explicit stop since this really should only be called
        # once and persist for the lifetime of the broker, which is a singleton.
        self.remote_subscriber.start()

    def subscribe(self, subscriber: AbstractSubscriber, target: str = "", subject: str = "") -> None:
        self.subscribers.append(Subscription(subscriber, target, subject))

    async def publish_locally(self, message: Message) -> None:
        dead = []
        # target = f'{message.target}|{message.subject}'
        for subscriber in self.subscribers:
            if subscriber.is_subscribed(message.target, message.subject):
                if not subscriber.receive(message):
                    dead.append(subscriber)
        for dead_subscriber in dead:
            self.subscribers.remove(dead_subscriber)

    async def publish(self, message: Message) -> None:
        logger.debug(f"publish {message.target}|{message.subject}")
        await self.publish_locally(message)
        if self.remote_broker is not None:
            await self.remote_broker.publish(message)

    def receive(self, message: Message) -> None:
        if message.sender != util.MESSAGING_NODE_ID:
            util.schedule(self.publish_locally(message))

    def __del__(self) -> None:
        # do I actually need to do this?
        if self.remote_subscriber is not None:
            self.remote_subscriber._stop()


_BROKER = _Broker()


def subscribe(subscriber: AbstractSubscriber, target: str, subject: str = "") -> None:
    _BROKER.subscribe(subscriber, target, subject)


async def publish(message: Message) -> None:
    await _BROKER.publish(message)


def publish_soon(message: Message) -> None:
    util.schedule(_BROKER.publish(message))
