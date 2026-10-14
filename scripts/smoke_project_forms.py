"""Real Chrome regression: no-referrer breaks native POST; same-origin fixes it.

Uses a temporary database and fresh headless browser; never reads user accounts.
Optional CHROMEDRIVER environment variable selects an already installed driver.
"""
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from werkzeug.serving import make_server

from run_tests import MemoryVault


def main():
    with tempfile.TemporaryDirectory(prefix="collector-form-smoke-") as folder:
        os.environ["FB_COLLECTOR_DATA_DIR"] = folder
        from fb_collector import credential_store, db
        from fb_collector.app import create_app
        with patch.object(credential_store, "_backend", return_value=MemoryVault()):
            app = create_app()
            observed = []

            @app.after_request
            def observe(response):
                from flask import request
                if request.method == "POST":
                    observed.append((request.path, request.headers.get("Origin"), response.status_code))
                return response

            server = make_server("127.0.0.1", 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            options = webdriver.ChromeOptions()
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1600,1000")
            options.add_argument(f"--user-data-dir={folder}/browser")
            options.add_argument("--disable-background-networking")
            driver = None
            try:
                service = Service(os.environ["CHROMEDRIVER"]) if os.environ.get("CHROMEDRIVER") else Service()
                driver = webdriver.Chrome(service=service, options=options)
                driver.set_page_load_timeout(40)
                base = f"http://127.0.0.1:{server.server_port}"
                driver.get(base)
                # Reproduce the previous page policy in an actual browser.
                driver.execute_script("const m=document.createElement('meta'); m.name='referrer'; m.content='no-referrer'; document.head.appendChild(m);")
                Select(driver.find_element(By.NAME, "project_type")).select_by_value("post_lookup")
                driver.find_element(By.CSS_SELECTOR, 'form[action="/projects/new"] button').click()
                WebDriverWait(driver, 10).until(lambda _: len(observed) == 1)
                assert observed[-1][1:] == ("null", 403), observed
                assert not db.list_projects()
                print("Reproduced old policy: native project form sends Origin:null and receives 403")
                for mode in ("post_lookup", "post", "page"):
                    driver.get(base)
                    Select(driver.find_element(By.NAME, "project_type")).select_by_value(mode)
                    driver.find_element(By.CSS_SELECTOR, 'form[action="/projects/new"] button').click()
                    WebDriverWait(driver, 10).until(lambda d: bool(d.find_elements(By.ID, "project-form")))
                    assert observed[-1][1:] == (base, 302), observed[-1]
                    assert driver.find_element(By.ID, "project-type").get_attribute("value") == mode
                    if mode == "post_lookup":
                        assert driver.find_element(By.NAME, "post_id_worksheet_name").is_displayed()
                        assert driver.find_element(By.NAME, "page_output_worksheet_name").get_attribute("value") == "采集结果"
                    print(f"PASS: native {mode} creation and edit-page redirect")
                assert len(db.list_projects()) == 3
            finally:
                if driver:
                    driver.quit()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    main()
