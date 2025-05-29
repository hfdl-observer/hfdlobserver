#!/usr/bin/env python3
# main.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import asyncio.protocols
import collections
import collections.abc
import logging
import logging.handlers
import os
import pathlib
import sys

# from signal import SIGINT, SIGTERM
from typing import Any, Awaitable, Callable, Optional

import click

import hfdl_observer.bus as bus
import hfdl_observer.data
import hfdl_observer.heat
import hfdl_observer.hfdl
import hfdl_observer.listeners
import hfdl_observer.manage as manage
import hfdl_observer.messaging as messaging
import hfdl_observer.network as network
import hfdl_observer.settings
import hfdl_observer.util as util
import hfdl_observer.zero as zero

import hfdl_observer.orm as orm

import receivers

if sys.version_info < (3, 11):
    from backports.asyncio.runner import Runner
else:
    from asyncio import Runner


logger = logging.getLogger(
    sys.argv[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if __name__ == "__main__"
    else __name__
)
TRACEMALLOC = util.tobool(os.getenv("TRACEMALLOC", False))
if TRACEMALLOC:
    import tracemalloc

try:
    import sqlite3  # noqa: F401
except ImportError:
    logger.error(
        'sqlite3 not found. You may need to run "extras/migrate-from-888.sh" or reinstall'
    )
    sys.exit(1)


class HFDLObserverNode(receivers.ReceiverNode):
    def __init__(self, config: collections.abc.Mapping) -> None:
        super().__init__(config)

        for receiver_config in config["local_receivers"]:
            self.build_local_receiver(receiver_config)

    def start(self) -> None:
        self.running = True
        super().start()

    def on_fatal_error(self, data: tuple[str, str]) -> None:
        receiver, error = data
        logger.error(f"Bailing due to error on receiver {receiver}: {error}")
        self.running = False

    async def shutdown(self) -> None:
        awaitables = [receiver.stop() for receiver in self.local_receivers]
        self.local_receivers = []
        await asyncio.gather(*awaitables, return_exceptions=True)


class HFDLObserverController(manage.ConductorNode, receivers.ReceiverNode):
    running: bool = True
    packet_watcher: orm.PacketWatcher
    previous_description: list[str] | None = None

    def __init__(self, config: collections.abc.Mapping) -> None:
        manage.ConductorNode.__init__(self, config)
        receivers.ReceiverNode.__init__(self, config)
        self.packet_watcher = orm.PacketWatcher()
        hfdl_observer.data.PACKET_WATCHER = self.packet_watcher
        self.network_overview = manage.NetworkOverview(
            config["tracker"], network.UPDATER
        )
        self.network_overview.watch_event("state", network.UPDATER.prune)
        self.network_overview.watch_event("frequencies", self.on_frequencies)
        self.watch_event("orchestrated", self.maybe_describe_receivers)

        self.hfdl_listener = hfdl_observer.listeners.HFDLListener(
            config.get("hfdl_listener", {})
        )
        self.hfdl_consumers = [
            hfdl_observer.listeners.HFDLPacketConsumer(
                [
                    hfdl_observer.listeners.HFDLPacketConsumer.any_in(
                        "spdu", "freq_data"
                    )
                ],
                [network.UPDATER.on_hfdl],
            ),
            hfdl_observer.listeners.HFDLPacketConsumer(
                [lambda line: True],
                [self.on_hfdl, self.packet_watcher.on_hfdl],
            ),
        ]
        self.listener_info = self.hfdl_listener.connection_info

        for receiver_config in config["local_receivers"]:
            self.build_local_receiver(receiver_config)

    def on_hfdl(self, packet: hfdl_observer.hfdl.HFDLPacketInfo) -> None:
        self.notify_event("packet", packet)

    def on_fatal_error(self, data: tuple[str, str]) -> None:
        receiver, error = data
        logger.error(f"Bailing due to error on receiver {receiver}: {error}")
        self.running = False

    def start(self) -> None:
        self.running = True
        manage.ConductorNode.start(self)
        receivers.ReceiverNode.start(self)
        self.packet_watcher.prune_every(60)
        self.network_overview.start()
        self.hfdl_listener.start(self.hfdl_consumers)

    def outstanding_awaitables(self) -> list[Awaitable]:
        outstanding: list[Awaitable] = [
            self.packet_watcher.stop_pruning(),
            self.network_overview.stop(),
        ]
        outstanding.extend(receivers.ReceiverNode.outstanding_awaitables(self))
        outstanding.extend(manage.ConductorNode.outstanding_awaitables(self))
        return outstanding

    async def stop(self) -> None:
        logger.info(f"{self} stopped")
        self.hfdl_listener.stop()
        outstanding = self.outstanding_awaitables()
        for o in outstanding:
            logger.info(f"outstanding {o}")
        for t in asyncio.all_tasks():
            logger.info(f"task {t}")
        await asyncio.gather(*outstanding, return_exceptions=True)
        logger.info(f"{self} tasks halted")

    def ministats(self, _: Any) -> None:
        util.schedule(self.aministats())

    async def aministats(self) -> None:
        table = hfdl_observer.heat.TableByFrequency()
        await table.populate(60, 10)
        for line in str(table).split("\n"):
            logger.info(f"{line}")

    def maybe_describe_receivers(self, *_: Any, force: bool = False) -> None:
        current = list(r.describe() for r in self.local_receivers)
        local_names = set(r.name for r in self.local_receivers)
        for proxy_name, proxy in self.proxies.items():
            if proxy_name not in local_names:
                current.append(proxy.describe())
        if force or self.previous_description != current:
            for line in current:
                logger.info(line)
            self.previous_description = current

    async def shutdown(self) -> None:
        awaitables = [receiver.stop() for receiver in self.local_receivers]
        self.local_receivers = []
        await asyncio.gather(*awaitables, return_exceptions=True)


class TraceMallocery(bus.PeriodicTask):
    def prepare(self) -> None:
        tracemalloc.start(2)
        self.last_snapshot = tracemalloc.take_snapshot()
        logger.warning("tracemalloc on")

    async def execute(self) -> None:
        p = pathlib.Path("memory.trace")
        with p.open("a", encoding="utf8") as f:
            f.write("====\n")
            f.write(f"{util.now()}\n")
            snapshot = tracemalloc.take_snapshot()
            try:
                diff = snapshot.compare_to(self.last_snapshot, "lineno")
                for e in diff:
                    f.write(
                        f'{e.size}|{e.size_diff}|{e.count}|{";".join(str(x) for x in e.traceback.format(5))}\n'
                    )
                # fname = f'memory-{util.now().isoformat().replace(':', '').replace('-', '')}.trace'
                # snapshot.dump(fname)
            except Exception as err:
                logger.error("error in tracemallocry", exc_info=err)
            else:
                logger.info("tracemalloc checkpoint")
        self.last_snapshot = snapshot


async def async_observe(observer: HFDLObserverController | HFDLObserverNode) -> None:
    logger.info("Starting observer")

    if TRACEMALLOC:
        tracemallocery = TraceMallocery(60)
        tracemallocery.start()
    observer.start()
    try:
        await util.shutdown_event.wait()
        logger.info(f"shutting down {observer}")
        try:
            await observer.shutdown()
        finally:
            logger.info(f"shutdown complete, stopping {observer}")
            await observer.stop()
    finally:
        logger.info(f"{observer} exiting")
        if TRACEMALLOC:
            await tracemallocery.stop()


def observe(
    on_observer: Optional[
        Callable[[HFDLObserverController, network.CumulativePacketStats], None]
    ] = None,
    as_controller: bool = True,
) -> None:
    key = "observer" if as_controller else "node"
    settings = getattr(hfdl_observer.settings, key)
    broker_config = settings.get("messaging", {})

    observer: HFDLObserverController | HFDLObserverNode

    try:
        with Runner() as runner:
            util.thread_local.loop = runner.get_loop()
            util.thread_local.runner = runner
            orm.gather_prune_stats = util.tobool(
                settings.get("show_prune_stats", False)
            )

            if as_controller:
                use_zmq = util.tobool(broker_config.get("enabled", False))
                if use_zmq:
                    # message_broker = zero.ThreadingZeroBroker(**broker_config)
                    message_broker = zero.MultiprocessingZeroBroker(**broker_config)
                    message_broker.start()  # daemon thread, not async.
                    logger.info("waiting for remote broker")
                    message_broker.initialised.wait()
                    remote_broker = messaging.RemoteBroker(broker_config)
                    messaging._BROKER.set_remote_broker(remote_broker)
                network.UPDATER = orm.NetworkUpdater()
                observer = HFDLObserverController(settings)
                cumulative = network.CumulativePacketStats()
                observer.watch_event("packet", cumulative.on_hfdl)
                if on_observer:
                    on_observer(observer, cumulative)
                else:
                    # initialize headless
                    observer.network_overview.watch_event("state", observer.ministats)
            else:  # just a node for receivers.
                remote_broker = messaging.RemoteBroker(broker_config)
                messaging._BROKER.set_remote_broker(remote_broker)
                observer = HFDLObserverNode(settings)
            try:
                runner.run(async_observe(observer))
            except KeyboardInterrupt:
                logger.error("Interrupted")
                try:
                    runner.run(observer.shutdown())
                    runner.run(observer.stop())
                except asyncio.CancelledError:
                    logger.error(
                        f"could not kill all the things: {list(asyncio.all_tasks())}"
                    )
                except RecursionError:
                    logger.error(
                        f"could not kill all the things: {list(asyncio.all_tasks())}"
                    )

        logger.info("HFDLObserver done.")
    except Exception as exc:
        logger.error("Fatal error encountered", exc_info=exc)
    finally:
        util.thread_local.runner = None
        util.thread_local.loop = None
        logger.info("HFDLObserver exiting.")


def setup_logging(
    loghandler: Optional[logging.Handler], debug: bool = True, quiet: bool = False
) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]  # default: stderr
    if loghandler:
        handlers.append(loghandler)
    logging.basicConfig(
        level=logging.WARNING if quiet else (logging.DEBUG if debug else logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


@click.command
@click.option("--headless", help="Run headless; with no CUI.", is_flag=True)
@click.option("--debug", help="Output debug/extra information.", is_flag=True)
@click.option(
    "--node",
    help="run as node only to connect with a remote Observer (implies headless)",
    is_flag=True,
)
@click.option(
    "--log",
    help="log output to this file",
    type=click.Path(
        path_type=pathlib.Path,
        writable=True,
        file_okay=True,
        dir_okay=False,
    ),
    default=None,
)
@click.option(
    "--config",
    help="load settings from this file",
    type=click.Path(
        path_type=pathlib.Path,
        readable=True,
        file_okay=True,
        dir_okay=False,
        exists=True,
    ),
    default=None,
)
@click.option(
    "--quiet", help="log only warnings and errors (overrides --debug)", is_flag=True
)
def command(
    headless: bool,
    debug: bool,
    node: bool,
    log: Optional[pathlib.Path],
    config: Optional[pathlib.Path],
    quiet: bool,
) -> None:

    settings_path = config or (pathlib.Path(__file__).parent.parent / "settings.yaml")
    try:
        hfdl_observer.settings.load(settings_path)
    except hfdl_observer.settings.DeprecatedSettingsError:
        logger.error(
            "A deprecated settings file is detected. It must be updated before you can continue."
        )
        logger.error("If you have not customized the settings file yourself:")
        logger.error('- run "hfdlobserver.sh configure" and')
        logger.error(f"- copy the resulting file to {settings_path}.")
        sys.exit(1)
    # old_settings.load(config or (pathlib.Path(__file__).parent.parent / 'settings.yaml'))
    handler = (
        logging.handlers.TimedRotatingFileHandler(log, when="d", interval=1)
        if log
        else None
    )
    if handler is not None:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s - %(message)s")
        )

    # if not executed in a tty-like thing, headless is forced.
    try:
        headless = headless or node or not sys.stdout.isatty()
        if headless:
            setup_logging(handler, debug, quiet)
            observe(as_controller=not node)
        else:
            import cui

            cui.screen(handler, debug, quiet)
    except Exception as exc:
        # will this catch the annoying libzmq assertion failures? nope.
        print(f"exiting due to exception: {exc}")
        print(str(exc.__traceback__))


if __name__ == "__main__":
    command()
