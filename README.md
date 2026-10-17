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

## 代理人：外部資料正規化（Agent 1）

`agent/cutsound_normalizer/` 是 ADK 代理人：Gemini 讀新資料夾與來源說明頁，提出逐欄對映（標籤規則、工況欄位、單位換算、證據、待答問題）；**人核准後**才由確定性工具轉成 MSM 規格（48 kHz／16-bit／單聲道 WAV＋12 欄 `label.csv`）、驗證、建表。來源沒講清楚的欄位一律空值或 -1，並列成待答問題。細節見 `agent/README.md`。

| 路徑 | 用途 |
|---|---|
| `agent/cutsound_normalizer/` | 代理人（14 個工具：對映 8 個＋聲學證據 6 個）、對映 schema、提案 schema `proposals.py`、MSM 規格 `spec/msm_spec.json` |
| `agent/mappings/` | 已核准的參照對映（`ctwa_reference.json`、`msm_vm20i_identity.json`） |
| `scripts/normalize.py` | 一條龍離線入口：已核准對映 → 轉換 → 驗證 → 表（parquet）→（可選）建庫 |
| `scripts/propose_mapping.py`、`eval_mapping.py` | 代理人提對映（需金鑰）、與參照對映逐檔比對一致率 |
| `scripts/propose_evidence.py`、`apply_proposals.py`、`eval_agent_phase2.py` | 代理人聽音檔提證據提案（需金鑰）、人核准的提案寫進新對映、以預先登記規則打分（見下節） |

```bash
# 離線（不呼叫 Gemini）：對映已由人核准
python scripts/normalize.py --source <資料夾> --mapping agent/mappings/ctwa_reference.json \
    --out <正規化輸出> --approved-by "<核准人>" [--db <庫目錄> --msm-raw <MSM raw>]
```

輸出結構與 MSM 原始資料相同：`<out>/<source_id>/<machine_id>/dataset<N>/`，內含 `<session_id>_s0.wav`、`label.csv`、`session.json`（含 `machine_short`、`machine_name`、`domain`、`coolant`、`mic_type`、`workpiece`、`remark`）；每步結果記在 `<out>/<source_id>/_manifest/normalize_run.json`。

- **編號 N**：`msm_session` 分組（MSM 版面）直接沿用來源資料夾 `dataset<N>` 的編號（MSM 恆等轉換的 37 場與原始編號相同）；`per_file` 分組（如 CTWA）依 session_id 排序從 1 起（不透明、不洩漏類別）。**同一檔案集合可重現；檔案集合改變（增刪檔）即重編，N、建庫 id 與驗證頁網址都會跟著變，穩定鍵是 session_id**。對照表 `_manifest/dataset_index.csv`（`dataset,session_id,source_relative_path`）只放在 manifest，不進 session.json。
- **未標註切削**：對映判定「在切削、但標籤未定」（`cut_state=cutting`、`msm_label=-1`，例如代理人棄權）的區間不寫進 `label.csv`（仍只含 0／1／2），另寫 `<session_id>_unlabeled.csv`（`start,end,cut_state`，有這種區間才寫）。建庫時這些區間成為 `cut_state=cutting`、`label_std=-1` 的事件，不被當成非切削空隙；驗證頁以紫色「未標註切削」顯示。
- **切削狀態未知**：對映說不出是否在切削（`cut_state=unknown`，或檔名沒有對應的標籤規則）的區間同樣寫進側檔（`cut_state=unknown`），建庫照存 `cut_state=unknown`、`label_std=-1`，不當成非切削；驗證頁以紫紅「切削狀態未知」顯示。側檔起訖非有限數字、start≥end、超出音長、或與 label.csv／其他側檔列重疊時，validate 與建庫都會指名場次與列報錯（建庫不產生任何庫）。
- 來源資料夾中任何以 `_` 或 `.` 開頭的路徑元件（例如自己放的 `_analysis/`）不會被音檔 glob 選進來。
- 用驗證頁看 `--db` 建出的庫：`session_viewer_app.py --msm <資料夾>`，其中 `<資料夾>/db` 就是 `--db` 的目錄（可用目錄連結）；正規化場次的音檔依庫內登記的根目錄讀取。給 `--db` 時會執行 `proto/scripts/build_db.py <msm-raw> <db> --extra-raw <out>/<source_id>`。驗證有錯即停（exit 1）；建庫程式尚不支援 `--extra-raw` 時明說並 exit 2。

**提案 → 人核准 → 正規化**（代理人的提案永遠不會自動套用）：

```bash
# 1. 線上：代理人讀資料夾與 Kaggle 說明頁，提出對映，存到 $NORMALIZER_WORK_DIR/mappings/<source_id>.json 後停下
python scripts/propose_mapping.py <資料夾> --kaggle nachiketsoni/cutting-tool-wear-audio-dataset --source-id ctwa --log propose.log
# 2. 離線：與參照對映逐檔比對（V6 一致率報告；Tool、Workpiece 兩個文字欄忽略大小寫與 -／_／空白差異；direction（UP／DOWN 列舉）與 N、ap、ae、F、Z、D 數值欄須完全相同；以 `_`／`.` 開頭的路徑元件不計，與轉換同規則）
python scripts/eval_mapping.py <資料夾> work/mappings/ctwa.json agent/mappings/ctwa_reference.json --report v6_report.json
# 3. 人讀提案全文、回答 unresolved 問題、必要時改標籤規則；確認後才轉換
python scripts/normalize.py --source <資料夾> --mapping <核准後的對映> --out <正規化輸出> --approved-by "<核准人>"
```

- **線上模式**（只有第 1 步）：需 Gemini 憑證，環境變數擇一：`GOOGLE_API_KEY`，或 `GOOGLE_API_KEY_FILE`（指向一個第一行為金鑰的文字檔；程式只讀不印）；或 `GOOGLE_GENAI_USE_VERTEXAI=TRUE`＋`GOOGLE_CLOUD_PROJECT`（Vertex AI）。選用 `NORMALIZER_MODEL`（預設 `gemini-3.8-flash`）、`NORMALIZER_WORK_DIR`（提案存放處，預設 `./work`）。成功時 exit 0，提案狀態 `saved_pending_approval`；沒有憑證、API 錯誤或代理人沒存出合格對映時 exit 非 0，且**不建立也不覆寫**提案檔（代理人先存到暫存資料夾，成功才複製過去）。設定 `GOOGLE_CLOUD_PROJECT` 時 `load_tables` 也會載入 BigQuery，否則回報 skipped。金鑰由使用者自行設定，不進 repo。
- **離線模式**（第 2、3 步）：不呼叫 Gemini、不需任何金鑰；用已核准的對映（例如 `agent/mappings/ctwa_reference.json`）。對映仍有 unresolved 問題時，沒有 `--approved-by` 會拒絕轉換。
- 互動版：`adk web agent`，代理人存檔後會列出標籤規則與待答問題並請求核准；`convert_source`、`load_tables` 需在介面上確認才會執行。目前互動版有一個已知問題待修（見 `agent/README.md` Online mode），headless 的 `propose_mapping.py` 已繞過。
- 套件：`pip install -r agent/requirements.txt`。資料（含 CTWA：Apache 2.0，N. Soni et al., IEEE R10-HTC 2023）請自原始來源下載，本 repo 不含音檔。

### 代理人第二階段：聽音檔、提證據提案（不標註）

代理人另有四個確定性聲學工具（`audio_profile` 四頻帶能量／削波、`detect_change_points` 狀態變化點、`estimate_spindle_speed` 轉速候選與宣稱值比對、`class_separability` 兩類別逐頻帶 AUC）、`render_session_figure`（與驗證頁同款的波形＋四頻帶圖），以及 `save_proposals`。它先讀工況與資料夾類型、**宣告檢查計畫**（每個資料夾跑哪些工具、為什麼），再執行，最後寫提案檔：候選＋引用工具數字的證據＋信心＋待答問題。**它不寫 label.csv、不改 N**；只允許三種結論：「這裡狀態改變」「轉速與宣稱不符」「兩類在聲學上分得開／分不開」。工具的已知限制（取自預先登記的 MSM 評估）寫在代理人指令裡，提案要引用。設計與場景：`design/bdd-agent1-phase2.md`。

```bash
# 1. 線上：代理人依工況選檢查、跑工具，寫 $NORMALIZER_WORK_DIR/proposals/<source_id>.json（狀態 pending_approval，不套用）
python scripts/propose_evidence.py <資料夾> --mapping agent/mappings/ctwa_reference.json --source-id ctwa --log evidence.log
#    MSM 場次（資料夾內有 label.csv）自動進入 consistency_only：只檢查既有標籤，不提標籤候選
# 2. 離線：用預先登記的規則打分（FEA-001／FEA-003 呼叫凍結的 check_scenarios.py；CTWA 斷刀點 GOLD 區間；MSM 變化點對 label.csv 的 |Δt|）
python scripts/eval_agent_phase2.py work/proposals/ctwa.json
# 3. 人寫核准清單（接受哪些 id、變化點的標籤），寫進「新的」對映，再照第一階段轉換
python scripts/apply_proposals.py work/proposals/ctwa.json --mapping agent/mappings/ctwa_reference.json \
    --out work/mappings/ctwa_with_evidence.json --approvals approvals.json [--replace-existing]
python scripts/normalize.py --source <資料夾> --mapping work/mappings/ctwa_with_evidence.json --out <正規化輸出> --approved-by "<核准人>"
```

- 沒有憑證 exit 3、模型或執行錯誤 exit 1、沒存出合格提案 exit 2；失敗時工作資料夾不留提案檔。證據跑中呼叫 `convert_source`／`load_tables`／`save_mapping` 也判失敗。
- 單一檔案讀取失敗會記在提案檔 `errors`，其餘照跑。提案檔或核准清單含 `set_N`、`write_label_csv` 等越權鍵時，`apply_proposals.py` 點名拒絕（exit 4），不寫任何檔。
- `save_proposals` 逐筆檢查證據數字是否出現在該工具的實際輸出，結果記在每筆的 `evidence_check`。
- 2026-10-17 實跑（gemini-3.8-flash）：CTWA 第一次 FEA-001 過、FEA-003 未過且沒抽到斷刀檔（指令只說「前段檔案」、也沒要求每個轉速不符都要提）；修正指令後第二次 FEA-001、FEA-003 皆過，斷刀點候選 1.5 s（GOLD 區間 0.75–1.80 s），43 筆證據數字全部可在工具輸出找到。MSM imi_vmx30ui dataset14：無任何標籤候選，引用的 5 個變化點與 label.csv 邊界相差 0.035–0.105 s；8 條路徑轉速 6 條一致，2 條（10,000 rpm 取到 0.804×N、12,000 rpm 取到 0.201×N）提為低信心問題。BigQuery 證據表與 Cloud Run 上的證據跑尚未執行。
