import os
import subprocess
import time
from .task_control import check_cancelled


def hidden_popen_kwargs():
    # Python CLI tools otherwise inherit the Windows GBK console encoding.
    kwargs = {"env": {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def run_hidden(command, **kwargs):
    merged = hidden_popen_kwargs()
    merged.update(kwargs)
    if os.name == "nt":
        merged["creationflags"] = subprocess.CREATE_NO_WINDOW | int(merged.get("creationflags") or 0)
        if "startupinfo" not in kwargs:
            merged["startupinfo"] = hidden_popen_kwargs()["startupinfo"]
    timeout = merged.pop("timeout", None)
    check = merged.pop("check", False)
    input_data = merged.pop("input", None)
    if merged.pop("capture_output", False):
        merged["stdout"] = subprocess.PIPE
        merged["stderr"] = subprocess.PIPE
    merged.setdefault("stdin", subprocess.PIPE if input_data is not None else subprocess.DEVNULL)
    check_cancelled()
    started = time.monotonic()
    with subprocess.Popen(command, **merged) as proc:
        try:
            while True:
                check_cancelled()
                remaining = None if timeout is None else timeout - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = proc.communicate(input=input_data, timeout=min(0.25, remaining) if remaining is not None else 0.25)
                    break
                except subprocess.TimeoutExpired:
                    input_data = None
            check_cancelled()
        except BaseException:
            if os.name == "nt" and proc.poll() is None:
                # Console-script launchers can have a Python child; stop only this owned tree.
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, **hidden_popen_kwargs())
                except (OSError, subprocess.SubprocessError):
                    pass
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
            raise
        result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
