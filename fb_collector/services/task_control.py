import threading
import time
from contextlib import contextmanager

_local = threading.local()


class TaskCancelled(BaseException):
    """Cancellation must not be swallowed by optional-media error handlers."""


def configure(stop=None, report=None):
    _local.stop = stop
    _local.report = report


def check_cancelled():
    stop = getattr(_local, "stop", None)
    if stop and stop():
        raise TaskCancelled()


def stage(message):
    check_cancelled()
    report = getattr(_local, "report", None)
    if report:
        report(message)


def cancellable_sleep(seconds):
    until = time.monotonic() + max(0, seconds)
    while time.monotonic() < until:
        check_cancelled()
        time.sleep(min(0.2, max(0, until - time.monotonic())))
    check_cancelled()


@contextmanager
def interruptible_lock(lock):
    while not lock.acquire(timeout=0.2):
        check_cancelled()
    try:
        check_cancelled()
        yield
    finally:
        lock.release()
