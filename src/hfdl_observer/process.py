# hfdl_observer/hfdl.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#
# flake8: noqa [W503]

import asyncio
import asyncio.subprocess
import contextlib
import dataclasses
import errno
import os
import re
import shlex
import signal

# for thread based execution.
import subprocess  # nosec # shell usage is correctly handled.

from copy import copy
from typing import Any, AsyncGenerator, AsyncIterator, Optional

import logging

import hfdl_observer.util as util


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class CommandState:
    event: str
    extra: Optional[Any] = None
    # process: Optional[asyncio.subprocess.Process] = None
    process: Optional[subprocess.Popen] = None


class Command:
    # process: Optional[asyncio.subprocess.Process] = None
    process: Optional[subprocess.Popen] = None
    execution_event: Optional[asyncio.Event] = None
    unrecoverable_errors: list[str]
    recoverable_errors: list[str]
    recoverable_error_count: int = 0
    recoverable_error_limit: int = 10
    fire_once: bool = True
    current_state: CommandState | None = None
    watcher_task: None | asyncio.Task = None

    def __init__(
        self,
        parent_logger: logging.Logger,
        cmd: list[str],
        execution_arguments: dict = {},
        shell: bool = False,
        fire_once: bool = True,
        recoverable_errors: Optional[list[str]] = None,
        unrecoverable_errors: Optional[list[str]] = None,
        valid_return_codes: Optional[list[int]] = None,
    ) -> None:
        if not cmd:
            raise ValueError("no command arguments specified")
        self.cmd = cmd
        self.execution_arguments = copy(execution_arguments)
        self.prepare_event = asyncio.Event()
        self.logger = parent_logger
        self.shell = shell
        self.fire_once = fire_once
        self.unrecoverable_errors = unrecoverable_errors or []
        self.recoverable_errors = recoverable_errors or []
        self.valid_return_codes = valid_return_codes or [0]
        self.process_logger = self.logger

    class StdErrWatcher:
        recoverable_error_count = 0
        recoverable_error_limit = 0

    def check_alive(self) -> bool:
        if self.process:
            pid = self.process.pid
            try:
                # If running, this will do nothing on POSIX. If not running, will raise exception:
                # doing it through the process object could give false results.
                # On Windows it hypotheticallyl could generate a CTRL_BREAK, but there are so many other POSIX
                # assumptions in this already, it's unlikely you'd get this far running on Windows.
                os.kill(pid, 0)
            except OSError as err:
                if err.errno == errno.EPERM:
                    # There is a process, but we don't have permission to send signals to it.
                    return True
                elif err.errno != errno.ESRCH:
                    raise
            else:
                return True
        # otherwise, it *might* be in the preparing state. If it's not then it's not really alive.
        elif self.current_state and self.current_state.event == "preparing":
            return True
        # Command is dead. Make it so.
        self.logger.debug(f"{self} appears dead. Stopping/cleaning up")
        util.schedule(self.stop())
        return False

    def is_running(self) -> bool:
        return self.current_state is not None and self.current_state.event not in ["error", "done"]

    async def lifecycle(self) -> AsyncGenerator:
        self.logger.debug("executing command")
        retcode: int | None = None
        try:
            # remember asend and athrow!
            cmd = self.cmd
            yield CommandState("preparing")
            self.logger.debug("gathering options")
            exec_args: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
            }
            exec_args.update(self.execution_arguments)
            shell_cmd = shlex.join(cmd)
            self.logger.debug(f"$ `{shell_cmd}`")

            for tries in range(3):
                try:
                    # Cannot use asyncio.subprocess. In testing, some odd behaviour resulted in these calls getting
                    # blocked on os.waitpid, which halts the entire event loop. The reasons for this aren't exactly
                    # clear, but there may be a subtle error with pipe handling that causes (eg) kiwirecorder stdout
                    # to be sent to stderr, which fills up the internal buffer and triggers the waitpid call. Since the
                    # (kiwirecorder) process is working fine, it doesn't end in any reasonable time, and thus os.waitpid
                    # blocks the event loop.
                    #
                    # Instead, we perform the process related in a different thread (using a thread/loop local executor
                    # and a custom thread pool) both the process creation call (Popen) and the resulting main wait()
                    # need to be performed in the executor.
                    #
                    # previous (in-loop) it looked like this:
                    # if self.shell:
                    #     spawner = asyncio.subprocess.create_subprocess_shell(shell_cmd, **exec_args)
                    # else:
                    #     spawner = asyncio.subprocess.create_subprocess_exec(*cmd, **exec_args)

                    # or we can just wrap Popen() and process.wait() in asyncio.to_thread calls...
                    if self.shell:
                        exec_args["shell"] = True
                    spawner = util.in_thread(subprocess.Popen, cmd, **exec_args)
                    process = await asyncio.wait_for(spawner, 5)

                    break
                except OSError as ose:
                    if not util.is_bad_file_descriptor(ose):
                        raise
                    self.logger.debug(f"retrying {tries} (descriptor)")
                except asyncio.TimeoutError:
                    self.logger.debug(f"retrying {tries} (timeout)")
            else:
                raise ValueError("Could not start process after 3 tries")
            self.process = process
            self.process_logger = self.logger.getChild(f"pid#{process.pid}")
            self.process_logger.debug("process started")
            yield CommandState("running", process.pid, process=process)

            try:
                async with util.async_reader(process.stderr, close_on_exit=False) as stderr_reader:
                    async with self.watch_stderr(process.pid, stderr_reader) as stderr_task:
                        self.process_logger.debug(f"launched {stderr_task}")
                        retcode = await util.in_thread(process.wait)
                if retcode not in self.valid_return_codes:
                    self.process_logger.debug(f"exited with {retcode}")
                else:
                    self.process_logger.info(f"exited with {retcode}")
            except asyncio.CancelledError:
                self.process_logger.debug("mapping cancellation to exit")
            except Exception as e:
                self.process_logger.info(f"process {process.pid} aborted.", exc_info=e)
                raise
        except Exception as exc:
            yield CommandState("error", exc, process=self.process)
        finally:
            yield CommandState("done", retcode, process=self.process)
            self.process = None

    async def execute(self) -> AsyncGenerator:
        if self.execution_event:
            self.logger.info("Command has been executed, joining its execution")
            await self.execution_event.wait()
            return
        self.current_state = CommandState("starting")
        self.execution_event = execution_event = asyncio.Event()
        try:
            async with util.aclosing(self.lifecycle()) as lifecycle:
                async for command_state in lifecycle:
                    self.current_state = command_state
                    match command_state.event:
                        case "error":
                            # we might want to ignore errors.
                            self.process_logger.info("encountered error", exc_info=command_state.extra)
                            if isinstance(command_state.extra, KeyboardInterrupt):
                                raise command_state.extra
                            else:
                                self.process_logger.debug("continuing")
                    yield self.current_state
        finally:
            execution_event.set()

    # async def signal(self, sig: int, process: Optional[asyncio.subprocess.Process] = None, timeout: int = 5) -> None:
    async def signal(self, sig: int, process: Optional[subprocess.Popen] = None, timeout: int = -1) -> None:
        if timeout < 0:
            timeout = 1 if util.is_shutting_down() else 3
        execution_event = self.execution_event
        process = process or self.process
        if process and execution_event and not execution_event.is_set():
            try:
                pid = process.pid
                try:
                    logger.info(f"signal {sig} to pid {pid}")
                    # department of redundancy department
                    process.send_signal(sig)
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                awaitables = [util.in_thread(process.wait, timeout)]
                if execution_event is not None:
                    awaitables.append(asyncio.wait_for(execution_event.wait(), timeout=timeout))
                for result in await asyncio.gather(*awaitables, return_exceptions=True):
                    if isinstance(result, asyncio.TimeoutError):
                        self.process_logger.debug(f"signal {sig} may have been ignored.")
            except asyncio.CancelledError:
                self.process_logger.debug(f"signal {sig} cancelled")
            except Exception as err:
                self.process_logger.debug(f"signal {sig} interrupted", exc_info=err)
        else:
            logger.debug(f"signal {sig} is a no op... {process}, {execution_event}")

    async def kill(self) -> None:
        await self.signal(signal.SIGKILL)

    async def terminate(self) -> None:
        await self.signal(signal.SIGTERM)
        await self.kill()

    async def stop(self) -> None:
        await self.signal(signal.SIGINT)
        await self.terminate()

    @contextlib.asynccontextmanager
    async def watch_stderr(self, pid: int, stream: Optional[asyncio.StreamReader]) -> AsyncIterator[asyncio.Task]:
        if stream is None:
            self.process_logger.warning(f"{self} no stderr stream to watch")
        else:
            self.process_logger.debug("starting error watcher")
            self.watcher_task = util.schedule(self._watch_stderr(pid, stream))
            try:
                yield self.watcher_task
            finally:
                self.process_logger.debug(f"{self} cleanup stderr watcher")
                await util.cleanup_task(self.watcher_task)

    async def _watch_stderr(self, pid: int, stream: Optional[asyncio.StreamReader]) -> None:
        stream_logger = None
        if stream:
            stream_logger = self.process_logger or self.logger or logger
            try:
                logger.debug(f"{self} listening to {stream}")
                async for data in stream:
                    try:
                        line = data.decode("utf8").rstrip()
                    except UnicodeDecodeError:
                        continue
                    stream_logger.info(line)
                    if any(re.search(pattern, line) for pattern in self.unrecoverable_errors):
                        stream_logger.warning(f'encountered unrecoverable error: "{line}".')
                        # terminate, subclasses can restart process if desired.
                        await self.stop()
                        break
                    if any(re.search(pattern, line) for pattern in self.recoverable_errors):
                        self.recoverable_error_count += 1
                        stream_logger.debug(f"recoverable error detected. Current count {self.recoverable_error_count}")
                        if self.recoverable_error_count > self.recoverable_error_limit:
                            stream_logger.warning(f"received too many recoverable errors `{line}`.")
                            await self.stop()
                            break
                    if not self.process or (self.execution_event and self.execution_event.is_set()):
                        break
                    await asyncio.sleep(0)
            except OSError as err:
                if util.is_bad_file_descriptor(err):
                    # file was closed elsewhere. should mean the watched process exited as well.
                    self.logger.info(f"{err} on error watcher. ending process.")
                    await self.stop()
                else:
                    raise
        (stream_logger or self.logger).debug(f"finished watching {stream}")

    def reset_recoverable_error_count(self, *_: Any) -> None:
        self.recoverable_error_count = 0

    def describe(self) -> str:
        pid_str: str = ""
        if self.process:
            pid_str = f"[pid: {self.process.pid}]"
        cmd_str = ""
        if self.cmd:
            cmd_str = str(self.cmd[0])
        state = "unknown"
        if self.current_state:
            state = self.current_state.event
        return f"{state} {cmd_str} {pid_str}"

    def __del__(self) -> None:
        if self.watcher_task:
            self.watcher_task.cancel()


class ProcessHarness:
    logger: logging.Logger
    settle_time: float = 0
    backoff_time: float = 0
    command: Optional[Command] = None
    previous_pids: list[int]
    starting_event: asyncio.Event

    def __init__(self) -> None:
        self.logger = logging.getLogger(str(self))
        self.previous_pids = []

    def create_command(self) -> Command:
        raise NotImplementedError()

    async def on_prepare(self) -> None:
        for pid in self.previous_pids[:]:
            # clean up any previous pids. This should not be necessary, but it seems that some USB based radios can
            # linger past exit.
            try:
                self.logger.debug(f"will cleanup pid {pid}")
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                self.logger.debug(f"pid {pid} cleared")
                self.previous_pids.remove(pid)
            except OSError as err:
                self.logger.info(f"pid {pid} could not be cleared at this time. {err}")
        if self.settle_time:
            self.logger.info(f"settling for {self.settle_time} seconds")
            await asyncio.sleep(self.settle_time)
        if self.backoff_time:
            self.logger.info(f"additional backoff: {self.backoff_time} seconds")
            await asyncio.sleep(self.backoff_time)
            self.backoff_time = 0

    def on_running(self, process: asyncio.subprocess.Process, context: Any) -> None:
        self.logger.debug(f"with pid {process.pid}")
        self.previous_pids.append(process.pid)

    def is_running(self) -> bool:
        return self.command is not None and self.command.check_alive() and self.command.is_running()

    def cleanup(self) -> None:
        logger.debug(f"cleanup {self.command}")
        self.command = None

    async def run(self) -> AsyncGenerator:
        if self.command:
            logger.info(f"{self} stopping existing command")
            await self.command.stop()
        try:
            pid = None
            command: Command = self.create_command()
        except ValueError:
            logger.info("no command to execute.")
        else:
            self.command = command
            logger.debug(f"{self} command set to {command}")
            await asyncio.sleep(0)
            try:
                async with util.aclosing(command.execute()) as executor:
                    async for current_state in executor:
                        self.logger.debug(f"reached {current_state.event}")
                        match current_state.event:
                            case "preparing":
                                await self.on_prepare()
                            case "running":
                                pid = current_state.process.pid
                                self.on_running(current_state.process, current_state.extra)
                        yield current_state
                    await asyncio.sleep(0)
                    logger.debug(f"{self} lifecycle exited")
            except RuntimeError as err:
                if "coroutine ignored GeneratorExit" not in str(err):
                    raise
                self.logger.info(str(err))
        finally:
            self.logger.debug(f"{command} lifecycle ended ({pid})")
            self.cleanup()

    async def stop(self) -> None:
        if self.command:
            await self.command.stop()
        else:
            logger.debug(f"{self} has no command, stop ignored.")
        return None

    async def kill(self) -> None:
        if self.command:
            await self.command.kill()
        return None

    def reset_recoverable_error_count(self, *_: Any) -> None:
        self.recoverable_error_count = 0

    def describe(self) -> str:
        if self.command:
            return self.command.describe()
        return f"{self.__class__.__name__} not running"
