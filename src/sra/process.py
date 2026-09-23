"""Bounded POSIX process supervision. No shell or tmux screen scraping."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .errors import ProcessError


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _terminate(process: subprocess.Popen) -> None:
    # The process group also includes ordinary children after the leader exits.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def execute(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
            stdin: str = "", timeout: float = 300, idle_timeout: float | None = None,
            max_bytes: int = 16_777_216, cancel: Event | None = None,
            on_line: Callable[[str, str], None] | None = None,
            on_start: Callable[[int], None] | None = None,
            heartbeat: Callable[[], None] | None = None) -> ProcessResult:
    if os.name != "posix":
        raise ProcessError("This release requires POSIX process groups (Linux/macOS)")
    if not argv or any("\x00" in x for x in argv):
        raise ProcessError("Invalid argv")
    # A file avoids pipe backpressure/deadlock for long prompts.
    with tempfile.TemporaryFile() as input_file:
        input_file.write(stdin.encode())
        input_file.seek(0)
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=input_file,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
        except OSError as exc:
            raise ProcessError(f"Cannot launch {argv[0]}: {exc.strerror}") from exc
        output = {"stdout": bytearray(), "stderr": bytearray()}
        pending = {"stdout": bytearray(), "stderr": bytearray()}
        start = last_output = last_heartbeat = time.monotonic()
        total = 0
        try:
            if on_start:
                on_start(process.pid)
            with selectors.DefaultSelector() as selector:
                for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if cancel and cancel.is_set():
                        raise ProcessError("cancelled")
                    if now - start > timeout:
                        raise ProcessError("wall-clock timeout")
                    if idle_timeout and now - last_output > idle_timeout:
                        raise ProcessError("output inactivity timeout")
                    if heartbeat and now - last_heartbeat > 5:
                        heartbeat()
                        last_heartbeat = now
                    for key, _ in selector.select(timeout=0.02):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        name = key.data
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if pending[name] and on_line:
                                on_line(name, pending[name].decode("utf-8", errors="replace"))
                            pending[name].clear()
                            continue
                        last_output = time.monotonic()
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProcessError("output byte limit exceeded")
                        output[name].extend(chunk)
                        if on_line:
                            pending[name].extend(chunk)
                            while b"\n" in pending[name]:
                                line, _, remainder = pending[name].partition(b"\n")
                                pending[name] = bytearray(remainder)
                                on_line(name, line.decode("utf-8", errors="replace"))
                            if len(pending[name]) > 1_048_576:
                                raise ProcessError("single output line exceeds 1 MiB")
            return ProcessResult(process.wait(), output["stdout"].decode("utf-8", errors="replace"),
                                 output["stderr"].decode("utf-8", errors="replace"))
        except BaseException:
            _terminate(process)
            raise
        finally:
            for pipe in (process.stdout, process.stderr):
                pipe.close()
