"""Check the actual bundled server, not the build Python environment."""
import subprocess
import os
import signal
import time
import urllib.request
from pathlib import Path

binary = Path('dist/FBPostCollector.app/Contents/MacOS/FBPostCollector').resolve()
process = subprocess.Popen([str(binary), '--server-only', '15888'])
try:
    for attempt in range(60):
        if process.poll() is not None:
            raise RuntimeError(f'Bundled server exited: {process.returncode}')
        try:
            with urllib.request.urlopen('http://127.0.0.1:15888/api/ping', timeout=2) as response:
                assert response.status == 200
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('Bundled server did not become ready')
    for route in ('/', '/browser-accounts', '/drive-accounts'):
        with urllib.request.urlopen('http://127.0.0.1:15888' + route, timeout=15) as response:
            assert response.status == 200, route
    print('Bundled macOS server and management pages passed')
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

# Exercise the menu-bar entry point and its bundled server child as well.
desktop = subprocess.Popen([str(binary)], start_new_session=True)
try:
    for attempt in range(60):
        if desktop.poll() is not None:
            raise RuntimeError(f'Desktop exited: {desktop.returncode}')
        try:
            with urllib.request.urlopen('http://127.0.0.1:5199/api/ping', timeout=2) as response:
                assert response.status == 200
            time.sleep(3)
            assert desktop.poll() is None, 'Menu bar process exited'
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('Desktop server did not become ready')
    print('Menu bar entry point and bundled child server passed')
finally:
    os.killpg(desktop.pid, signal.SIGTERM)
    desktop.wait(timeout=10)
