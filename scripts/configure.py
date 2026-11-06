"""Create local Docker account configuration without requiring third-party packages."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def main():
    destination = Path(__file__).resolve().parents[1] / '.local' / 'server.json'
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    if destination.exists():
        config = json.loads(destination.read_text())
        print('更新既有帳號設定；未指定的帳號會保留。')
    else:
        config = {'users': {}, 'session_secret': secrets.token_urlsafe(48)}
    while True:
        username = input('帳號名稱（留空完成）：').strip()
        if not username:
            break
        if len(username) > 128:
            print('帳號不能超過 128 字元。'); continue
        password = getpass.getpass('密碼（至少 12 字元）：')
        confirmation = getpass.getpass('再次輸入密碼：')
        if len(password) < 12 or password != confirmation:
            print('密碼長度不足或兩次輸入不同，請重試。'); continue
        config['users'][username] = hash_password(password)
    if not config['users']:
        raise SystemExit('至少需要一個帳號，未儲存設定。')
    # Parent directory is 0700 on the host; mounted secret is readable by the non-root container.
    fd, temporary = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('設定已儲存至 .local/server.json。請勿提交或分享此檔案。')
    print('執行 docker compose up -d --build；更新帳號後執行 docker compose restart。')


if __name__ == '__main__':
    main()
