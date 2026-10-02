"""Run inside the built image with mounted data/cache/log volumes, without credentials."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

sys.path.insert(0, '/app')
from picker_cache import PickerCache


def main():
    assert (os.getuid(), os.getgid()) == (99, 100), 'Expected Unraid UID/GID 99:100'
    assert not Path('/app/config.py').exists(), 'Local configuration leaked into image'
    assert not Path('/app/.venv').exists(), 'Local virtualenv leaked into image'
    persisted = os.environ.get('SMOKE_EXPECT_PERSISTED') == '1'
    for directory in ('APP_STATE_DIR', 'CACHE_DIR', 'LOG_DIR'):
        marker = Path(os.environ[directory]) / 'container-smoke.txt'
        if persisted:
            assert marker.read_text() == '99:100', f'{directory} did not persist'
        marker.write_text('99:100')

    process = subprocess.Popen([sys.executable, '/app/app.py'])
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, 'Application exited before startup'
            try:
                with urlopen('http://127.0.0.1:5087/processed-items', timeout=2) as response:
                    assert json.load(response)['items'] == []
                break
            except URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)

        database = Path(os.environ['APP_STATE_DIR']) / 'poster_manager.sqlite3'
        with sqlite3.connect(database) as db:
            if persisted:
                row = db.execute("SELECT value FROM settings WHERE namespace='smoke' AND key='persisted'").fetchone()
                assert row == ('true',), 'SQLite state did not persist'
            db.execute("INSERT OR REPLACE INTO settings VALUES ('smoke', 'persisted', 'true')")
        assert (Path(os.environ['LOG_DIR']) / 'app.log').exists()
        cache = PickerCache(str(Path(os.environ['CACHE_DIR']) / 'picker-v2'))
        if persisted:
            assert cache.get('smoke') == {'persisted': True}, 'Picker cache did not persist'
        cache.put('smoke', {'persisted': True})

        options = Options()
        options.binary_location = os.environ['CHROME_BINARY']
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        with webdriver.Chrome(options=options) as driver:
            driver.set_page_load_timeout(10)
            driver.get('data:text/html,<title>Container smoke test</title>')
            assert driver.title == 'Container smoke test'
        print('PASS: startup on custom port, volume writes/persistence, SQLite, and Selenium as 99:100')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == '__main__':
    main()
