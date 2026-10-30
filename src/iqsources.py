# iqsources.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import collections
import collections.abc
import functools
import logging
import os
import pathlib
import random
import threading
import time
from abc import abstractmethod
from typing import AsyncGenerator, Callable, Generator, Mapping, Optional

import ka9q
import ka9q.stream
import numpy

import hfdl_observer.data
import hfdl_observer.env as env
import hfdl_observer.process as process
import hfdl_observer.util as util

logger = logging.getLogger(__name__)


class Configurable:
    name: str
    config: collections.abc.Mapping
    channel: hfdl_observer.data.ObservingChannel | None = None

    def __init__(self, name: str, config: collections.abc.MutableMapping):
        self.name = name
        self.config = config


class AbstractIQSource(process.Runnable, Configurable):
    config: collections.abc.Mapping
    channel: hfdl_observer.data.ObservingChannel | None = None

    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> AsyncGenerator[process.CommandState, None]:
        self.channel = channel
        self.logger.debug(f"{self} starting {channel}")
        if self.channel and self.channel.frequencies:
            async with util.aclosing(self.run()) as lifecycle:
                async for current_state in lifecycle:
                    match current_state.event:
                        case "running":
                            self.on_started()
                    yield current_state

    def on_started(self) -> None:
        pass

    @abstractmethod
    def describe(self) -> str:
        raise NotImplementedError(self.__class__.__name__)


class PipingIQSource(AbstractIQSource):
    pipe: util.Pipe

    def on_started(self) -> None:
        self.pipe.close_write()


class KiwiClientCommand(process.Command):
    pass


class KiwiClientMixin:
    # set outside the mixin
    name: str
    config: Mapping
    channel: hfdl_observer.data.ObservingChannel | None = None
    channel_width: int

    def agc_file(self, center_freq: float) -> Optional[pathlib.Path]:
        agc = self.config["agc_files"]
        band = center_freq // 1000
        for k in [band, "*"]:
            agc_value = agc.get(k)
            if agc_value:
                agc_file = env.as_path(agc_value)
                if agc_file.exists():
                    return agc_file
        return None

    def commandline(self) -> list[str]:
        if not self.channel or not self.channel.frequencies:
            return []
        # python3 kiwirecorder.py --nc -s n4dkd.asuscomm.com -p 8901 --log info -f 8927 -m iq --tlimit 60 --user kiwi_nc
        # | dumphfdl --iq-file - --sample-rate 12000 --sample-format CS16 --read-buffer-size 9600 --centerfreq 8927 8927
        # find the executable.
        pass_width = int(self.channel_width / 2)  # int((self.channel_width + hfdl.HFDL_CHANNEL_WIDTH * 1.5) / 2)
        args = [
            str(env.as_executable_path(self.config["recorder_path"])),
            "--nc",
            "--log",
            "warn",
            "-s",
            self.config["address"],
            "-p",
            str(self.config["port"]),
            "-f",
            str(self.channel.center_khz),
            "-m",
            "iq",
            "-L",
            str(-pass_width),
            "-H",
            f"+{pass_width}",
            "--OV",
            "--user",
            self.config["username"],
        ]
        agc_file = self.agc_file(self.channel.center_khz)
        if agc_file:
            args.extend(["--agc-yaml", str(agc_file)])
        return args

    def __str__(self) -> str:
        return f"{self.__class__.__name__}@{self.name}"


class KiwiClientStub(AbstractIQSource, KiwiClientMixin):
    def __init__(self, name: str, config: collections.abc.MutableMapping):
        # KiwiClientMixin has no init
        AbstractIQSource.__init__(self, name, config)

    async def stop(self) -> None:
        pass

    def is_running(self) -> bool:
        return False

    async def run(self) -> AsyncGenerator[process.CommandState, None]:
        yield process.CommandState("n/a")

    def describe(self) -> str:
        return self.__class__.__name__


class KiwiClientProcess(process.ProcessHarness, PipingIQSource, KiwiClientMixin):
    def __init__(self, name: str, config: collections.abc.MutableMapping, channel_width: int = 12000):
        # KiwiClientMixin has no init
        PipingIQSource.__init__(self, name, config)
        process.ProcessHarness.__init__(self)
        self.channel_width = channel_width
        self.settle_time = config.get("settle_time", 0) + random.randrange(1, 1000) / 1000.0  # nosec

    def execution_arguments(self) -> dict:
        return {
            "stdout": self.pipe.write,
            "pass_fds": (self.pipe.write,),
        }

    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> AsyncGenerator[process.CommandState, None]:
        self.channel = channel
        async with util.aclosing(super().listen(channel)) as lifecycle:
            async for state in lifecycle:
                yield state

    def create_command(self) -> KiwiClientCommand:
        cmd = self.commandline()
        command = KiwiClientCommand(
            parent_logger=self.logger,
            cmd=cmd,
            execution_arguments=self.execution_arguments(),
            recoverable_errors=[
                "Too busy now. Reconnecting after 15 seconds",
                "server closed the connection unexpectedly. Reconnecting after 5 seconds",
            ],
            unrecoverable_errors=[
                "Errno 9.*Bad file descriptor",
                "Errno 22.*Invalid argument",
                "Errno 32.*Broken pipe",
            ],
            valid_return_codes=[0, -11, -15],  # -11 is speculative. Some weirdness on odroid
        )
        return command


class DummySource(AbstractIQSource):
    async def run(self) -> AsyncGenerator[process.CommandState, None]:
        self.killed = False
        self.process = await asyncio.subprocess.create_subprocess_exec("ls")
        yield process.CommandState("done")
        self.logger.debug("dummy run completed")

    async def stop(self) -> None:
        pass

    def is_running(self) -> bool:
        return True

    def describe(self) -> str:
        return self.__class__.__name__


class KA9QManagedStream(ka9q.ManagedStream):
    def _start_stream(self) -> None:
        # Ensure that the filter is not the default +/- 5kHz default, but is mapped to the actual sample rate.
        filter_width = self._sample_rate // 2
        if self._channel:
            self._control.set_filter(ssrc=self._channel.ssrc, low_edge=-filter_width, high_edge=filter_width)
        super()._start_stream()

    def clean_channel(self) -> None:
        if self._channel:
            self._control.remove_channel(ssrc=self._channel.ssrc)

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)
        self.clean_channel()
        return False

    def __del__(self):
        try:
            super().__del__()
            self.clean_channel()
        except Exception:
            pass


class KA9QDynamicSource(PipingIQSource):
    stream_task: None | asyncio.Future = None
    sample_rates: list[tuple[int, int]]
    stopping_event: threading.Event

    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> AsyncGenerator[process.CommandState, None]:
        self.channel = channel
        self.logger.debug(f"starting {channel}")
        if self.channel and self.channel.frequencies:
            self.stopping_event = threading.Event()
            async with util.aclosing(self.run()) as lifecycle:
                async for current_state in lifecycle:
                    yield current_state
            self.stopping_event.set()

    async def run(self) -> AsyncGenerator[process.CommandState, None]:
        if not self.channel:
            raise ValueError(f"No channel set for {self}")
        stdin = None

        try:
            yield process.CommandState("preparing")
            queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def on_data(self, packet: bytearray) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, packet)

            self.stream_task = util.task_in_thread(self.stream, on_data)
            yield process.CommandState("running")
            while not self.stopping_event.is_set():
                frame = await queue.get()
                if frame is None:  # Context manager exited and loop finished cleanly
                    self.logger.debug("stream closed")
                    break
                if isinstance(frame, Exception):
                    yield process.CommandState("error", frame)
                    raise frame from None
                if stdin or (self.pipe and self.pipe.write):
                    if not stdin:
                        self.logger.debug(f"opening pipe stream for write")
                        stdin = os.fdopen(self.pipe.write, "ab", buffering=0)
                    stdin.write(frame)
                    stdin.flush()  # superfluous, but does no harm.
            yield process.CommandState("ending")
        finally:
            # self.control.remove_channel(self.channel.center_hz)
            if not self.stream_task.cancelled():
                self.stream_task.cancel()
            self.stream_task = None
            yield process.CommandState("teardown")
            self.pipe.close_write()  # releases stdin
        yield process.CommandState("unknown")

    def stream(self, queue_data: Callable[[object], None]) -> None:
        if not self.channel:
            raise ValueError(f"No channel set for {self}")
        try:
            sample_rate = util.best_sample_rate(
                self.channel.width_hz, self.config.get("shoulder", 1.0), self.sample_rates
            )

            def on_data(packet: numpy.ndarray, quality: ka9q.stream.StreamQuality | None = None) -> None:
                if not self.stopping_event.is_set() and packet:
                    try:
                        queue_data(b"".join(packet))
                    except Exception as exc:
                        self.logger.error(f"processing error", exc_info=exc)

            with ka9q.RadiodControl(self.config["status_address"], client_id=f"{self.name}") as control:
                with KA9QManagedStream(
                    control=control,
                    frequency_hz=self.channel.center_hz,
                    sample_rate=sample_rate,
                    encoding=ka9q.Encoding.S16LE,
                    preset="iq",
                    on_samples=on_data,
                    raw_payloads=True,
                    agc_enable=True,
                ) as stream:
                    self.logger.info(f"Managed Stream {stream} from {self.config['status_address']}")
                    self.stopping_event.wait()
                self.logger.debug("ManagedStream ended")
        except Exception as exc:
            self.logger.error(f"Managed Stream problem", exc_info=exc)
            queue_data(exc)
        finally:
            queue_data(None)
            self.stopping_event.set()

    async def stop(self) -> None:
        if self.channel:
            self.stopping_event.set()

    def is_running(self) -> bool:
        return self.stream_task is not None and not self.stream_task.done()

    def describe(self) -> str:
        return f"{self.__class__.__name__} {self.channel} running:{self.is_running()}"

    def on_started(self) -> None:
        pass  # undoes the base implementation, since we want the pipe's write FD to remain open.
