#!/usr/bin/env bash
# 部署 MSM 切段驗證頁到 Cloud Run（spec: design/bdd-viewer-deploy.md）。
# 先決條件（使用者本人）：gcloud auth login；有 GCP 專案且已綁帳單。
# 用法：PROJECT=<專案ID> deploy/deploy.sh        可選：REGION（預設 asia-east1）、BUCKET、BUNDLE
set -euo pipefail
PROJECT=${PROJECT:?請設定 PROJECT=<GCP 專案 ID>}
REGION=${REGION:-asia-east1}
SERVICE=msm-viewer
BUCKET=${BUCKET:-${PROJECT}-msm-viewer}
BUNDLE=${BUNDLE:-$HOME/m143359/金屬中心/datasets/MSM/viewer_bundle}
REPO=$(cd "$(dirname "$0")/.." && pwd)
TOKEN_FILE=$HOME/.config/msm-viewer/token

[ -f "$BUNDLE/sessions.json" ] || { echo "找不到資料包 $BUNDLE（先跑 proto/scripts/export_viewer_bundle.py）"; exit 1; }
gcloud config set project "$PROJECT" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com

# 資料包：私有 bucket，只有 Cloud Run 的服務帳號可讀
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access --public-access-prevention
gcloud storage rsync -r -c "$BUNDLE" "gs://$BUCKET"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role=roles/storage.objectViewer >/dev/null

# 存取碼：第一次產生後存在本機（權限 600），之後重用
if [ ! -s "$TOKEN_FILE" ]; then
  mkdir -p "$(dirname "$TOKEN_FILE")"; umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$TOKEN_FILE"
fi
TOKEN=$(cat "$TOKEN_FILE")

gcloud run deploy "$SERVICE" --source "$REPO" --region "$REGION" \
  --allow-unauthenticated --execution-environment gen2 \
  --add-volume name=bundle,type=cloud-storage,bucket="$BUCKET",readonly=true \
  --add-volume-mount volume=bundle,mount-path=/bundle \
  --set-env-vars VIEWER_TOKEN="$TOKEN" \
  --memory 512Mi --cpu 1 --max-instances 2 --concurrency 40

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "分享網址（含存取碼，請勿公開張貼）：${URL}/?k=${TOKEN}"
