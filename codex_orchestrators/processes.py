"""Bounded pipe transport and process-tree cleanup (standard library only)."""

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    duration: float


class WindowsJob:
    """Kill-on-close job; assignment failure prevents further processing.

    Assignment happens immediately after Popen, not atomically at creation.
    This is cleanup for a trusted CLI, not containment of a hostile executable.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Cannot create process cleanup job")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("Cannot configure process cleanup job")
        if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise OSError("Cannot assign process cleanup job")

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_process(
    command: list[str], *, cwd: Path, env: dict[str, str], input_text: str = "",
    timeout: float = 600, max_output_bytes: int = 2 * 1024 * 1024,
    cancel: threading.Event | None = None,
) -> ProcessResult:
    """No shell; bounded input, output, wall time, and daemon pipe readers."""
    if not 0 < timeout <= 86400 or not 1 <= max_output_bytes <= 16 * 1024 * 1024:
        raise ValueError("Invalid process limits")
    data = input_text.encode("utf-8")
    if len(data) > 1024 * 1024:
        raise ValueError("Prompt exceeds 1 MiB limit")
    started = time.monotonic()
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    job = None
    stopped = threading.Event()
    chunks: queue.Queue = queue.Queue(maxsize=64)
    output = {"out": bytearray(), "err": bytearray()}
    error = ""
    code = 1

    def send(kind: str, chunk: bytes | None) -> None:
        while not stopped.is_set():
            try:
                chunks.put((kind, chunk), timeout=0.05)
                return
            except queue.Full:
                pass

    def reader(pipe, kind: str) -> None:
        try:
            while not stopped.is_set():
                chunk = pipe.read1(4096)
                if not chunk:
                    break
                send(kind, chunk)
        except (OSError, ValueError):
            pass
        finally:
            send(kind, None)

    def writer() -> None:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    threads: list[threading.Thread] = []
    try:
        if os.name == "nt":
            job = WindowsJob(proc)
        for pipe, kind in ((proc.stdout, "out"), (proc.stderr, "err")):
            thread = threading.Thread(target=reader, args=(pipe, kind), daemon=True)
            threads.append(thread)
            thread.start()
        thread = threading.Thread(target=writer, daemon=True)
        threads.append(thread)
        thread.start()
        done: set[str] = set()
        size = 0
        while len(done) < 2 or proc.poll() is None:
            if cancel is not None and cancel.is_set():
                error, code = "interrupted", -2
                break
            if time.monotonic() - started >= timeout:
                error, code = "process timed out", -1
                break
            try:
                kind, chunk = chunks.get(timeout=0.05)
            except queue.Empty:
                continue
            if chunk is None:
                done.add(kind)
            else:
                size += len(chunk)
                if size > max_output_bytes:
                    error, code = "output limit exceeded", 1
                    break
                output[kind].extend(chunk)
        else:
            code = proc.returncode
    except KeyboardInterrupt:
        error, code = "interrupted", -2
    finally:
        stopped.set()
        if os.name == "nt":
            if job:
                job.close()
            elif proc.poll() is None:
                proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                # Bound graceful cleanup even if a descendant ignores SIGTERM.
                time.sleep(0.05)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=1)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
    stdout = output["out"].decode("utf-8", errors="replace")
    stderr = error or output["err"].decode("utf-8", errors="replace")
    return ProcessResult(stdout, stderr, code, time.monotonic() - started)
