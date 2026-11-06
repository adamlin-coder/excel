import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA = Path(os.getenv('CSV_DATA_DIR', '.data')).resolve()
MAX_BYTES = int(os.getenv('CSV_MAX_UPLOAD_MB', '1024')) * 1024 * 1024
STATIC = Path(__file__).parent / 'static'
csv.field_size_limit(16 * 1024 * 1024)
IMPORT_LOCK = threading.Lock()


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def password_matches(password, encoded):
    try:
        kind, rounds, salt, expected = encoded.split('$')
        if kind != 'pbkdf2_sha256' or not 100000 <= int(rounds) <= 2000000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def connect(dataset):
    try:
        if str(uuid.UUID(dataset)) != dataset:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, '找不到資料集')
    path = DATA / f'{dataset}.sqlite3'
    if not path.is_file():
        raise HTTPException(404, '找不到資料集')
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    return db


def metadata(db):
    return json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])


def set_metadata(db, info):
    db.execute('UPDATE metadata SET value=?', (json.dumps(info, ensure_ascii=False),))
    db.commit()


def import_csv(dataset):
    # Serialize imports so simultaneous uploads cannot exhaust memory or disk I/O.
    with IMPORT_LOCK:
        db = connect(dataset)
        info = metadata(db)
        source = DATA / f'{dataset}.upload'
        try:
            with source.open('r', encoding=info['encoding'], newline='') as stream:
                sample = stream.read(65536)
                stream.seek(0)
                delimiter = info['delimiter']
                if delimiter == 'auto':
                    try:
                        delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
                    except csv.Error:
                        delimiter = ','
                reader = csv.reader(stream, delimiter=delimiter, strict=True)
                headers = next(reader, None)
                if not headers or len(headers) > 500:
                    raise ValueError('CSV 必須含標題列，且欄位數需為 1–500')
                names = [f'c{i}' for i in range(len(headers))]
                db.execute('CREATE TABLE rows (id INTEGER PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1, ' + ','.join(f'{c} TEXT NOT NULL' for c in names) + ')')
                db.execute('CREATE TABLE audit (id INTEGER PRIMARY KEY, row_id INTEGER, column_index INTEGER, old_value TEXT, new_value TEXT, username TEXT, changed_at INTEGER)')
                sql = 'INSERT INTO rows (' + ','.join(names) + ') VALUES (' + ','.join('?' for _ in names) + ')'
                count, batch = 0, []
                info.update(columns=headers, delimiter=delimiter)
                for row in reader:
                    if not row:
                        continue
                    if len(row) != len(headers):
                        raise ValueError(f'第 {reader.line_num} 行有 {len(row)} 欄，應為 {len(headers)} 欄')
                    batch.append(row)
                    count += 1
                    if len(batch) == 1000:
                        db.executemany(sql, batch)
                        batch.clear()
                        info['row_count'] = count
                        set_metadata(db, info)
                if batch:
                    db.executemany(sql, batch)
                info.update(state='ready', row_count=count, error=None)
                set_metadata(db, info)
        except Exception as exc:
            db.rollback()
            db.execute('DROP TABLE IF EXISTS rows')
            db.execute('DROP TABLE IF EXISTS audit')
            info.update(state='failed', error=str(exc) if isinstance(exc, (ValueError, UnicodeError, csv.Error)) else '匯入失敗，請檢查伺服器磁碟與日誌')
            set_metadata(db, info)
        finally:
            db.close()
            source.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    config_path = os.getenv('CSV_CONFIG_FILE')
    config = json.loads(Path(config_path).read_text()) if config_path else {}
    users = config.get('users') or json.loads(os.getenv('CSV_USERS_JSON', '{}'))
    key = config.get('session_secret') or os.getenv('CSV_SESSION_SECRET', '')
    if not users or len(key) < 32:
        raise RuntimeError('Set CSV_USERS_JSON (username: PBKDF2 hash) and CSV_SESSION_SECRET (at least 32 characters); see README.md')
    app.state.users = users
    app.state.key = key.encode()
    app.state.attempts = {}
    for path in DATA.glob('*.sqlite3'):
        db = connect(path.stem)
        try:
            info = metadata(db)
            if info['state'] == 'importing':
                info.update(state='failed', error='伺服器在匯入期間重啟，請重新上傳')
                set_metadata(db, info)
                (DATA / f'{path.stem}.upload').unlink(missing_ok=True)
        finally:
            db.close()
    yield


app = FastAPI(title='CSV 工作台', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=STATIC), name='static')


def user(request: Request):
    token = request.cookies.get('csv_session', '')
    try:
        payload, signature = token.rsplit('.', 1)
        expected = hmac.new(request.app.state.key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if data['exp'] < time.time() or data['user'] not in request.app.state.users:
            raise ValueError()
        return data['user']
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, '請先登入')


class Login(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=1024)


@app.post('/api/login')
def login(body: Login, request: Request):
    # Bounded rate limit per source address (use a gateway rate limit in production).
    now = time.time()
    attempts = request.app.state.attempts
    for ip in list(attempts):
        if attempts[ip][0] < now - 60:
            del attempts[ip]
    ip = request.client.host if request.client else 'unknown'
    stamp, count = attempts.get(ip, (now, 0))
    if count >= 10 or (ip not in attempts and len(attempts) >= 10000):
        raise HTTPException(429, '登入嘗試過多，請稍後重試')
    attempts[ip] = (stamp, count + 1)
    encoded = request.app.state.users.get(body.username)
    if not encoded or not password_matches(body.password, encoded):
        raise HTTPException(401, '帳號或密碼錯誤')
    payload = base64.urlsafe_b64encode(json.dumps({'user': body.username, 'exp': now + 28800}).encode()).decode()
    signature = hmac.new(request.app.state.key, payload.encode(), hashlib.sha256).hexdigest()
    response = JSONResponse({'username': body.username})
    response.set_cookie('csv_session', f'{payload}.{signature}', httponly=True, secure=os.getenv('CSV_SECURE_COOKIE', '1') == '1', samesite='strict', max_age=28800)
    return response


@app.post('/api/logout')
def logout():
    response = JSONResponse({'ok': True})
    response.delete_cookie('csv_session')
    return response


@app.get('/api/me')
def me(username=Depends(user)):
    return {'username': username}


@app.get('/api/datasets')
def datasets(username=Depends(user)):
    result = []
    for path in DATA.glob('*.sqlite3'):
        db = connect(path.stem)
        try:
            result.append(metadata(db))
        finally:
            db.close()
    return sorted(result, key=lambda item: item['created_at'], reverse=True)


@app.post('/api/datasets', status_code=202)
async def upload(background: BackgroundTasks, file: UploadFile = File(...), encoding: str = 'utf-8-sig', delimiter: str = 'auto', username=Depends(user)):
    if encoding not in ('utf-8-sig', 'cp950', 'utf-16') or delimiter not in ('auto', ',', ';', '\t', '|'):
        raise HTTPException(400, '不支援的編碼或分隔符號')
    if not file.filename or not file.filename.lower().endswith('.csv'):
        raise HTTPException(400, '請上傳 CSV 檔案')
    dataset = str(uuid.uuid4())
    source = DATA / f'{dataset}.upload'
    size = 0
    try:
        with source.open('xb') as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f'檔案超過 {MAX_BYTES // 1024 // 1024}MB 上限')
                out.write(chunk)
        info = dict(id=dataset, name=Path(file.filename).name, state='importing', columns=[], row_count=0, bytes=size, encoding=encoding, delimiter=delimiter, created_at=time.time(), created_by=username, error=None)
        db = sqlite3.connect(DATA / f'{dataset}.sqlite3')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE metadata(value TEXT NOT NULL)')
            db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(info, ensure_ascii=False),))
            db.commit()
        finally:
            db.close()
        background.add_task(import_csv, dataset)
        return info
    except Exception:
        source.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def ready(db):
    info = metadata(db)
    if info['state'] != 'ready':
        raise HTTPException(409, '資料尚未匯入完成')
    return info


def query_parts(info, q, column, op, value, sort, direction):
    n = len(info['columns'])
    conditions, params = [], []
    def like(text):
        return '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    if q:
        conditions.append('(' + ' OR '.join(f"c{i} LIKE ? ESCAPE '\\'" for i in range(n)) + ')')
        params.extend([like(q)] * n)
    if column is not None:
        if not 0 <= column < n:
            raise HTTPException(400, '欄位不存在')
        if op == 'contains':
            conditions.append(f"c{column} LIKE ? ESCAPE '\\'")
            params.append(like(value))
        elif op == 'equals':
            conditions.append(f'c{column} = ?')
            params.append(value)
        elif op == 'empty':
            conditions.append(f"c{column} = ''")
        else:
            raise HTTPException(400, '篩選方式不存在')
    if direction not in ('asc', 'desc') or (sort is not None and not 0 <= sort < n):
        raise HTTPException(400, '排序參數錯誤')
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    order = f' ORDER BY c{sort} {direction}, id' if sort is not None else ' ORDER BY id'
    return where, order, params


@app.get('/api/datasets/{dataset}/rows')
def rows(dataset: str, offset: int = 0, limit: int = 100, q: str = '', column: int | None = None, op: str = 'contains', value: str = '', sort: int | None = None, direction: str = 'asc', username=Depends(user)):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(400, '分頁參數錯誤')
    db = connect(dataset)
    try:
        info = ready(db)
        where, order, params = query_parts(info, q, column, op, value, sort, direction)
        db.execute('BEGIN')
        total = db.execute('SELECT COUNT(*) FROM rows' + where, params).fetchone()[0] if where else info['row_count']
        result = db.execute('SELECT * FROM rows' + where + order + ' LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
        return {'columns': info['columns'], 'total': total, 'rows': [{'id': row['id'], 'version': row['version'], 'values': [row[f'c{i}'] for i in range(len(info['columns']))]} for row in result]}
    finally:
        db.close()


class Edit(BaseModel):
    column: int
    value: str = Field(max_length=16 * 1024 * 1024)
    version: int


@app.patch('/api/datasets/{dataset}/rows/{row_id}')
def edit(dataset: str, row_id: int, body: Edit, username=Depends(user)):
    db = connect(dataset)
    try:
        info = ready(db)
        if not 0 <= body.column < len(info['columns']):
            raise HTTPException(400, '欄位不存在')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM rows WHERE id=?', (row_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '資料列不存在')
        if row['version'] != body.version:
            raise HTTPException(409, '此列已被其他人修改，請重新整理後再試')
        old = row[f'c{body.column}']
        db.execute(f'UPDATE rows SET c{body.column}=?, version=version+1 WHERE id=?', (body.value, row_id))
        db.execute('INSERT INTO audit(row_id,column_index,old_value,new_value,username,changed_at) VALUES (?,?,?,?,?,?)', (row_id, body.column, old, body.value, username, int(time.time())))
        db.commit()
        return {'version': body.version + 1, 'old_value': old}
    finally:
        db.close()


@app.get('/api/datasets/{dataset}/audit')
def audit(dataset: str, username=Depends(user)):
    db = connect(dataset)
    try:
        ready(db)
        return [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')]
    finally:
        db.close()


@app.get('/api/datasets/{dataset}/export')
def export(dataset: str, q: str = '', column: int | None = None, op: str = 'contains', value: str = '', sort: int | None = None, direction: str = 'asc', username=Depends(user)):
    db = connect(dataset)
    try:
        info = ready(db)
        where, order, params = query_parts(info, q, column, op, value, sort, direction)
    finally:
        db.close()
    def generate():
        db = connect(dataset)
        try:
            db.execute('BEGIN')
            buf = io.StringIO(newline='')
            writer = csv.writer(buf)
            writer.writerow(info['columns'])
            yield '\ufeff' + buf.getvalue()
            buf.seek(0); buf.truncate(0)
            for row in db.execute('SELECT * FROM rows' + where + order, params):
                writer.writerow([row[f'c{i}'] for i in range(len(info['columns']))])
                if buf.tell() >= 65536:
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate(0)
            if buf.tell():
                yield buf.getvalue()
        finally:
            db.close()
    return StreamingResponse(generate(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': "attachment; filename=data.csv; filename*=UTF-8''" + quote(info['name'])})


@app.get('/healthz')
def health():
    return {'status': 'ok'}


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')
