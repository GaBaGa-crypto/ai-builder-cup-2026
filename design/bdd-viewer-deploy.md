# BDD：切段驗證模組上 GitHub 與 Cloud Run（2026-10-16，GB10）

> 使用者拍板（10/16）：GitHub **私人 repo、只放程式**；網址用 **Google Cloud Run**；GitHub 登入走 GitHub MCP（本 session 仍為 needs_auth，等使用者 `/mcp` 登入）。
> CLAUDE.md 硬規則 #6 原為「只用本機 git、不設 remote」，本次由使用者明確改為推到 GitHub 私人 repo。

## 架構（雲端不碰原始資料庫）
- GB10 先把 69 場的頁面資料算好成「資料包」：`sessions.json`、`payload/<machine>-<ds>.json`、`audio/<session_id>/<ch>.mp3`、`index.html`。VF-10 測試場次照 prereg-v1 封存（不含事件、標籤計數、備註）。
- 資料包上傳到 GCS bucket；Cloud Run 以 Cloud Storage volume 掛載唯讀讀取。**來源庫、標籤庫、原始 WAV 都不上雲**（design/11 與 bdd-step2 S12 的護欄）。
- 雲端伺服器 `viewer_bundle_server.py` 只用 Python 標準庫，靜態讀資料包、支援 HTTP Range（MP3 跳播）。
- 存取碼：環境變數 `VIEWER_TOKEN`；第一次以 `?k=<token>` 進入後設 HttpOnly cookie，之後同瀏覽器免再帶；沒有碼一律 401。

## 場景
- **S1 資料包完整**：69 個 payload、175 個 MP3；每個 MP3 長度與原 WAV 差 < 0.1 秒；sessions.json 69 列。
- **S2 封存**：10 個 VF-10 測試場次的 payload 與 sessions.json 列沒有事件起訖、標籤計數、備註；全資料包不含 `labels.sqlite`／`source.sqlite`。
- **S3 與本機模組一致**：同一場（抽 VM20i ds9、VF-10 ds2）資料包 payload 的事件、波形、頻帶與本機模組 `/api/session` 相同（音檔網址除外）。
- **S4 存取碼**：無碼 → 401；`?k=錯碼` → 401；`?k=正確碼` → 200 並設 cookie；帶 cookie 的後續請求（含 /audio Range）→ 200／206。
- **S5 路徑安全**：只服務資料包內的檔；`..`、絕對路徑、不在白名單的副檔名 → 404。
- **S6 本機先跑通**：同一個 server 用本機資料包跑起來，瀏覽器實測選場次、播放、跳播、四頻帶圖都正常，再部署。
- **S7 repo 只含程式**：git 追蹤清單只有 `proto/scripts/`、`design/bdd-*.md`、`design/prereg-*`、`deploy/`、`README.md`、`.gitignore`；不含任何 wav／mp3／sqlite／pdf／xlsx／png／npz。

## 積木對照表
| 需要的能力 | 既有積木 | 動作 |
|---|---|---|
| 單場頁面資料 | `build_payload()`（session_viewer.py） | 複用 |
| 頁面 | `session_viewer_template.html`（模組模式 `fetch("api/...")`） | 複用；資料包伺服器提供同樣的 `api/sessions`、`api/session` 路徑 |
| 場次清單（含封存遮蔽） | session_viewer_app.py 的 listing 邏輯 | **抽出** `session_listing()` 到 session_viewer.py（第 2 個呼叫者＝真接縫） |
| HTTP Range 回應 | session_viewer_app.py 的 `send_wav` | **抽出** 成 stdlib 模組 `http_range.py`，本機模組與雲端伺服器共用 |
| MP3 編碼 | session_viewer.py `main()` 的 soundfile MP3 段 | 抽成 `write_mp3()` 共用 |
| 資料包匯出、雲端伺服器、Dockerfile、部署腳本 | 無 | 新建 `export_viewer_bundle.py`、`viewer_bundle_server.py`、`deploy/` |
