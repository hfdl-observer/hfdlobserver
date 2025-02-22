# hfdl_observer/manage.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import datetime
import functools
import itertools
import json
import logging

from typing import Any, Callable, Coroutine, Optional

import hfdl_observer.bus
import hfdl_observer.env
import hfdl_observer.data as data
import hfdl_observer.hfdl
import hfdl_observer.network as network
import hfdl_observer.util as util


logger = logging.getLogger(__name__)


REAPER_HORIZON = 3600


class NetworkOverview(hfdl_observer.bus.Publisher):
    last_state: dict
    startables: list[Callable[[], Coroutine[Any, Any, None]]]
    tasks: list[asyncio.Task]

    def __init__(self, config: dict, updater: network.AbstractNetworkUpdater):
        super().__init__()
        self.last_state = {}
        self.config = config
        self.updater = updater
        self.save_path = hfdl_observer.env.as_path(config['state'])
        self.tasks = []
        self.startables = []
        for file_source in [hfdl_observer.env.as_path(p) for p in config.get('station_files', [])]:
            file_watcher = hfdl_observer.bus.FileRefresher(file_source, period=3600)
            assert file_source.exists(), f'{file_source} does not exist'
            # prime this pump... shouldn't be necessary, but.
            updater.on_systable(file_source.read_text())
            file_watcher.subscribe('text', updater.on_systable)
            self.startables.append(file_watcher.run)
        if self.save_path:
            try:
                previous = json.loads(self.save_path.read_text())
            except (json.JSONDecodeError, IOError):
                pass
            else:
                logger.debug("loading previous state")
                updater.on_community(previous)
        for ix, url_source in enumerate(config.get('station_updates', [])):
            if not isinstance(url_source, dict):
                url_source = {
                    'url': url_source
                }
            url_watcher = hfdl_observer.bus.RemoteURLRefresher(
                url_source['url'],
                period=url_source.get('period', 60 + ix)
            )
            url_watcher.subscribe('response', updater.on_community)
            self.startables.append(url_watcher.run)
        self.will_save = False
        updater.subscribe('availability', self.schedule_save)

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        for startable in self.startables:
            self.tasks.append(loop.create_task(startable()))
        # loop.create_task(url_watcher.run())
        # loop.create_task(file_watcher.run())

    def stop(self) -> None:
        for task in self.tasks:
            task.cancel()

    def schedule_save(self, _: Any) -> None:
        if not self.will_save:
            self.will_save = True
            asyncio.get_running_loop().call_later(self.config['save_delay'], self.save)
        self.publish('frequencies', {s.station_id: s.frequencies for s in self.updater.current()})

    def save(self) -> None:
        logger.debug('saving ground stations frequencies')
        current = self.updater.current()
        if self.save_path:
            self.will_save = False
            out = []
            when = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for station in current:
                sd = {
                    'id': station.station_id,
                    'last_updated': util.datetime_to_timestamp(station.valid_at),
                    'name': network.STATIONS[station.station_id].station_name,
                    'when': station.valid_at.astimezone(datetime.timezone.utc).isoformat(),
                    'stratum': station.stratum,
                    'update_source': station.from_station or station.agent,
                    'frequencies': {'active': station.frequencies}
                }
                out.append(sd)

            payload: dict = {
                'ground_stations': out
            }
            if payload != self.last_state:
                logger.info('saving station data')
                self.last_state = payload.copy()
                payload['when'] = when
                self.save_path.write_text(json.dumps(payload, indent=4) + '\n')
            self.publish('state', payload)  # maybe should be a deepcopy


class ReceiverProxy(hfdl_observer.bus.Publisher, data.ChannelObserver):
    name: str
    channel: Optional[data.ObservingChannel]

    def __init__(self, name: str, observable_widths: list[int], other: hfdl_observer.bus.Publisher):
        super().__init__()
        self.name = name
        self.observable_channel_widths = observable_widths
        self.channel = None
        other.subscribe(f'receiver:{self.name}', self.on_remote_event)

    def connect(self, queue: hfdl_observer.bus.Publisher) -> None:
        queue.subscribe(f'receiver:{self.name}', self.on_local_event)

    def send(self, *params: Any) -> None:
        self.publish(f'receiver:{self.name}', tuple(params))

    def covers(self, channel: data.ObservingChannel) -> bool:
        return self.channel is not None and self.channel.covers(channel)

    def on_local_event(self, payload: tuple[str, Any]) -> None:
        action, arg = payload
        if action == 'listen':
            self.send(payload)

    def on_remote_event(self, payload: tuple[str, Any]) -> None:
        action, arg = payload
        if action == 'listening':
            self.channel = self.observing_channel_for(arg)
            logger.debug(f'{self} updated from remote')

    def __str__(self) -> str:
        return f'{self.name} on {self.channel}'

    def die(self) -> None:
        self.send('die')

    def listen(self, freqs: list[int]) -> None:
        self.send('listen', freqs)

    def observable_widths(self) -> list[int]:
        return self.observable_channel_widths


class AbstractConductor(hfdl_observer.bus.Publisher, data.ChannelObserver):
    ranked_station_ids: list[int]
    ignored_frequencies: list[tuple[int, int]]
    proxies: list[ReceiverProxy]

    def __init__(self, config: dict) -> None:
        super().__init__()
        self.config = config
        self.ranked_station_ids = config['ranked_stations']
        ignores = config.get('ignored_frequencies', [])
        self.ignored_frequencies = util.normalize_ranges(ignores)
        self.proxies = []
        self.reaper = Reaper()
        self.reaper.subscribe('dead-receiver', self.on_dead_receiver)

    def add_receiver(self, receiver: ReceiverProxy) -> None:
        self.proxies.append(receiver)

    def is_ignored(self, frequency: int) -> bool:
        for ignore in self.ignored_frequencies:
            if ignore[0] <= frequency <= ignore[1]:
                return True
        return False

    def observable_widths(self) -> list[int]:
        raise NotImplementedError(str(self.__class__))

    def on_dead_receiver(self, frequencies: list[int]) -> None:
        for receiver in self.proxies:
            if receiver.channel and receiver.channel.frequencies == frequencies:
                receiver.die()


class UniformConductor(AbstractConductor):
    def observable_widths(self) -> list[int]:
        widths: set[int] = set()
        for proxy in self.proxies:
            proxy_widths = set(proxy.observable_widths())
            if not widths:
                widths = proxy_widths
            elif widths != proxy_widths:
                raise ValueError('only uniform channel widths is currently supported')
        return list(widths)

    def pick_channels(
        self,
        station_frequencies: dict[int, list[int]],
        original_channels: Optional[list[data.ObservingChannel]] = None,
    ) -> list[data.ObservingChannel]:
        channels: list[data.ObservingChannel] = [c.clone() for c in original_channels or []]
        for sid in self.ranked_station_ids:
            for frequency in sorted(station_frequencies.get(sid, [])):
                if self.is_ignored(frequency):
                    continue
                for channel in channels:
                    if channel.maybe_add(frequency):
                        break
                else:
                    channels.append(self.observing_channel_for([frequency]))
        return channels

    def merge_channels(self, channels: list[data.ObservingChannel]) -> list[data.ObservingChannel]:
        if not self.proxies:
            return []

        chosen_channels = channels[:len(self.proxies)]
        targetted_frequencies = []
        untargetted_frequencies = []
        chosen_frequencies = []
        for channel in chosen_channels:
            logger.debug(f'considering {channel}')
            for frequency in channel.frequencies:
                station = network.STATIONS[frequency]
                chosen_frequencies.append(frequency)
                if station.is_assigned(frequency):
                    if station.is_active(frequency):
                        targetted_frequencies.append(frequency)
                    else:
                        untargetted_frequencies.append(frequency)

        targetted_count = len(targetted_frequencies)
        untargetted_count = len(untargetted_frequencies)
        possible_frequencies = list(itertools.chain(*[c.frequencies for c in channels]))
        active_count = len([f for f in possible_frequencies if network.STATIONS.is_active(f)])
        extra = untargetted_count
        logger.info(f'Listening to {targetted_count} of {active_count} active frequencies (+{extra} extra).')
        return chosen_channels

    def assign_channels(self, channels: list[data.ObservingChannel]) -> None:
        keeps = {}
        starts: list[data.ObservingChannel] = []
        proxies_by_width = sorted(self.proxies, key=lambda p: max(p.observable_widths()), reverse=True)
        for channel in channels:
            for receiver in proxies_by_width:
                if receiver.covers(channel):
                    keeps[receiver.name] = channel
                    break
            else:
                logger.debug(f'adding {channel} to starts')
                starts.append(channel)

        available = []
        for receiver in proxies_by_width:
            if receiver.name in keeps:
                channel = keeps[receiver.name]
                logger.debug(f'keeping {receiver}')
            else:
                logger.debug(f'marking {receiver} available')
                available.append(receiver)

        available.sort(key=lambda a: max(a.observable_widths()))
        for channel in starts:
            for receiver in available:
                if max(receiver.observable_widths()) >= channel.width:
                    if receiver.channel:
                        self.reaper.remove_channel(receiver.channel)
                        receiver_freqs = receiver.channel.frequencies
                    else:
                        receiver_freqs = []
                    receiver.listen(channel.frequencies)
                    logger.info(f'assigned {channel.frequencies} to {receiver.name} (was {receiver_freqs})')
                    self.reaper.add_channel(channel)
                    available.remove(receiver)
                    break
            else:
                nl = '\n'
                logger.warning(f'cannot allocate a receiver for {channel}')
                logger.info(f'channels\n{nl.join(repr(c) for c in channels)}')
                logger.info(f'available left\n{nl.join(str(a) for a in available)}')
                logger.info(f'keeps\n{nl.join(str(a) for a in keeps.values())}')
                logger.info(f'starts\n{nl.join(str(s) for s in starts)}')

    def orchestrate(self, targetted: dict[int, list[int]], fill_assigned: bool = False) -> list[data.ObservingChannel]:
        if not self.proxies:
            return []
        channels = self.pick_channels(targetted)
        if fill_assigned:
            channels = self.pick_channels(network.STATIONS.assigned(), channels)
        actual_channels = self.merge_channels(channels)
        self.assign_channels(actual_channels)
        return actual_channels


class DiverseConductor(UniformConductor):
    def observable_widths(self) -> list[int]:
        width = 0
        for proxy in self.proxies:
            for proxy_width in proxy.observable_widths():
                if not width:
                    width = proxy_width
                else:
                    width = min(width, proxy_width)
        if not width:
            raise ValueError('conductor has no observable channel widths')
        return [width]

    def merge_channels(self, channels: list[data.ObservingChannel]) -> list[data.ObservingChannel]:
        if not self.proxies:
            return []
        targetted = []
        untargetted = []
        possible_frequencies = list(itertools.chain(*[c.frequencies for c in channels]))
        active_frequencies = [f for f in possible_frequencies if network.STATIONS.is_active(f)]

        actual_channels: list[data.ObservingChannel] = []
        for proxy in self.proxies:
            actual_channels.append(data.ObservingChannel(max(proxy.observable_widths()), []))

        actual_channels.sort(key=lambda e: e.allowed_width, reverse=True)

        # merge the atomic channels into the possible proxy channels.
        for channel in channels:
            for proxy_channel in actual_channels:
                if proxy_channel.maybe_add_all(channel.frequencies):
                    targetted.extend([f for f in channel.frequencies if f in active_frequencies])
                    untargetted.extend([f for f in channel.frequencies if f not in active_frequencies])
                    break
            else:
                logger.debug(f'no proxy channel for {channel}, ignoring')

        targetted_count = len(targetted)
        untargetted_count = len(untargetted)
        assigned = set(itertools.chain.from_iterable(network.STATIONS.assigned().values()))
        active_count = len([f for f in assigned if network.STATIONS.is_active(f)])
        extra = untargetted_count
        logger.info(f'Listening to {targetted_count} of {active_count} active frequencies (+{extra} extra).')
        # logger.info(f'actual channels\n{"\n".join(repr(c) for c in actual_channels)}')

        return actual_channels


class Reaper(hfdl_observer.bus.Publisher):
    channels: dict[int, data.ObservingChannel]
    last_seen: dict[int, int]

    def __init__(self) -> None:
        super().__init__()
        self.channels = {}
        self.last_seen = {}

    async def run(self) -> None:
        while True:
            await asyncio.sleep(59)

    @functools.cached_property
    def task(self) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(self.run())

    def start(self) -> asyncio.Task:
        return self.task

    def on_hfdl(self, packet: hfdl_observer.hfdl.HFDLPacketInfo) -> None:
        frequency = packet.frequency
        self.last_seen[frequency] = max(packet.timestamp, self.last_seen.get(frequency, 0))

    def add_channel(self, channel: data.ObservingChannel) -> None:
        logger.debug(f'reaper adding channel {channel}')
        for freq in channel.frequencies:
            self.channels[freq] = channel

    def remove_channel(self, channel: data.ObservingChannel) -> None:
        logger.debug(f'reaper removing channel {channel}')
        for freq in channel.frequencies:
            if freq in self.channels:
                del self.channels[freq]
            if freq in self.last_seen:
                del self.last_seen[freq]

    def check(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        horizon = now - REAPER_HORIZON
        for freq, channel in self.channels.items():
            if 0 < self.last_seen.get(freq, 0) < horizon:
                self.publish('dead-receiver', channel.frequencies)
