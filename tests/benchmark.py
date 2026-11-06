"""Real HTTP + optional browser smoke test with a generated 200 MiB CSV.
Run with: python tests/benchmark.py (requires httpx; Playwright is optional).
No production credentials or user data are used.
"""
import csv
import io
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import password_hash

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='csv-benchmark-') as folder:
    tmp = Path(folder)
    source = tmp / '200mb.csv'
    count = 1_000_000
    with source.open('w', encoding='utf-8', newline='') as stream:
        stream.write('name,code,notes\n')
        for i in range(count):
            stream.write(f'user{i:07d},{i:09d},' + 'x' * 188 + '\n')
    size = source.stat().st_size
    assert size >= 200 * 1024 * 1024
    print(f'Generated {size:,} bytes, {count:,} rows', flush=True)
    password = secrets.token_urlsafe(24)
    env = {**os.environ, 'CSV_DATA_DIR': str(tmp / 'data'), 'CSV_USERS_JSON': json.dumps({'tester': password_hash(password)}), 'CSV_SESSION_SECRET': secrets.token_urlsafe(48), 'CSV_SECURE_COOKIE': '0'}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    with (tmp / 'server.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port), '--workers', '1'], cwd=root, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=url, timeout=600, trust_env=False) as client:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError((tmp / 'server.log').read_text())
                    try:
                        if client.get('/healthz').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('Server startup timed out')
                assert client.post('/api/login', json={'username': 'tester', 'password': password}).status_code == 200
                started = time.perf_counter()
                with source.open('rb') as file:
                    response = client.post('/api/datasets', files={'file': ('200mb.csv', file, 'text/csv')})
                assert response.status_code == 202, response.text
                dataset = response.json()['id']
                for _ in range(600):
                    info = next(x for x in client.get('/api/datasets').json() if x['id'] == dataset)
                    if info['state'] != 'importing':
                        break
                    time.sleep(.2)
                assert info['state'] == 'ready', info
                assert info['row_count'] == count
                print(f'Upload + import: {time.perf_counter()-started:.2f}s', flush=True)
                path = f'/api/datasets/{dataset}'
                for label, params in [('first page', {}), ('last page', {'offset': 999900}), ('filter', {'column': 1, 'op': 'equals', 'value': '000999999'}), ('text sort', {'sort': 0, 'direction': 'desc'})]:
                    started = time.perf_counter()
                    response = client.get(path + '/rows', params=params)
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert len(result['rows']) <= 100
                    if label == 'filter':
                        assert result['total'] == 1 and result['rows'][0]['values'][1] == '000999999'
                    else:
                        assert result['total'] == count
                    print(f'{label}: {time.perf_counter()-started:.2f}s; returned {len(result["rows"])} rows', flush=True)
                response = client.patch(path + '/rows/1', json={'column': 2, 'value': 'edited from benchmark', 'version': 1})
                assert response.status_code == 200
                exported = 0
                started = time.perf_counter()
                with client.stream('GET', path + '/export') as response:
                    assert response.status_code == 200
                    for chunk in response.iter_bytes():
                        exported += len(chunk)
                assert exported > 200 * 1024 * 1024
                print(f'Full streaming export: {exported:,} bytes in {time.perf_counter()-started:.2f}s', flush=True)
                # Linux process high-water memory (application server, excluding browser/client).
                if Path(f'/proc/{proc.pid}/status').exists():
                    for line in Path(f'/proc/{proc.pid}/status').read_text().splitlines():
                        if line.startswith(('VmHWM:', 'VmRSS:')):
                            print('Server ' + line, flush=True)
                if os.getenv('CSV_BROWSER_TEST') == '1':
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as p:
                        browser = p.chromium.launch(executable_path=os.getenv('CSV_CHROMIUM_PATH', '/usr/bin/chromium'), headless=True, args=['--no-sandbox'])
                        page = browser.new_page(viewport={'width': 1440, 'height': 960})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(url)
                        page.locator('[name=username]').fill('tester')
                        page.locator('[name=password]').fill(password)
                        page.get_by_role('button', name='登入工作台').click()
                        page.locator('.dataset-button').first.click()
                        page.locator('#table-body tr').first.wait_for()
                        assert page.locator('#table-body tr').count() == 100
                        page.locator('#search').fill('user0999999')
                        page.get_by_role('button', name='套用篩選').click()
                        page.wait_for_function("document.getElementById('result-count').textContent === '1 筆符合條件'")
                        page.locator('#table-body td button').first.click()
                        page.locator('#edit-value').fill('browser edit')
                        page.get_by_role('button', name='儲存修改', exact=True).click()
                        page.wait_for_function("!document.getElementById('edit-dialog').open")
                        page.locator('#clear-filters').click()
                        page.wait_for_function("document.getElementById('result-count').textContent === '1,000,000 筆符合條件'")
                        page.screenshot(path='/tmp/csv-workspace.png', full_page=True)
                        assert not errors, errors
                        browser.close()
                        print('Browser login, pagination, filter and edit: passed; screenshot /tmp/csv-workspace.png', flush=True)
        finally:
            proc.terminate()
            proc.wait(timeout=15)
