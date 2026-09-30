import sys
import threading
import time
import unittest
from fb_collector.services.task_control import configure, TaskCancelled, cancellable_sleep, interruptible_lock
from fb_collector.services.proc import run_hidden


class CancellationTests(unittest.TestCase):
    def tearDown(self):
        configure()

    def test_running_child_is_cancelled(self):
        stop = threading.Event()
        configure(stop.is_set)
        timer = threading.Timer(0.4, stop.set)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(TaskCancelled):
                run_hidden([sys.executable, "-c", "import time; time.sleep(30)"], capture_output=True, timeout=40)
            self.assertLess(time.monotonic() - start, 5)
        finally:
            timer.cancel()

    def test_lock_wait_is_cancelled(self):
        lock = threading.Lock()
        lock.acquire()
        configure(lambda: True)
        try:
            with self.assertRaises(TaskCancelled):
                with interruptible_lock(lock):
                    self.fail("must not acquire")
        finally:
            lock.release()

    def test_sleep_is_cancelled(self):
        configure(lambda: True)
        with self.assertRaises(TaskCancelled):
            cancellable_sleep(20)
