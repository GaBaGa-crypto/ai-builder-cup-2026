#!/usr/bin/env bash
# 部署 MSM 切段驗證頁到 Cloud Run（spec: design/bdd-viewer-deploy.md）。
# 先決條件（使用者本人）：gcloud auth login；GCP 專案已綁帳單。
# 用法：PROJECT=<專案ID> deploy/deploy.sh        可選：REGION（預設 asia-east1）、BUCKET、BUNDLE
# 順序：先傳頁面與場次資料（小）→ 部署（網址先通）→ 再傳音檔（大，熱點上要很久；中斷可重跑，rsync 只補差異）
set -euo pipefail
PROJECT=${PROJECT:?請設定 PROJECT=<GCP 專案 ID>}
REGION=${REGION:-asia-east1}
SERVICE=msm-viewer
SA_NAME=msm-viewer-run
BUCKET=${BUCKET:-${PROJECT}-msm-viewer}
BUNDLE=${BUNDLE:-$HOME/m143359/金屬中心/datasets/MSM/viewer_bundle}
REPO=$(cd "$(dirname "$0")/.." && pwd)
TOKEN_FILE=$HOME/.config/msm-viewer/token
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

[ -f "$BUNDLE/sessions.json" ] || { echo "找不到資料包 $BUNDLE（先跑 proto/scripts/export_viewer_bundle.py）"; exit 1; }
gcloud config set project "$PROJECT" >/dev/null
echo "== 啟用 API"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  storage.googleapis.com iam.googleapis.com compute.googleapis.com

echo "== 私有 bucket 與專屬服務帳號（只能讀這個 bucket）"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="MSM viewer (Cloud Run, read-only bundle)"
# 新建的服務帳號要幾秒到一分鐘才會在 IAM 生效，授權失敗就等一下重試
for i in 1 2 3 4 5 6 7 8; do
  if gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectViewer >/dev/null 2>&1; then
    echo "bucket 讀取權限已授予 $SA"; break
  fi
  [ "$i" = 8 ] && { echo "授權失敗：$SA"; exit 1; }
  echo "服務帳號尚未生效，${i}0 秒後重試"; sleep $((i * 10))
done

echo "== 上傳頁面與場次資料（不含音檔）"
gcloud storage rsync -r -c --exclude='^audio/' "$BUNDLE" "gs://$BUCKET"

# 存取碼：第一次產生後存在本機（權限 600），之後重用
if [ ! -s "$TOKEN_FILE" ]; then
  mkdir -p "$(dirname "$TOKEN_FILE")"; umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$TOKEN_FILE"
fi
TOKEN=$(cat "$TOKEN_FILE")

echo "== 部署 Cloud Run（Cloud Build 在雲端建 x86 映像）"
gcloud run deploy "$SERVICE" --source "$REPO" --region "$REGION" --quiet \
  --service-account "$SA" --allow-unauthenticated --execution-environment gen2 \
  --add-volume name=bundle,type=cloud-storage,bucket="$BUCKET",readonly=true \
  --add-volume-mount volume=bundle,mount-path=/bundle \
  --set-env-vars VIEWER_TOKEN="$TOKEN" \
  --memory 512Mi --cpu 1 --max-instances 2 --concurrency 40

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "$URL" > "$(dirname "$TOKEN_FILE")/url"
echo "== 服務已上線（音檔上傳完前，播放會失敗）：$URL"

echo "== 上傳音檔（約 1.4 GB）"
gcloud storage rsync -r -c "$BUNDLE/audio" "gs://$BUCKET/audio"
echo "== 完成。分享網址（含存取碼，請勿公開張貼）存在 $(dirname "$TOKEN_FILE")/；網址：$URL/?k=<存取碼>"
