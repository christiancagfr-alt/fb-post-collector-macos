"""Smoke-test the actual Windows bundle with isolated application data."""
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

binary = Path("dist/FBPostCollector/FBPostCollector.exe").resolve()
with tempfile.TemporaryDirectory(prefix="collector-smoke-") as directory:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, FB_COLLECTOR_DATA_DIR=directory)
    process = subprocess.Popen([str(binary), "--server-only", str(port)], env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        base = f"http://127.0.0.1:{port}"
        for attempt in range(60):
            if process.poll() is not None:
                raise RuntimeError(f"Bundled server exited: {process.returncode}")
            try:
                with urllib.request.urlopen(base + "/api/ping", timeout=2) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Bundled server did not become ready")
        for route in ("/", "/browser-accounts", "/drive-accounts", "/settings"):
            with urllib.request.urlopen(base + route, timeout=15) as response:
                assert response.status == 200, route
        print("Bundled Windows server and management pages passed")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
