"""Optional browser smoke test. Requires httpx, Playwright and Chromium."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import password_hash

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='csv-browser-') as folder:
    password = secrets.token_urlsafe(24)
    env = {**os.environ, 'CSV_DATA_DIR': folder, 'CSV_USERS_JSON': json.dumps({'tester': password_hash(password)}), 'CSV_SESSION_SECRET': secrets.token_urlsafe(48), 'CSV_SECURE_COOKIE': '0'}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    with open(Path(folder) / 'server.log', 'w') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=root, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=url, trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get('/healthz').status_code == 200:
                            break
                    except httpx.ConnectError:
                        time.sleep(.1)
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=os.getenv('CSV_CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
                    page = browser.new_page(viewport={'width': 1440, 'height': 960})
                    errors = []
                    page.on('pageerror', lambda error: (errors.append(str(error)), print('PAGE ERROR:', str(error), flush=True)))
                    page.on('response', lambda response: print('HTTP:', response.status, response.url, flush=True) if '/api/' in response.url else None)
                    page.goto(url)
                    page.locator('[name=username]').fill('tester')
                    page.locator('[name=password]').fill(password)
                    page.get_by_role('button', name='登入工作台').click()
                    page.locator('#empty-upload').click()
                    page.locator('#file').set_input_files({'name': '測試資料.csv', 'mimeType': 'text/csv', 'buffer': '姓名,代碼,備註\n小林,001,台北\n小陳,002,台中\n小王,003,高雄\n'.encode()})
                    page.get_by_role('button', name='上傳並匯入').click()
                    try:
                        page.wait_for_function("document.getElementById('result-count').textContent === '3 筆符合條件'", timeout=10000)
                    except Exception:
                        print(page.locator('body').inner_text(), flush=True)
                        raise
                    assert page.locator('#table-body tr').count() == 3
                    page.locator('#search').fill('小陳')
                    page.get_by_role('button', name='套用篩選').click()
                    page.wait_for_function("document.getElementById('result-count').textContent === '1 筆符合條件'")
                    page.locator('#table-body td button').last.click()
                    page.locator('#edit-value').fill('更新備註')
                    page.get_by_role('button', name='儲存修改', exact=True).click()
                    page.wait_for_function("!document.getElementById('edit-dialog').open")
                    page.locator('#table-body td button').filter(has_text='更新備註').wait_for()
                    page.locator('#table-body td button').filter(has_text='更新備註').click()
                    page.get_by_role('button', name='復原上一筆修改').click()
                    page.wait_for_function("!document.getElementById('edit-dialog').open")
                    page.locator('#table-body td button').filter(has_text='台中').wait_for()
                    page.locator('#clear-filters').click()
                    page.wait_for_function("document.getElementById('result-count').textContent === '3 筆符合條件'")
                    page.locator('#history').click()
                    page.wait_for_function("document.querySelectorAll('.audit-item').length === 2")
                    page.locator('[data-close=history-dialog]').click()
                    with page.expect_download() as download:
                        page.locator('#export').click()
                    assert download.value.suggested_filename.endswith('.csv')
                    page.screenshot(path='/tmp/csv-workspace.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.screenshot(path='/tmp/csv-workspace-mobile.png', full_page=True)
                    assert not errors, errors
                    browser.close()
                    print('Browser: login, upload, auto refresh, filter, edit, undo, audit, export: passed')
        finally:
            proc.terminate()
            proc.wait(timeout=15)
