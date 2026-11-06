# CSV 工作台

以繁體中文網頁檢視與編輯大型 CSV；適合一台雲端主機上讓小型團隊共同使用。上傳檔案分批匯入 SQLite，瀏覽器每頁僅接收 100 列，避免把整個 200MB CSV 載入畫面。

## 功能

- 共用資料集、獨立帳號登入、UTF-8 / Big5 (CP950) / UTF-16 匯入。
- 全欄位搜尋、單欄包含 / 等於 / 空白篩選、文字排序、分頁。
- 儲存格編輯、上一筆修改復原（目前瀏覽器工作階段）、最近 100 筆修改紀錄。
- 列版本檢查：其他人已修改該列時回報衝突，不覆蓋既有變更。
- 串流匯出目前查詢結果，UTF-8 BOM CSV，保留前置零與多行文字。

CSV 需含標題列，最多 500 欄。重複欄名依欄位位置區分。所有欄位均以文字保存；排序為文字順序（例如 10 排在 2 前），無 Excel 公式、樣式、圖表、欄位新增刪除或即時同步游標。其他人的修改可重新套用查詢以取得。所有登入帳號均可讀寫所有資料集。

## Mac Docker 快速開始（建議）

支援 Intel 與 Apple Silicon Mac，使用 Docker Desktop 的 Linux containers。此程式會在你的 Mac 執行，資料存於 Docker 持久資料卷。

1. 安裝並啟動 Docker Desktop；確認 `docker compose version` 可執行。
2. 在終端機進入此專案資料夾。執行 `python3 scripts/configure.py`，建立至少一個帳號（可重複輸入以新增多人帳號）。此工具只需 Python 3 標準函式庫。若 Mac 尚無 Python 3，先安裝 Python 3。
3. 執行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=50
```

服務顯示 healthy 後，在 Mac 瀏覽器輸入 `http://localhost:8000`，登入並上傳 CSV。可先在 Docker Desktop 配置 2 CPU、2GB 記憶體，依實際使用情況調整。

### 讓同一區域網路的同事連線

預設只允許 Mac 本機連線。若要開放區域網路，在專案根目錄建立 `.env`（不含密碼）：

```dotenv
CSV_BIND_IP=0.0.0.0
CSV_PORT=8000
CSV_SECURE_COOKIE=0
```

執行 `docker compose up -d`。在 macOS「系統設定 → 網路」查看 Mac 的區網 IP，同事在相同網路以 `http://Mac的區網IP:8000` 存取，並各自登入。若 macOS 防火牆詢問，允許 Docker Desktop 的連入連線。這是 HTTP 區網配置；跨網路存取應設置 HTTPS 反向代理與 `CSV_SECURE_COOKIE=1`，不要直接把 HTTP 連接埠轉送到公網。

Mac 需保持開機，Docker Desktop 需持續執行；睡眠或關機會中斷連線。所有帳號共用同一批資料，並非每人獨立的私有空間。

### 帳號、更新與資料保存

- 新增或修改帳號：重跑 `python3 scripts/configure.py`，再執行 `docker compose restart`。設定保存於 `.local/server.json`，內含密碼雜湊與工作階段金鑰，勿分享或提交 Git。主機的 `.local` 目錄限制為僅擁有者可讀取；檔案以唯讀 Docker secret 掛載至非 root 程序。
- 停止：`docker compose down`。再次 `docker compose up -d` 可恢復已匯入資料。
- **不要加 `-v`**：`docker compose down -v` 會刪除資料卷。Docker Desktop 的清除資料 / 重設功能也可能刪除資料卷。
- 更新程式：保留 `.local` 與資料卷，執行 `docker compose up -d --build`。
- 若檔案達 200MB，預留足夠的 Docker Desktop 磁碟容量與主機空間。CSV 上限預設 1GB。
- 資料卷不能取代備份。可先 `docker compose stop`，在 Docker Desktop 的 Volumes 匯出 `csv-data` 資料卷，再 `docker compose start`；還原流程應另行驗證。

## 不使用 Docker 的開發啟動（Python 3.12）

```bash
cd /workspace/excel
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

產生密碼雜湊（互動輸入不顯示密碼）：

```bash
.venv/bin/python -c 'from app.main import password_hash; from getpass import getpass; print(password_hash(getpass("Password: ")))'
```

在主機的安全環境設定中配置下列變數；不要將實際密碼、雜湊或工作階段金鑰提交到 Git：

| 變數 | 設定 |
| --- | --- |
| `CSV_USERS_JSON` | 帳號對應密碼雜湊的 JSON，例如 `{"amy":"pbkdf2_sha256$600000$...$...","bob":"pbkdf2_sha256$600000$...$..."}` |
| `CSV_SESSION_SECRET` | 至少 32 字元隨機金鑰，可由 `python -c 'import secrets; print(secrets.token_urlsafe(48))'` 產生 |
| `CSV_DATA_DIR` | 持久資料目錄，預設目前目錄下 `.data` |
| `CSV_MAX_UPLOAD_MB` | 預設 1024（1GB），可依磁碟與反向代理限制調整 |
| `CSV_SECURE_COOKIE` | 預設 `1`，僅在本機 HTTP 開發時設為 `0` |

環境變數已設定後：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

服務健康檢查：`curl --fail http://127.0.0.1:8000/healthz`。以設定的帳號登入，匯入 CSV，等待狀態變為可用。第一次匯入時可選擇 Big5 與分隔符號修正亂碼或欄位錯位。

## 雲端部署

部署前需選定雲端供應商與持久磁碟；本專案尚未部署到公網。

- 使用單一應用程式實例、單一 worker；SQLite 檔案放在本機持久磁碟，不能放到共用網路檔案系統。重啟程序不會刪除已完成的資料。
- 反向代理 / 負載平衡器提供 HTTPS，保持 `CSV_SECURE_COOKIE=1`，限制登入及上傳請求頻率。登入 cookie 有效八小時。
- 設定代理上傳上限（例如 Nginx `client_max_body_size 1024m`）、足夠的上傳與查詢逾時（例如 600 秒）。只信任部署平台的轉送標頭。
- 上傳解析會先使用系統暫存目錄；暫存區與資料磁碟都需有足夠空間。依原始檔、SQLite、排序暫存、WAL 與匯出併行量預留容量；200MB 檔案建議先預留至少 2GB 可用空間，持續監控。
- 匯入每批 1000 列，單一程序內依序匯入。程式重啟會將未完成匯入標記失敗，需重新上傳；沒有外部工作佇列。
- 搜尋與未建索引的排序可能掃描整個資料集。分頁限制回傳量，不代表所有查詢都即時；高併發或更大資料量需另行改用 PostgreSQL / 查詢引擎與背景工作佇列。
- 定期用 SQLite backup API 對每個資料庫做一致性備份。勿在寫入時僅複製 `.sqlite3` 而漏掉 WAL；正式採用前應驗證備份還原。
- 匯出保留使用者原始文字，開啟至試算表軟體時需留意來源資料中的公式內容。

## 測試

測試工具可另外安裝 `httpx==0.28.1`，執行：

```bash
python -m unittest discover -s tests -v
```

測試使用臨時資料目錄與臨時登入設定，驗證登入限制、CSV 引號 / 換行 / 前置零、中文編碼、篩選、排序、編輯、跨使用者衝突、修改紀錄、復原及篩選匯出。

## 大檔案與瀏覽器驗證

`python tests/benchmark.py` 會產生約 201MB、100 萬列的臨時 CSV，啟動臨時 HTTP 伺服器，驗證匯入、首尾分頁、篩選、排序、編輯與完整串流匯出，最後清除測試資料。

`python tests/browser_smoke.py` 額外需要 Playwright 與 Chromium；使用 `CSV_CHROMIUM_PATH` 指定瀏覽器執行檔，驗證網頁登入、上傳、篩選、編輯、復原、修改紀錄及下載。

目前 Linux 開發環境合成資料實測：上傳加匯入 4.58 秒、等值篩選 0.15 秒、文字排序 0.84 秒、完整匯出 4.94 秒，應用伺服器記憶體峰值約 43MB。這不是 Mac 或多使用者壓力測試的效能保證；真實 CSV 的欄數與欄位長度會影響結果。
