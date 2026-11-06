"""Validate the built Docker image with isolated temporary credentials and volume.
Requires image csv-workspace:onboarding, Docker Compose and httpx.
"""
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
project = 'csv-test-' + secrets.token_hex(6)
with tempfile.TemporaryDirectory(prefix='csv-docker-', dir='/workspace') as folder:
    temporary = Path(folder)
    config = temporary / 'server.json'
    password = secrets.token_urlsafe(24)
    config.write_text(json.dumps({'users': {'tester': password_hash(password)}, 'session_secret': secrets.token_urlsafe(48)}))
    config.chmod(0o444)
    override = temporary / 'override.json'
    override.write_text(json.dumps({'services': {'csv-workspace': {'image': 'csv-workspace:onboarding'}}, 'secrets': {'csv_config': {'file': str(config)}}}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, CSV_BIND_IP='127.0.0.1', CSV_PORT=str(port), CSV_SECURE_COOKIE='0')
    for name in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(name, None)
    command = ['docker', '--host=unix:///var/run/docker.sock', 'compose', '-p', project, '-f', str(root / 'compose.yaml'), '-f', str(override)]
    def compose(*args):
        subprocess.run([*command, *args], env=env, cwd=root, check=True)
    try:
        compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '60')
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
            assert client.get('/healthz').status_code == 200
            assert client.post('/api/login', json={'username': 'tester', 'password': password}).status_code == 200
            response = client.post('/api/datasets', files={'file': ('test.csv', '姓名,代碼\n小林,001\n'.encode(), 'text/csv')})
            assert response.status_code == 202, response.text
            dataset = response.json()['id']
            for _ in range(100):
                response = client.get(f'/api/datasets/{dataset}/rows')
                if response.status_code == 200:
                    break
                time.sleep(.1)
            assert response.json()['rows'][0]['values'] == ['小林', '001']
            assert client.patch(f'/api/datasets/{dataset}/rows/1', json={'column': 0, 'value': 'Docker編輯', 'version': 1}).status_code == 200
            compose('up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '60')
            response = client.get(f'/api/datasets/{dataset}/rows')
            assert response.status_code == 200, response.text
            assert response.json()['rows'][0]['values'] == ['Docker編輯', '001']
            assert client.get(f'/api/datasets/{dataset}/audit').json()[0]['username'] == 'tester'
            assert 'Docker編輯' in client.get(f'/api/datasets/{dataset}/export').text
            print('Docker Compose: health, secret config, login, import, edit, container recreation, persistent volume, audit and export: passed')
    finally:
        # Only the random project created by this test is removed.
        compose('down', '-v')
