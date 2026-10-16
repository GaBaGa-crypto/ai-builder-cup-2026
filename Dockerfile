# MSM 切段驗證頁（Cloud Run）。只含標準庫伺服器；資料包由 Cloud Storage volume 掛在 /bundle（見 deploy/deploy.sh）。
FROM python:3.12-slim
WORKDIR /app
COPY proto/scripts/http_range.py proto/scripts/viewer_bundle_server.py ./
ENV BUNDLE_DIR=/bundle PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "viewer_bundle_server.py"]
