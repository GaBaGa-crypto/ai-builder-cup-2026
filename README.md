# MSM 切削異音：資料管線與切段驗證工具

AI Builder Cup 2026（工況條件化的切削聲音異常偵測）在 GB10 上的程式。資料是 Purdue 的 MSM 多感測器銑削資料集（Scientific Data 2026, 13:945；資料 CC BY 4.0，Kaggle DOI 10.34740/kaggle/ds/8392825）。**本 repo 不含任何資料**：音檔、資料庫、標籤都不進版控。

## 內容

| 路徑 | 用途 |
|---|---|
| `proto/scripts/download_msm.py` | 從 Kaggle 匿名逐檔下載研究子集（可續傳、MD5 驗證） |
| `proto/scripts/inventory.py`、`msm_io.py` | 第 1 步盤點、共用讀取器 |
| `proto/scripts/build_db.py`、`check_db.py` | 第 2 步建三庫（主庫／隔離來源庫／封存標籤庫）與獨立驗收 |
| `proto/scripts/make_splits.py`、`check_splits.py` | 第 3 步切分與預先登記（`design/prereg-v1.json`） |
| `proto/scripts/band_trends.py`、`per_file_report.py`、`band_pdf.py`、`plot_db_envelope.py` | 四頻帶（0–4k、1–4k、4–10k、10–24k Hz）趨勢、Excel 與 PDF 報表 |
| `proto/scripts/session_viewer*.py`、`session_viewer_template.html` | 切段驗證頁：選場次、播放、依標籤上色的波形、四頻帶 dB／秒圖、主軸轉速、事件表 |
| `proto/scripts/export_viewer_bundle.py`、`viewer_bundle_server.py`、`http_range.py` | 把驗證頁匯出成資料包，並在 Cloud Run 上服務 |
| `Dockerfile`、`.gcloudignore`、`deploy/deploy.sh` | Cloud Run 部署 |
| `design/bdd-*.md` | 每項工作的 BDD 規格與驗收場景 |
| `design/prereg-v1.json`、`prereg-v1-deviations.md` | 預先登記與偏離紀錄 |

## 本機使用（GB10）

```bash
# 切段驗證模組（只綁 127.0.0.1，直接讀原始 WAV）
~/anaconda3/envs/audio_preject/bin/python proto/scripts/session_viewer_app.py --open
```

## 雲端網址（Cloud Run）

```bash
# 1. 在 GB10 匯出資料包（約 1.5 GB：69 場的頁面資料＋175 個 MP3）
~/anaconda3/envs/audio_preject/bin/python proto/scripts/export_viewer_bundle.py
# 2. 部署（需先 gcloud auth login，且專案已綁帳單）
PROJECT=<GCP 專案 ID> deploy/deploy.sh
```

部署腳本會：建私有 bucket 並上傳資料包、以唯讀 Cloud Storage volume 掛到服務、產生存取碼（存在 `~/.config/msm-viewer/token`，權限 600），最後印出 `https://…/?k=<存取碼>`。沒有存取碼的請求一律 401。

## 封存規則

VF-10 測試場次（`prereg-v1.json` 的 `vf10_reference_datasets` 以外）在本機模組與雲端資料包中**預設不送切段、標籤計數與備註**。雲端資料包不含來源庫、標籤庫與原始 WAV。
