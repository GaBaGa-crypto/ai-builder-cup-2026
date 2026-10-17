#!/usr/bin/env bash
# 部署 ADK 資料正規化代理人（agent/cutsound_normalizer 的 root_agent）到 Cloud Run，含 ADK Web UI。
# 說明與先決條件見 deploy/agent/README.md。本腳本不讀、不印、不寫任何 API 金鑰。
#
# 用法（在 Git Bash / Linux / macOS 執行；路徑以 repo 根目錄為準）：
#   PROJECT=<GCP 專案 ID> deploy/agent/deploy_agent.sh             預設：私有服務（需 IAM 驗證才能開）
#   PROJECT=<GCP 專案 ID> deploy/agent/deploy_agent.sh --public    明確選擇公開（任何人可開，會花你的 Gemini 額度）
#   deploy/agent/deploy_agent.sh --stage-only                      只組上傳內容並檢查，不碰雲端（不需 gcloud）
# 可選環境變數：
#   REGION=asia-east1            Cloud Run 區域
#   AUTH_MODE=vertex|apikey      vertex（預設）：服務帳號＋Vertex AI，映像與環境變數都沒有金鑰
#                                apikey：從 Secret Manager 掛 GOOGLE_API_KEY（祕密需使用者事先自建，見 README）
#   API_KEY_SECRET=gemini-api-key  apikey 模式使用的 Secret Manager 祕密名稱
#   MODEL_LOCATION=global        Vertex AI 端點位置（Gemini 3.x 走 global；與 Cloud Run 區域無關）
#   NORMALIZER_MODEL=gemini-3.8-flash
#   DATA_BUCKET=<bucket>         可選：唯讀掛載到容器 /data，讓 inspect_source 能讀雲端資料夾
#   BQ_WRITE=1                   可選：多給 bigquery.dataEditor，讓雲端的 load_tables 能寫表（預設唯讀）
#   STAGE_DIR=<dir>              可選：上傳內容的組裝目錄（預設 mktemp；--stage-only 時保留供檢查）
#   ADK=<adk 執行檔>             預設：PATH 上的 adk，否則 Anaconda audio_preject 環境的 adk.exe
# 重跑安全：API 啟用、服務帳號、IAM 綁定都是「已存在就略過／重複綁定無副作用」，部署會產生新 revision。
set -euo pipefail

SERVICE=${SERVICE:-msm-normalizer-agent}
SA_NAME=${SA_NAME:-msm-normalizer-run}
REGION=${REGION:-asia-east1}
AUTH_MODE=${AUTH_MODE:-vertex}
API_KEY_SECRET=${API_KEY_SECRET:-gemini-api-key}
MODEL_LOCATION=${MODEL_LOCATION:-global}
NORMALIZER_MODEL=${NORMALIZER_MODEL:-gemini-3.8-flash}
DATA_BUCKET=${DATA_BUCKET:-}
BQ_WRITE=${BQ_WRITE:-0}
ADK_VERSION=2.11.0           # 與 agent/requirements.txt 的 google-adk 釘版一致
APP_NAME=cutsound_normalizer # ADK app 名＝資料夾名＝Python 套件名（__init__.py 用相對匯入）

PUBLIC=0
STAGE_ONLY=0
for a in "$@"; do
  case "$a" in
    --public) PUBLIC=1 ;;
    --stage-only) STAGE_ONLY=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "未知參數：$a（可用 --public、--stage-only、--help）" >&2; exit 2 ;;
  esac
done
case "$AUTH_MODE" in vertex|apikey) ;; *) echo "AUTH_MODE 只能是 vertex 或 apikey" >&2; exit 2 ;; esac

REPO=$(cd "$(dirname "$0")/../.." && pwd)
SRC="$REPO/agent/$APP_NAME"
REQ="$REPO/agent/requirements.txt"
[ -f "$SRC/agent.py" ] && [ -f "$SRC/__init__.py" ] || { echo "找不到代理人程式 $SRC" >&2; exit 1; }
[ -f "$REQ" ] || { echo "找不到 $REQ" >&2; exit 1; }

# Windows（Git Bash）上 adk.exe 是原生程式，路徑要轉成 Windows 形式
to_native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s\n' "$1"; fi; }

# ---------------------------------------------------------------------------
# 1. 組裝上傳內容（白名單複製，不靠忽略規則）
#    adk deploy cloud_run 只會把 AGENT 資料夾複製進映像（agents/<app>/），並安裝該資料夾內的 requirements.txt；
#    我們的 requirements.txt 在上一層，所以先組一個乾淨資料夾：只放 *.py、spec/msm_spec.json、requirements.txt。
#    注意：不能在這個資料夾放白名單式 .gcloudignore（'*' 開頭）——ADK 會把它當成 shutil.ignore_patterns，
#    '*' 會把所有檔案都排除（google/adk/cli/cli_deploy.py:_get_ignore_patterns_func）。
# ---------------------------------------------------------------------------
if [ -n "${STAGE_DIR:-}" ]; then
  STAGE_ROOT="$STAGE_DIR"
  mkdir -p "$STAGE_ROOT"
  [ -z "$(ls -A "$STAGE_ROOT" 2>/dev/null)" ] || { echo "STAGE_DIR 必須是空資料夾：$STAGE_ROOT" >&2; exit 1; }
  OWN_STAGE=0
else
  STAGE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/msm-agent-stage.XXXXXX")
  OWN_STAGE=1
fi
STAGE="$STAGE_ROOT/$APP_NAME"
cleanup() {
  # 只清自己用 mktemp 建的組裝目錄；--stage-only 時保留給使用者檢查
  if [ "$OWN_STAGE" = 1 ] && [ "$STAGE_ONLY" = 0 ] && [ -d "$STAGE_ROOT" ]; then
    case "$STAGE_ROOT" in *msm-agent-stage.*) rm -r -- "$STAGE_ROOT" ;; esac
  fi
}
trap cleanup EXIT

mkdir -p "$STAGE/spec"
(cd "$SRC" && find . -type f -name '*.py' -not -path '*/__pycache__/*' -print) | while IFS= read -r f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$SRC/$f" "$STAGE/$f"
done
cp "$SRC/spec/msm_spec.json" "$STAGE/spec/msm_spec.json"
cp "$REQ" "$STAGE/requirements.txt"

echo "== 上傳內容檢查（會進映像 /app/agents/$APP_NAME/ 的檔案）"
FILES=$(cd "$STAGE" && find . -type f | sed 's|^\./||' | LC_ALL=C sort)
printf '%s\n' "$FILES"
BAD=0
while IFS= read -r f; do
  case "$f" in
    requirements.txt|spec/msm_spec.json) ;;                 # 白名單的兩個非程式檔
    *.py) ;;                                                 # 程式
    *) echo "不允許的檔案：$f" >&2; BAD=1 ;;
  esac
  case "/$f" in
    */work/*|*/_tables/*|*/_manifest/*|*/.env|*/.adk/*) echo "不允許的路徑（資料／工作區／環境檔）：$f" >&2; BAD=1 ;;
  esac
done <<< "$FILES"
# 金鑰樣式掃描：只列檔名，不印內容（Google API key 以 AIza 開頭；私鑰區塊；服務帳號 JSON）
if grep -rlE 'AIza[0-9A-Za-z_-]{30,}|AQ\.[0-9A-Za-z_-]{40,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"private_key_id"' "$STAGE" >/dev/null 2>&1; then
  echo "疑似金鑰內容出現在以下檔案，停止：" >&2
  grep -rlE 'AIza[0-9A-Za-z_-]{30,}|AQ\.[0-9A-Za-z_-]{40,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"private_key_id"' "$STAGE" >&2
  BAD=1
fi
SIZE_KB=$(du -sk "$STAGE" | cut -f1)
[ "$SIZE_KB" -le 2048 ] || { echo "上傳內容 ${SIZE_KB} KB 超過 2 MB，疑似夾帶資料，停止" >&2; BAD=1; }
[ "$BAD" = 0 ] || exit 1
echo "== 檢查通過：$(printf '%s\n' "$FILES" | wc -l | tr -d ' ') 個檔案，${SIZE_KB} KB"

if [ "$STAGE_ONLY" = 1 ]; then
  echo "== 只組裝不部署。組裝目錄：$(to_native "$STAGE")"
  echo "   用 gcloud 確認會上傳的清單：gcloud meta list-files-for-upload \"$(to_native "$STAGE")\""
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. 雲端準備（需要 gcloud auth login；PROJECT 必填）
# ---------------------------------------------------------------------------
PROJECT=${PROJECT:?請設定 PROJECT=<GCP 專案 ID>}
command -v gcloud >/dev/null 2>&1 || { echo "找不到 gcloud，請先安裝 Google Cloud SDK（README 第 1 步）" >&2; exit 1; }
if [ -z "${ADK:-}" ]; then
  if command -v adk >/dev/null 2>&1; then ADK=adk
  elif [ -x "$HOME/AppData/Local/anaconda3/envs/audio_preject/Scripts/adk.exe" ]; then
    ADK="$HOME/AppData/Local/anaconda3/envs/audio_preject/Scripts/adk.exe"
  else echo "找不到 adk（pip install google-adk==$ADK_VERSION，或設 ADK=<路徑>）" >&2; exit 1; fi
fi
ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n1)
[ -n "$ACCOUNT" ] || { echo "gcloud 尚未登入：請先執行 gcloud auth login" >&2; exit 1; }
echo "== gcloud 帳號：$ACCOUNT；專案：$PROJECT；區域：$REGION；認證模式：$AUTH_MODE"

SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
APIS="run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com bigquery.googleapis.com iam.googleapis.com"
[ "$AUTH_MODE" = apikey ] && APIS="$APIS secretmanager.googleapis.com"
[ -n "$DATA_BUCKET" ] && APIS="$APIS storage.googleapis.com"
echo "== 啟用 API：$APIS"
# shellcheck disable=SC2086
gcloud services enable $APIS --project "$PROJECT" --quiet

echo "== 專屬服務帳號 $SA"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name="MSM normalizer agent (Cloud Run)" --quiet

# 新建的服務帳號要幾秒到一分鐘才會在 IAM 生效，失敗就等一下重試（同 deploy/deploy.sh）
retry_bind() {
  local desc=$1; shift
  local i
  for i in 1 2 3 4 5 6 7 8; do
    if "$@" >/dev/null 2>&1; then echo "   已授予：$desc"; return 0; fi
    [ "$i" = 8 ] && { echo "授權失敗：$desc" >&2; return 1; }
    echo "   服務帳號尚未生效，${i}0 秒後重試（$desc）"; sleep $((i * 10))
  done
}

# 最小權限：呼叫 Gemini（Vertex AI）＋ BigQuery 唯讀查詢（代理人的 BigQueryToolset 是 WriteMode.BLOCKED）
ROLES="roles/aiplatform.user roles/bigquery.jobUser roles/bigquery.dataViewer"
[ "$BQ_WRITE" = 1 ] && ROLES="$ROLES roles/bigquery.dataEditor"
for r in $ROLES; do
  retry_bind "$r（專案層級）" gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" --role="$r" --condition=None --quiet
done

# MSYS/Git Bash would rewrite "/tmp/..." and "/data" into Windows paths before gcloud sees them; disable that here.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
EXTRA_ENV=(--env "NORMALIZER_MODEL=$NORMALIZER_MODEL" --env "NORMALIZER_WORK_DIR=/tmp/normalizer_work" --env "NORMALIZER_DATA_ROOT=/data")
EXTRA_GCLOUD=()
if [ "$AUTH_MODE" = vertex ]; then
  # Vertex：ADK 預設就設 GOOGLE_GENAI_USE_ENTERPRISE=1、GOOGLE_CLOUD_PROJECT、GOOGLE_CLOUD_LOCATION=<區域>；
  # 這裡把 LOCATION 改成 MODEL_LOCATION（--env 的值優先，見 _cloud_run_deployer.py:_add_gcp_env_vars）
  EXTRA_ENV+=(--env "GOOGLE_GENAI_USE_ENTERPRISE=1" --env "GOOGLE_CLOUD_LOCATION=$MODEL_LOCATION")
else
  # API 金鑰：祕密由使用者事先建立；本腳本只檢查祕密存在、授權讀取、把它掛成環境變數，從不讀取值
  gcloud secrets describe "$API_KEY_SECRET" --project "$PROJECT" >/dev/null 2>&1 || {
    echo "找不到 Secret Manager 祕密 '$API_KEY_SECRET'。請依 README「API 金鑰模式」自行建立後重跑。" >&2; exit 1; }
  retry_bind "secretmanager.secretAccessor（$API_KEY_SECRET）" gcloud secrets add-iam-policy-binding "$API_KEY_SECRET" \
    --project "$PROJECT" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor --quiet
  EXTRA_ENV+=(--env "GOOGLE_GENAI_USE_ENTERPRISE=0")
  EXTRA_GCLOUD+=("--update-secrets=GOOGLE_API_KEY=${API_KEY_SECRET}:latest")
fi

if [ -n "$DATA_BUCKET" ]; then
  retry_bind "storage.objectViewer（gs://$DATA_BUCKET）" gcloud storage buckets add-iam-policy-binding "gs://$DATA_BUCKET" \
    --member="serviceAccount:$SA" --role=roles/storage.objectViewer
  EXTRA_GCLOUD+=("--add-volume=name=data,type=cloud-storage,bucket=$DATA_BUCKET,readonly=true"
                 "--add-volume-mount=volume=data,mount-path=/data")
fi

if [ "$PUBLIC" = 1 ]; then
  echo "== 注意：--public 會讓任何人不需登入就能開 ADK Web UI（開發用介面），每次對話都花你的 Gemini 額度"
  EXTRA_GCLOUD+=(--allow-unauthenticated)
else
  EXTRA_GCLOUD+=(--no-allow-unauthenticated)
fi

# ---------------------------------------------------------------------------
# 3. 部署：adk deploy cloud_run 產生 Dockerfile（python:3.11-slim、非 root、移除 dev_server）並呼叫
#    gcloud run deploy --source（Cloud Build 在雲端建 x86 映像）。'--' 之後的參數原樣傳給 gcloud。
#    --source/--project/--region/--port/--update-env-vars/--verbosity 由 ADK 管理，這裡不能再給。
# ---------------------------------------------------------------------------
echo "== 部署 Cloud Run 服務 $SERVICE（ADK $ADK_VERSION，含 Web UI）"
"$ADK" deploy cloud_run \
  --project "$PROJECT" --region "$REGION" \
  --service_name "$SERVICE" --app_name "$APP_NAME" \
  --adk_version "$ADK_VERSION" --with_ui --log_level info \
  "${EXTRA_ENV[@]}" \
  "$(to_native "$STAGE")" \
  -- \
  --service-account="$SA" \
  --execution-environment=gen2 \
  --memory=4Gi --cpu=2 --min-instances=0 --max-instances=2 --concurrency=4 --timeout=600 \
  --labels=app=msm-normalizer \
  --quiet \
  "${EXTRA_GCLOUD[@]}"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo "== 服務網址：$URL"
if [ "$PUBLIC" = 1 ]; then
  echo "   直接開：$URL/dev-ui/"
else
  echo "   私有服務，用本機代理開（會用你的 gcloud 身分）："
  echo "   gcloud run services proxy $SERVICE --project $PROJECT --region $REGION --port 8080"
  echo "   然後瀏覽 http://localhost:8080/dev-ui/"
fi
