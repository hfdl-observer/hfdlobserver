# receivers.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import collections
import collections.abc
import datetime
import functools
import logging
import random
import uuid

from typing import Any, AsyncGenerator, Awaitable, MutableMapping, Optional

import hfdl_observer.bus as bus
import hfdl_observer.data as data
import hfdl_observer.messaging as messaging
import hfdl_observer.process as process
import hfdl_observer.util as util

import decoders
import iqsources


logger = logging.getLogger(__name__)


class ReceiverError(Exception):
    pass


class LocalReceiver(bus.EventNotifier, data.ChannelObserver, messaging.GenericSubscriber):
    frequencies: list[int]
    name: str
    tasks: list[asyncio.Task]
    conductor: Optional[str] = None
    last_seen: datetime.datetime
    registered: bool = False

    def __init__(self, name: str, config: collections.abc.MutableMapping) -> None:
        self.uuid = str(uuid.uuid4())
        self.config = config
        self.name = name
        self.logger = logger.getChild(self.name)
        self.frequencies = []
        self.last_seen = util.now()
        messaging.subscribe(self, self.target)
        messaging.subscribe(self, '/', 'available')
        messaging.subscribe(self, '/', 'unavailable')
        self.watchdog = bus.PeriodicCallback(60, [self.heartbeat], chatty=False)
        super().__init__()

    def payload(self, **data: Any) -> dict:
        _payload = {
            'name': self.name,
            'uuid': self.uuid,
        }
        _payload.update(data)
        return _payload

    @functools.cached_property
    def target(self) -> str:
        return f'@receiver+{self.name}'

    def on_remote_listen(self, message: messaging.Message) -> None:
        self.keepalive()
        if (message.target == self.target):
            payload: list[int] = message.payload
            util.schedule(self.listen(payload))
        else:
            logger.info(f"{self.target} told to listen to {message.target}'s frequencies")

    def on_remote_registered(self, message: messaging.Message) -> None:
        self.keepalive()
        uuid: str = message.payload
        if uuid == self.uuid:
            self.registered = True

    def on_remote_deregistered(self, message: messaging.Message) -> None:
        self.keepalive()
        uuid: str = message.payload
        if uuid == self.uuid:
            self.deregistered()

    def on_remote_ping(self, message: messaging.Message) -> None:
        if isinstance(message.payload, str):
            uuid: str = message.payload
            requester: str = str(self.conductor)
        else:
            requester = message.payload['src']
            uuid = message.payload['dst']
        if uuid == self.uuid:
            self.keepalive()
            messaging.publish_soon(messaging.Message(self.target, 'pong', self.payload(to=requester, src=self.uuid)))
        else:
            # observer is confused. Thinks another node is us. Reregister to clear it up.
            # This could cause flapping if there are two nodes with the same name actively running and registering.
            self.register()

    def on_remote_die(self, _: messaging.Message) -> None:
        # self.keepalive()
        logger.warning(f'{self} received DIE order')
        self.deregister()

    def on_remote_available(self, message: messaging.Message) -> None:
        if self.registered and message.payload['name'] == self.conductor:
            self.keepalive()
        else:
            payload: dict = message.payload
            self.conductor = payload['name']
            self.listener = data.ListenerConfig(payload['listener'])
            self.register()

    def on_remote_unavailable(self, _: messaging.Message) -> None:
        self.deregister()

    def on_observer_start(self) -> None:
        self.watchdog.start()

    def register(self) -> None:
        if self.conductor is not None:
            self.setup_harnesses()
            messaging.publish_soon(
                messaging.Message(
                    self.conductor,
                    'register',
                    self.payload(
                        widths=self.observable_widths(),
                        weight=self.config.get('weight', data.DEFAULT_RECEIVER_WEIGHT)
                    )
                )
            )

    def deregister(self) -> None:
        if self.conductor:
            logger.info(f'{self} deregistering')
            messaging.publish_soon(messaging.Message(self.conductor, 'deregister', self.payload()))
            task = util.schedule(self.stop())
            task.add_done_callback(self.deregistered)

    def deregistered(self, _: Any = None) -> None:
        self.registered = False
        self.conductor = None
        self.keepalive()

    def setup_harnesses(self) -> None:
        raise NotImplementedError(str(self.__class__))

    def keepalive(self) -> None:
        self.last_seen = util.now()

    def heartbeat(self) -> None:
        # called regularly. Should check to see if the (controlling) observer has been seen lately.
        # If not... do something drastic.
        # only need to care if we're registered!
        if self.registered:
            horizon = util.now() - datetime.timedelta(seconds=60)
            if self.last_seen < horizon:
                logger.warning(f'controller {self.conductor} may be dead. {self.last_seen}')
                self.deregister()
        if not self.is_running():
            util.schedule(self.maybe_stop())

    async def maybe_stop(self) -> None:
        if not self.is_running():
            await self.stop()

    def covers(self, freqs: list[int]) -> bool:
        # in this implementation it must be exact.
        return set(freqs) == set(self.frequencies)

    def is_running(self) -> bool:
        return False

    async def listen(self, frequencies: list[int]) -> None:
        if self.is_running() and set(frequencies) == set(self.frequencies):
            self.logger.info(f'already listening to {frequencies}')
            self.publish_listening()
            return
        self.logger.info(f'switching to {frequencies} from {self.frequencies}')
        await self.stop()
        self.frequencies = frequencies
        self.channel = self.observing_channel_for(frequencies)
        if frequencies:
            self.logger.info(f'switched to {frequencies}')
            _state = None
            try:
                await asyncio.sleep(0)
                async with util.aclosing(self.run()) as lifecycle:
                    async for state in lifecycle:
                        _state = state
                        await asyncio.sleep(0)
            finally:
                if _state and _state != 'done':
                    self.logger.debug(f'{self.name} finished listening lifecycle with {_state}')
                self.clear()
                await asyncio.sleep(0)
        else:
            self.logger.debug('empty frequency list, not starting new processes.')
            self.publish_listening()

    def publish_listening(self) -> None:
        messaging.publish_soon(messaging.Message(self.target, 'listening', self.payload(frequencies=self.frequencies)))

    async def stop(self) -> None:
        pass

    async def run(self) -> AsyncGenerator:
        raise NotImplementedError(str(self.__class__))
        yield None  # mypy gets confused without this.

    def clear(self) -> None:
        logger.info(f'{self} clearing frequencies')
        self.frequencies = []
        self.channel = self.observing_channel_for([])
        self.publish_listening()

    def __str__(self) -> str:
        return f'({self.__class__.__name__}) {self.name} on {self.frequencies}'

    def describe(self) -> str:
        if self.frequencies:
            middle = f' @{(min(self.frequencies) + max(self.frequencies)) / 2.0}'
        else:
            middle = ''
        return f'{self.name} via {self.describe_components()} on {len(self.frequencies)} freqs{middle}'

    def describe_components(self) -> list[str]:
        return []


class Web888Receiver(LocalReceiver):
    shell: bool = False

    def __init__(self, name: str, config: collections.abc.MutableMapping) -> None:
        super().__init__(name, config)

    def observable_widths(self) -> list[int]:
        return [int(self.config.get('channel_width', 12000))]


class DummyReceiver(Web888Receiver):
    client: iqsources.DummyClient | None = None
    decoder: decoders.DummyDecoder | None = None

    def setup_harnesses(self) -> None:
        if not self.client:
            self.client = iqsources.DummyClient(self.name, self.config.get('client', {}))
        if not self.decoder:
            self.decoder = decoders.DummyDecoder(self.name, self.config.get('decoder', {}), self.listener)

    async def run(self) -> AsyncGenerator:
        self.publish_listening()
        yield process.CommandState('done')


class Web888ExecReceiver(Web888Receiver):
    pipe: util.Pipe
    client: iqsources.KiwiClientProcess | None = None
    decoder: decoders.IQDecoderProcess | None = None

    def setup_harnesses(self) -> None:
        if not self.client:
            self.client = iqsources.KiwiClientProcess(
                self.name, self.config.get('client', {}), int(self.config.get('channel_width', 12000))
            )
        if not self.decoder:
            self.decoder = decoders.IQDecoderProcess(self.name, self.config.get('decoder', {}), self.listener)

    def is_running(self) -> bool:
        return (
            (self.client is not None and self.client.is_running())
            or (self.decoder is not None and self.decoder.is_running())
        )

    async def run(self) -> AsyncGenerator:
        self.publish_listening()
        if not self.channel:
            logger.debug(f'{self} channel was empty')
            return
        await asyncio.sleep(random.randrange(1, 40) / 20.0)   # thundering herd dispersal
        if self.client is None:
            raise ValueError('client not set up. This should not be reached')
        if self.decoder is None:
            raise ValueError('decoder not set up. This should not be reached')

        yield None   # FIXME

        channel = self.channel
        client: iqsources.KiwiClientProcess = self.client
        decoder: decoders.IQDecoderProcess = self.decoder

        with util.Pipe() as pipe:
            client.pipe = decoder.pipe = pipe

            async def exhaust(lifecycle: AsyncGenerator | None, other: process.ProcessHarness) -> None:
                if lifecycle is not None:
                    await asyncio.sleep(0)
                    async with util.aclosing(lifecycle):
                        async for state in lifecycle:
                            await asyncio.sleep(0)
                    await other.stop()

            client_run = client.listen(channel)
            decoder_run: AsyncGenerator | None = None
            # don't use aclosing here because we need both to be awaitable in parallel, so we only loop through
            # states until we can start the decoder.
            async for client_state in client_run:
                match client_state.event:
                    case 'running':
                        decoder_run = decoder.listen(channel)
                        break
            await asyncio.sleep(0)  # should exit from running agen to allow it to be exhausted?

            awaitables = [
                exhaust(client_run, decoder),
                exhaust(decoder_run, client),
            ]
            try:
                client_result, decoder_result = await asyncio.gather(*awaitables, return_exceptions=True)
                if isinstance(client_result, Exception) and decoder.is_running():
                    logger.warning(f'{self} client encountered an error', exc_info=client_result)
                    await decoder.stop()
                if isinstance(decoder_result, Exception) and client.is_running():
                    logger.warning(f'{self} decoder encountered an error', exc_info=decoder_result)
                    await client.stop()
            except asyncio.CancelledError:
                if decoder.is_running():
                    await decoder.stop()
                if client.is_running():
                    await client.stop()
            except Exception as err:
                logger.error(f'{self} encountered an error', exc_info=err)

    async def stop(self) -> None:
        if (self.decoder or self.client) and self.frequencies:
            self.logger.info(f'Stopping {self}')
        client = self.client
        decoder = self.decoder
        if decoder is not None:
            await decoder.stop()
        if client is not None:
            await client.stop()

    def clear(self) -> None:
        if (
            (self.client is None or not self.client.is_running())
            and (self.decoder is None or not self.decoder.is_running())
        ):
            super().clear()
        else:
            logger.info(f'{self.name} still has running process, not clearing')

    def describe_components(self) -> list[str]:
        out = []
        if self.client:
            out.append(self.client.describe())
        if self.decoder:
            out.append(self.decoder.describe())
        return out


class ReceiverPipe(process.ProcessHarness):
    cmd: list[str]

    def __init__(self, cmd: list[str]) -> None:
        super().__init__()
        self.shell = True
        self.cmd = cmd

    def commandline(self) -> list[str]:
        return self.cmd


class Web888PipeReceiver(Web888Receiver):
    client: iqsources.KiwiClient | None = None
    decoder: decoders.IQDecoder | None = None
    receiver_pipe: None | ReceiverPipe = None

    def setup_harnesses(self) -> None:
        if not self.client:
            self.client = iqsources.KiwiClient(self.name, self.config.get('client', {}))
        if not self.decoder:
            self.decoder = decoders.IQDecoder(self.name, self.config.get('decoder', {}), self.listener)
        if not self.receiver_pipe:
            self.receiver_pipe = ReceiverPipe(self.client.commandline() + ['|'] + self.decoder.commandline())

    async def run(self) -> AsyncGenerator:
        if self.receiver_pipe is not None:
            async with util.aclosing(self.receiver_pipe.run()) as lifecycle:
                async for current_state in lifecycle:
                    yield current_state
                await asyncio.sleep(0)

    async def stop(self) -> None:
        self.logger.debug('Stopping')
        if self.receiver_pipe is not None:
            await self.receiver_pipe.stop()


class DirectReceiver(LocalReceiver):
    observable_channel_widths: list[int]
    decoder: decoders.DirectDecoder | None = None

    def setup_harnesses(self) -> None:
        if not self.decoder:
            decoder_type = self.config['decoder']['type']
            decoder_class = getattr(decoders, decoder_type)
            self.decoder = decoder_class(self.name, self.config.get('decoder', {}), self.listener)
            if not isinstance(self.decoder, decoders.DirectDecoder):
                raise ValueError(f'{self.decoder} is not an expected Decoder')
            self.observable_channel_widths = self.decoder.observable_channel_widths()
            logger.debug(f'{self.name} observable channel widths {self.observable_channel_widths}')

    def is_running(self) -> bool:
        return self.decoder is not None and self.decoder.is_running()

    async def run(self) -> AsyncGenerator:
        if self.decoder is None:
            logger.info(f'{self} has no decoder')
            return
        self.publish_listening()
        await asyncio.sleep(random.randrange(1, 20) / 10.0)   # thundering herd dispersal
        try:
            async with util.aclosing(self.decoder.listen(self.channel)) as lifecycle:
                async for state in lifecycle:
                    logger.debug(f'{self} reached state {state.event}')
                    yield state
        except asyncio.CancelledError:
            logger.debug(f'{self} cancelled')
        except Exception:  # as err:
            logger.info(f'{self} encountered an error')  # , exc_info=err)

    async def stop(self) -> None:
        self.logger.debug('Stopping')
        if self.decoder is not None:
            await self.decoder.stop()

    def observable_widths(self) -> list[int]:
        return self.observable_channel_widths

    def clear(self) -> None:
        if (self.decoder is None or not self.decoder.is_running()):
            logger.debug(f'{self.name} has no running process {self.decoder}, clearing.')
            super().clear()
        else:
            logger.debug(f'{self.name} still has running a process, not clearing')

    def describe_components(self) -> list[str]:
        if self.decoder:
            return [self.decoder.describe()]
        return []


class ReceiverNode():
    local_receivers: list[LocalReceiver]

    def __init__(self, config: collections.abc.Mapping) -> None:
        self.config = config
        self.local_receivers = []

    def build_local_receiver(self, receiver_config: MutableMapping) -> LocalReceiver:
        typename = receiver_config['type']
        klass = globals()[typename]
        receiver: LocalReceiver = klass(receiver_config['name'], receiver_config)
        receiver.watch_event('fatal', self.on_fatal_error)
        self.local_receivers.append(receiver)
        return receiver

    def start(self) -> None:
        logger.debug(f'starting {len(self.local_receivers)} local receivers')
        for receiver in self.local_receivers:
            receiver.on_observer_start()

    def outstanding_awaitables(self) -> list[Awaitable]:
        outstanding: list[Awaitable] = [receiver.stop() for receiver in self.local_receivers]
        return outstanding

    async def stop(self) -> None:
        logger.debug(f'{self} stopped')
        await asyncio.gather(*self.outstanding_awaitables(), return_exceptions=True)

    def on_fatal_error(self, _: Any) -> None:
        pass
