# hfdl_observer/util.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import collections
import collections.abc
import concurrent.futures
import contextlib
import dataclasses
import datetime
import json
import logging
import math
import os
import re
import select
import sys
import termios
import threading
import uuid
from typing import IO, Any, AsyncGenerator, Callable, Coroutine, Union

logger = logging.getLogger(__name__)
thread_local = threading.local()
thread_local.loop = None
MESSAGING_NODE_ID = uuid.uuid4().int
shutdown_event = asyncio.Event()


def tobool(val: Union[bool, str, int]) -> bool:
    val = val.lower() if isinstance(val, str) else val
    if val in ("y", "yes", "t", "true", "on", "1", True, 1):
        return True
    elif val in ("n", "no", "f", "false", "off", "0", False, 0):
        return False
    else:
        raise ValueError("invalid truth value %r" % (val,))


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def timestamp_to_datetime(timestamp: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def datetime_to_timestamp(when: datetime.datetime) -> float:
    return when.timestamp()


HFDL_FRAME_TIME = 32


def pseudoframe(when: datetime.datetime) -> int:
    return int(datetime_to_timestamp(when) // HFDL_FRAME_TIME)


def pseudoframe_timestamp(when: datetime.datetime) -> int:
    return pseudoframe(when) * HFDL_FRAME_TIME


def pseudoframe_from_timestamp(when: int) -> int:
    return int(when // HFDL_FRAME_TIME)


def timestamp_from_pseudoframe(when: int) -> int:
    return when * HFDL_FRAME_TIME


def deserialise_station_table(station_table: str) -> dict:
    # station table is a custom(?) "conf" format. Almost, but not quite, JSON.
    # sed -e 's/(/[/g' -e s'/)/]/g' -e 's/=/:/g' -e 's/;/,/g' -e 's/^\s*\([a-z]\+\) /"\1"/' >> ~/gs.json
    # does most of the conversion, but not quite.
    # first the simple substitutions
    for f, t in [("(", "["), (")", "]"), ("=", ":"), (";", ",")]:
        station_table = station_table.replace(f, t)
    # quote the keys...
    lines = station_table.split("\n")
    for ix, line in enumerate(lines):
        lines[ix] = re.sub(r"^\s*([a-z]+) ", r'"\1"', line).strip()
    # remove trailing commas
    station_table = "{" + "".join(lines).replace(",}", "}").replace(",]", "]").strip(",") + "}"
    # in theory it is now JSON decodable.
    return dict(json.loads(station_table))


def hsv_rgb(hue: float, saturation: float, value: float) -> tuple[float, float, float]:
    i = math.floor(hue * 6)
    f = hue * 6 - i
    p = value * (1 - saturation)
    q = value * (1 - f * saturation)
    t = value * (1 - (1 - f) * saturation)
    r, g, b = [
        (value, t, p),
        (q, value, p),
        (p, value, t),
        (p, q, value),
        (t, p, value),
        (value, p, q),
    ][int(i % 6)]
    return r, g, b


def spectrum_colour(value: int, max_value: int) -> tuple[int, int, int]:
    effective = max_value - min(max(0, value), max_value)
    start_hue = 280
    hue_range = 300
    hue = (start_hue + hue_range * effective / max_value) % 360
    hsv = hsv_rgb(hue / 360, 1, 1)
    return (int(hsv[0] * 255), int(hsv[1] * 255), int(hsv[2] * 255))


def normalize_ranges(ranges: list[int | list[int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for arange in ranges:
        if arange:
            if isinstance(arange, list):
                result.append(tuple((arange + arange[-1:])[:2]))  # type: ignore # I'm too clever for mypy
            else:
                result.append((arange, arange))
    return result


class Pipe:
    read: int
    write: int

    def __init__(self) -> None:
        self.read, self.write = os.pipe()
        # os.set_inheritable(self.read, True)
        # os.set_inheritable(self.write, True)

    def close_read(self) -> None:
        try:
            os.close(self.read)
        except OSError:
            pass

    def close_write(self) -> None:
        try:
            os.close(self.write)
        except OSError:
            pass

    def close(self) -> None:
        self.close_write()
        self.close_read()

    def __enter__(self) -> "Pipe":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def is_bad_file_descriptor(error: OSError) -> bool:
    # for now, naive.
    return "Errno 9" in str(error)


class DeepChainMap(collections.ChainMap):
    def __getitem__(self, key: Any) -> Any:
        values = (mapping[key] for mapping in self.maps if key in mapping)
        try:
            first = next(values)
        except StopIteration:
            return self.__missing__(key)
        if isinstance(first, collections.abc.MutableMapping):
            return self.__class__(first, *values)
        return first

    def __repr__(self) -> str:
        return repr(dict(self))  # decompose to dict-ish

    def dict(self) -> dict:
        d = dict(self)
        for k, v in list(d.items()):
            if hasattr(v, "dict"):
                d[k] = v.dict()
            elif isinstance(v, list):
                d[k] = list(e.dict() if hasattr(e, "dict") else e for e in v)
        return d


def schedule(coro: Coroutine) -> asyncio.Task[Any]:
    loop: asyncio.AbstractEventLoop = thread_local.loop
    return loop.create_task(coro)


def call_soon(fn: Callable, *args: Any) -> asyncio.Handle:
    loop: asyncio.AbstractEventLoop = thread_local.loop
    return loop.call_soon(fn, *args)


def call_soon_threadsafe(fn: Callable, *args: Any) -> asyncio.Handle:
    loop: asyncio.AbstractEventLoop = thread_local.loop
    return loop.call_soon_threadsafe(fn, *args)


def call_later(delay: float, fn: Callable, *args: Any) -> asyncio.TimerHandle:
    loop: asyncio.AbstractEventLoop = thread_local.loop
    return loop.call_later(delay, fn, *args)


async def cleanup_task(task: asyncio.Task) -> None:
    if not task.done() and not task.cancelled() and not task.cancelling():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning(f"{task} produced {exc} on cleanup")


class async_reader(contextlib.AbstractAsyncContextManager):
    transport: asyncio.ReadTransport | None = None

    def __init__(self, openable: IO[Any] | None, close_on_exit: bool = True) -> None:
        self.openable = openable
        self.close_on_exit = close_on_exit

    async def __aenter__(self) -> asyncio.StreamReader | None:
        """Wrap a readable pipe in a stream"""
        if self.openable is None:
            return None
        loop: asyncio.AbstractEventLoop = thread_local.loop
        stream_reader = asyncio.StreamReader(loop=loop)

        def factory() -> asyncio.StreamReaderProtocol:
            return asyncio.StreamReaderProtocol(stream_reader)

        self.transport, _ = await loop.connect_read_pipe(factory, self.openable)
        return stream_reader

    async def __aexit__(self, *exc_info: Any) -> None:
        # closing this on a pipe managed by a subprocess handler *might* be responsible for "detached" EBADFs.
        # TODO: investigate this.
        if self.close_on_exit and self.transport is not None:
            self.transport.close()


async def async_keystrokes(pacing: float = 0) -> AsyncGenerator:
    # This doesn't work well with rich. Rich's response to this seems to be "dunno, don't care".
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    def read_with_timeout() -> str | None:
        if select.select(
            [
                sys.stdin,
            ],
            [],
            [],
            5,
        )[0]:
            return sys.stdin.read(1)
        return None

    try:
        term = termios.tcgetattr(fd)
        # we can't use setraw, since it will mess up Rich
        # tty.setraw(sys.stdin.fileno())
        term[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN | termios.IGNBRK | termios.BRKINT | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSAFLUSH, term)
        pacing_delta = datetime.timedelta(seconds=pacing)
        last_keystroke: datetime.datetime | None = None
        while not is_shutting_down():
            key = await in_thread(read_with_timeout)
            if key is None:
                continue
            if not key:
                logger.debug("no more keystrokes")
                break
            if ord(key) in (3, 4):  # ^C, ^D
                logger.warning("break received")
                shutdown_event.set()
                break
            if pacing > 0:
                when = now()
                if last_keystroke and when - last_keystroke < pacing_delta:
                    continue
                last_keystroke = when
            yield key
    except Exception as err:
        logger.error("keyboard error?", exc_info=err)
        raise err
    finally:
        logger.debug("keyboard loop finished")
        termios.tcsetattr(fd, termios.TCSAFLUSH, old_settings)


class AbstractKeyboard:
    mappings: dict[str, Callable]

    def __init__(self) -> None:
        self.mappings = {}

    def add_mapping(self, key: str, callback: Callable) -> None:
        self.mappings[key] = callback

    def remove_mapping(self, key: str) -> None:
        del self.mappings[key]

    def on_keystroke(self, key: str) -> None:
        try:
            callback = self.mappings[key]
        except KeyError:
            pass
        else:
            call_soon_threadsafe(callback, key)


class AsyncKeyboard(AbstractKeyboard):
    def __init__(self, pacing: float = 0) -> None:
        super().__init__()
        self.pacing = pacing

    async def run(self) -> None:
        keystrokes = async_keystrokes(self.pacing)
        try:
            async for key in keystrokes:
                self.on_keystroke(key)
        except Exception as err:
            logger.error("AsyncKeyboard error", exc_info=err)


Keyboard = AsyncKeyboard


class aclosing(contextlib.AbstractAsyncContextManager):
    # version of contextlib.aclosing that tries to relinquish running state of generator before closing it.
    def __init__(self, thing: AsyncGenerator) -> None:
        self.thing = thing

    async def __aenter__(self) -> AsyncGenerator:
        return self.thing

    async def __aexit__(self, *exc_info: Any) -> None:
        await asyncio.sleep(0)
        await self.thing.aclose()


async def in_thread(func: Callable, *args: Any, **kwargs: Any) -> Any:
    # Runs a function in a separate thread via an executor in the current event loop so it can be awaited.
    if not hasattr(thread_local, "executor"):
        thread_local.executor = concurrent.futures.ThreadPoolExecutor(max_workers=64)
    loop: asyncio.AbstractEventLoop = thread_local.loop

    def run() -> Any:
        return func(*args, **kwargs)

    return await loop.run_in_executor(thread_local.executor, run)


async def in_db_thread(func: Callable, *args: Any, **kwargs: Any) -> Any:
    # Runs a function in a separate thread via an executor in the current event loop so it can be awaited.
    if not hasattr(thread_local, "db_executor"):
        thread_local.db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop: asyncio.AbstractEventLoop = thread_local.loop

    def run() -> Any:
        return func(*args, **kwargs)

    return await loop.run_in_executor(thread_local.db_executor, run)


async def in_ui_thread(func: Callable, *args: Any, **kwargs: Any) -> Any:
    # Runs a function in a separate thread via an executor in the current event loop so it can be awaited.
    if not hasattr(thread_local, "ui_executor"):
        thread_local.ui_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop: asyncio.AbstractEventLoop = thread_local.loop

    def run() -> Any:
        return func(*args, **kwargs)

    return await loop.run_in_executor(thread_local.ui_executor, run)


def is_shutting_down() -> bool:
    shutting_down: bool = shutdown_event.is_set()
    return shutting_down


@dataclasses.dataclass
class Message:
    target: str
    subject: str
    payload: Any
    sender: int = MESSAGING_NODE_ID

    def __str__(self) -> str:
        body: str
        if isinstance(self.payload, str):
            body = self.payload
        else:
            body = self.payload.__class__.__name__
            if hasattr(self.payload, "__len__"):
                body = f"{body} l={len(self.payload)}"
        return f"<Message: t={self.target} s={self.subject} b={body} f={self.sender}>"
