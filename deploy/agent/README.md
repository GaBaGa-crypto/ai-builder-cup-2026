# 代理人上 Cloud Run（ADK Web UI）

把 `agent/cutsound_normalizer` 的 `root_agent`（ADK `LlmAgent`，Gemini）部署成 Cloud Run 服務 `msm-normalizer-agent`，
網頁上可以和代理人對話，並在事件面板看到它呼叫了哪些工具、傳回什麼。

> 狀態：腳本與檢查已備妥，**尚未部署**。gcloud（Google Cloud SDK 589.0.0）已安裝在
> `C:\Users\m143359\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin`，使用者 PATH 已加入，**需開新的終端機**才吃得到；
> **尚未登入**（`gcloud auth list` 顯示 No credentialed accounts），下面的雲端指令都還沒有執行過（2026-10-17 確認）。

| 檔案 | 用途 |
|---|---|
| `deploy_agent.sh` | 一支腳本完成：組裝上傳內容 → 安全檢查 → 啟用 API → 建專屬服務帳號與授權 → `adk deploy cloud_run` → 印出網址 |
| `check_local.sh` | 部署前的本機檢查：匯入 `root_agent` 列出工具（至少 8 個）；組裝上傳內容並檢查；`--serve` 印出本機冒煙測試步驟 |
| `adk_deploy_help.txt` | `adk deploy cloud_run --help`（ADK 2.11.0）原文，腳本用到的旗標都在裡面 |

## 為什麼用 `adk deploy cloud_run`，沒有自己的 Dockerfile

`adk deploy cloud_run` 會自己產生 Dockerfile 再呼叫 `gcloud run deploy --source`，所以這裡**不放 Dockerfile**：

- 這是 ADK 官方的 Cloud Run 路徑；產生的映像以非 root 使用者執行、移除 `dev_server`、用 exec 形式啟動
  `adk api_server --with_ui`（`google/adk/cli/deployers/_dockerfile_template.py:100-138`），服務也會自動帶 `created-by=adk` 標籤。
- repo 根目錄的 `Dockerfile` 與 `.gcloudignore` 屬於驗證頁（viewer），`gcloud run deploy --source <repo>` 只會用根目錄那份，
  無法讓兩個服務共用；不論選哪種做法都要另外組一個上傳資料夾。
- 自己維護一份 Dockerfile 會和 ADK 的啟動參數（session 服務、UI 路徑、移除開發端點）脫節。

代價與處理：

- ADK 只安裝 **AGENT 資料夾裡**的 `requirements.txt`（`_dockerfile_template.py:141-160`），我們的在上一層 `agent/requirements.txt`。
  所以腳本先組一個乾淨資料夾 `cutsound_normalizer/`（只複製 `*.py`、`spec/msm_spec.json`、`requirements.txt`），再交給 ADK。
- 基底映像固定是 `python:3.11-slim`（本機是 3.10）。requirements 的套件在 3.11 上應有現成 wheel，但**尚未在 3.11 實測**，
  第一次 Cloud Build 的安裝步驟就是驗證。
- 對話紀錄存在記憶體（ADK 1.3 以後沒指定就用 `memory://`，`cli_deploy.py:779-781`）；容器重啟或縮到 0 會清空。

## 認證：兩種模式（腳本 `AUTH_MODE`）

| | `vertex`（預設，建議） | `apikey`（替代方案） |
|---|---|---|
| 金鑰 | 無。服務帳號＋Vertex AI，映像與環境變數都沒有金鑰 | Secret Manager 祕密掛成環境變數 `GOOGLE_API_KEY`（值不進映像、不進腳本） |
| Cloud Run 上的環境變數 | `GOOGLE_GENAI_USE_ENTERPRISE=1`、`GOOGLE_CLOUD_PROJECT=<專案>`、`GOOGLE_CLOUD_LOCATION=global` | `GOOGLE_GENAI_USE_ENTERPRISE=0`、`GOOGLE_API_KEY`（祕密） |
| 服務帳號權限 | `roles/aiplatform.user` | 該祕密的 `roles/secretmanager.secretAccessor` |

依據（ADK 2.11.0 / google-genai 2.30.1 原始碼，site-packages 下）：

- `google/adk/cli/deployers/_cloud_run_deployer.py:258-264`：ADK 部署時預設設 `GOOGLE_GENAI_USE_ENTERPRISE=1`、`GOOGLE_CLOUD_PROJECT=--project`、
  `GOOGLE_CLOUD_LOCATION=--region`；`:266-271` 使用者 `--env` 的值優先。腳本因此用 `--env GOOGLE_CLOUD_LOCATION=global`
  把模型端點和 Cloud Run 區域（asia-east1）分開——Gemini 3.x 在 Vertex 上走 global 端點。
- `google/adk/utils/env_utils.py:63-78`：`GOOGLE_GENAI_USE_ENTERPRISE` 優先；舊名 `GOOGLE_GENAI_USE_VERTEXAI` 仍可用但會警告已棄用。
- `google/genai/_api_client.py:675-697`：同上，兩者衝突時以 `ENTERPRISE` 為準；`:733-735` 讀 `GOOGLE_CLOUD_PROJECT`、`GOOGLE_CLOUD_LOCATION`
  與 API 金鑰；`:136-137` 金鑰變數為 `GOOGLE_API_KEY`（優先）或 `GEMINI_API_KEY`；`:798-805` Vertex 模式沒給位置時預設 `global`；
  `:777-790` Vertex 模式下環境的 project/location 優先於環境的 API 金鑰；`:817-822` 兩者都沒有就報錯。
- `google/adk/utils/_gcp_metadata.py:164-188`：Vertex 模式且未設 `GOOGLE_CLOUD_PROJECT` 時，從 Cloud Run metadata 補專案；有金鑰就略過。
- 代理人本身：`agent/cutsound_normalizer/agent.py:18` 只在本機用 `GOOGLE_API_KEY_FILE` 讀檔補 `GOOGLE_API_KEY`（雲端不設此變數）；
  `:22` `NORMALIZER_WORK_DIR`（雲端設 `/tmp/normalizer_work`，因為 `/app` 不可寫）；`:23` `NORMALIZER_MODEL`；
  `:102-104` 有 `GOOGLE_CLOUD_PROJECT` 才掛唯讀 `BigQueryToolset`——所以本機檢查至少 8 個工具（聲學工具接上後更多），雲端再多 1 個唯讀 BigQuery 工具組。

## 使用者要先做的事（依序）

1. **Google Cloud SDK 已安裝**（見上方狀態）。開一個**新的** Git Bash，確認 `gcloud --version` 有輸出；
   若仍找不到，暫時用 `export PATH="$PATH:/c/Users/m143359/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"`。
2. **登入**：
   ```bash
   gcloud auth login                                   # 部署用（gcloud 指令的身分）
   gcloud auth application-default login               # ADC：只有本機跑代理人／BigQuery 才需要，部署本身不用
   gcloud auth application-default set-quota-project endless-bolt-494801-b1
   ```
3. **確認專案**：`endless-bolt-494801-b1` 已綁帳單；你的帳號在該專案有 Owner（或至少能啟用 API、建服務帳號、改專案 IAM、部署 Cloud Run 與 Cloud Build）。
4. **確認模型**：在 Vertex AI Model Garden 確認 `gemini-3.8-flash` 在 `global` 可用；若 Vertex 上的模型 ID 不同，部署時加 `NORMALIZER_MODEL=<ID>`。
5. **本機檢查**：`deploy/agent/check_local.sh`（不需 gcloud；預期印出至少 8 個工具與約 16 個上傳檔）。
6. **部署**（在 repo 根目錄，Git Bash）：
   ```bash
   PROJECT=endless-bolt-494801-b1 deploy/agent/deploy_agent.sh
   ```
7. **開網頁**（預設私有，需 IAM 驗證）：
   ```bash
   gcloud run services proxy msm-normalizer-agent --project endless-bolt-494801-b1 --region asia-east1 --port 8080
   # 瀏覽 http://localhost:8080/dev-ui/ ，選 cutsound_normalizer
   ```
   要讓評審用自己的 Google 帳號開：`gcloud run services add-iam-policy-binding msm-normalizer-agent --region asia-east1 --member=user:<email> --role=roles/run.invoker`。
   要完全公開（任何人免登入）：重跑時加 `--public`。ADK 自己警告 Web UI 是開發用介面（`cli_tools_click.py:296-299`），
   公開時每次對話都花你的 Gemini 額度，所以預設不公開。

### API 金鑰模式（替代方案）

祕密由你自己建立，腳本只檢查它存在、授權服務帳號讀取、掛成環境變數，**從不讀取值**：
```bash
gcloud services enable secretmanager.googleapis.com --project endless-bolt-494801-b1
gcloud secrets create gemini-api-key --project endless-bolt-494801-b1 --replication-policy=automatic
gcloud secrets versions add gemini-api-key --project endless-bolt-494801-b1 --data-file=<你的金鑰檔>   # 檔案只含金鑰一行
AUTH_MODE=apikey PROJECT=endless-bolt-494801-b1 deploy/agent/deploy_agent.sh
```
從 apikey 切回 vertex：重跑預設模式即可（`GOOGLE_GENAI_USE_ENTERPRISE` 會被改回 1，Vertex 的 project/location 優先於金鑰），
但建議把舊祕密拿掉：`gcloud run services update msm-normalizer-agent --region asia-east1 --remove-secrets=GOOGLE_API_KEY`。

## 腳本做了什麼

1. **組裝與檢查上傳內容**（白名單複製，不靠忽略規則）：只放 `*.py`、`spec/msm_spec.json`、`requirements.txt`；
   出現其他副檔名、`work/`、`_tables/`、`_manifest/`、`.env`、`.adk/`，或內容符合 Google API 金鑰／私鑰／服務帳號 JSON 樣式，
   或總大小超過 2 MB，就停止（只列檔名，不印內容）。
2. **啟用 API**：run、cloudbuild、artifactregistry、aiplatform、bigquery、iam（apikey 模式加 secretmanager；有 `DATA_BUCKET` 加 storage）。
3. **專屬服務帳號** `msm-normalizer-run@<專案>.iam.gserviceaccount.com`，專案層級只給：
   `roles/aiplatform.user`、`roles/bigquery.jobUser`、`roles/bigquery.dataViewer`（代理人的 BigQuery 工具組是 `WriteMode.BLOCKED`）。
   `BQ_WRITE=1` 才多給 `roles/bigquery.dataEditor`（讓雲端的 `load_tables` 能寫表；正常流程是在本機離線載入）。
   新帳號 IAM 生效有延遲，授權失敗會等 10–80 秒重試（同 `deploy/deploy.sh`）。
4. **部署**：`adk deploy cloud_run --with_ui --adk_version 2.11.0 --service_name msm-normalizer-agent --app_name cutsound_normalizer`，
   `--` 之後交給 gcloud：`--service-account`、`--no-allow-unauthenticated`（或 `--allow-unauthenticated`）、`--execution-environment=gen2`、
   `--memory=4Gi --cpu=2 --min-instances=0 --max-instances=2 --concurrency=4 --timeout=900`、`--labels=app=msm-normalizer`、`--quiet`。
   `--source/--project/--region/--port/--update-env-vars/--verbosity` 由 ADK 管理，不能放在 `--` 之後（`_cloud_run_deployer.py:80-96`）。
5. **印出網址**與開啟方式。

可重跑：API 啟用、服務帳號（先 describe 再建）、IAM 綁定都是冪等；每次部署產生一個新 revision。

### 雲端能做到與做不到的

- 能：對話、讀 MSM 規格與 mapping schema、`kaggle_dataset_info`（公開網頁）、`save_mapping`（存到容器 `/tmp`，重啟即消失）、唯讀 BigQuery 查詢。
- 需要資料夾的工具（`inspect_source`、`convert_source`、`validate_normalized`、`load_tables`）在容器裡**沒有資料**；
  要在雲端示範請設 `DATA_BUCKET=<bucket>`，腳本會把該 bucket 唯讀掛在 `/data`，對話時給代理人 `/data/<資料夾>`。
- 轉換與載入照設計仍需人在網頁上按確認（`require_confirmation=True`）。
- **聲學工具的記憶體**（`tools/acoustic.py`）：每次呼叫最多分析 `max_window_s` 秒（預設 60、上限 120）；更長的要求回
  `window_required` 與建議視窗，不讀整個檔。60 秒、48 kHz 視窗的 tracemalloc 峰值（只算 numpy 配置，實際 RSS 更高）：
  `estimate_spindle_speed` 約 220 MiB、`audio_profile` 約 154 MiB、`detect_change_points` 約 70 MiB；120 秒約加倍。
  服務設定 `--memory=4Gi --concurrency=4`：單次 60 s 轉速估計約 220 MiB（120 s 約 440 MiB），4 個同時請求在 4 GiB 內；工具對超過 60 s 的請求回 `window_required`，不會整場讀入。

## 確認會上傳哪些檔案

```bash
deploy/agent/deploy_agent.sh --stage-only            # 只組裝與檢查，不需 gcloud，保留組裝目錄並印出路徑
gcloud meta list-files-for-upload "<印出的組裝目錄>"   # gcloud 自己判定的上傳清單
```
**gcloud 尚未登入，`gcloud meta list-files-for-upload` 還沒執行過。** `--stage-only` 於 2026-10-17 在本機執行，組裝結果是下列 16 個檔案（199 KB；
代理人程式仍在開發中，數字會隨檔案增減而變）：
```
__init__.py  agent.py  credentials.py  mapping_schema.py  proposals.py  requirements.txt  spec/msm_spec.json
tools/__init__.py  tools/acoustic.py  tools/audio.py  tools/convert.py  tools/figure.py  tools/inspect_tool.py
tools/kaggle_info.py  tools/tables.py  tools/validate.py
```
實際送進 Cloud Build 的是 ADK 的暫存資料夾：上面這些檔放在 `agents/cutsound_normalizer/` 下，再加 ADK 產生的 `Dockerfile`；
ADK 部署完會自動刪掉暫存資料夾（`cli_deploy.py:874-876`）。

為什麼不用 repo 根目錄那種白名單 `.gcloudignore`（`*` 加 `!例外`）：ADK 會把 AGENT 資料夾裡的 `.gitignore`/`.gcloudignore`
逐行當成 `shutil.ignore_patterns` 的檔名樣式（`cli_deploy.py:677-705`），不支援 `!`，`*` 會把所有檔案排除。
所以白名單改在腳本裡用「只複製這幾類檔案＋檢查」實現，效果相同，且可以在本機驗證。

## 費用

- **Cloud Run**：`--min-instances=0`，沒人用就不計費；依請求期間的 vCPU 秒與 GiB 秒計費，每月有免費額度。`--max-instances=2` 限制上限。
- **Cloud Build**：每次部署建一次映像（numpy/scipy/pandas/pyarrow，預估數分鐘）；每月有免費建置分鐘數。
- **Artifact Registry**：映像存在 `<區域>-docker.pkg.dev/<專案>/cloud-run-source-deploy/`，映像約 1 GB 級，超過免費額度依 GB-月計費；舊映像要自己刪。
- **Vertex AI Gemini**：每次對話依 token 計費（代理人一次提案會呼叫多個工具、多輪推論）。公開服務會被任何人消耗。
- **BigQuery**：唯讀查詢依掃描量計費，這裡的表很小。
- 金額請以 Google Cloud 官方價目表為準；建議在專案設預算警示。

## 回復與刪除

```bash
P=endless-bolt-494801-b1; R=asia-east1; S=msm-normalizer-agent
gcloud run revisions list --service $S --project $P --region $R                      # 查有哪些 revision
gcloud run services update-traffic $S --project $P --region $R --to-revisions=<舊 revision>=100   # 退回舊版
gcloud run services update $S --project $P --region $R --no-allow-unauthenticated   # 收回公開（若曾用 --public）
gcloud run services delete $S --project $P --region $R                              # 刪除服務（不可復原，會詢問確認）
```
其餘留下的資源（需要時自行刪，都會詢問確認）：Artifact Registry 映像 `cloud-run-source-deploy/msm-normalizer-agent`、
服務帳號 `msm-normalizer-run`（`gcloud iam service-accounts delete`）與它的專案層級角色、apikey 模式的祕密 `gemini-api-key`。

## 常見問題

- **Cloud Build 權限錯誤**（新專案常見）：`--source` 部署由 Cloud Build 以預設的 Compute Engine 服務帳號建置；若錯誤訊息指出缺權限，
  依訊息給 `<專案編號>-compute@developer.gserviceaccount.com` 補 `roles/run.builder`。
- **組織政策不允許 allUsers**：`--public` 會失敗；改用預設私有＋`roles/run.invoker` 給指定帳號。
- **模型 404 / 不支援的位置**：確認 `MODEL_LOCATION`（預設 global）與 `NORMALIZER_MODEL` 在 Vertex 上可用。
- **Windows**：在 Git Bash 執行；腳本會把組裝路徑轉成 Windows 形式交給 `adk.exe`，ADK 在 Windows 會呼叫 `gcloud.cmd`。
