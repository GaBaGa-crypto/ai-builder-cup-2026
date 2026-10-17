#!/usr/bin/env bash
# 部署前的本機檢查（不碰雲端、不需 gcloud、不讀金鑰）。
#   deploy/agent/check_local.sh           1) 匯入 root_agent 並列出工具（預期至少 8 個）  2) 組裝上傳內容並檢查
#   deploy/agent/check_local.sh --serve   另外印出本機啟動 ADK Web UI 的冒煙測試步驟（只印，不啟動）
# PY 可覆寫 Python 路徑；預設為 Anaconda audio_preject 環境（專案規範）。
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PY=${PY:-$HOME/AppData/Local/anaconda3/envs/audio_preject/python.exe}
[ -x "$PY" ] || PY=$(command -v python3)

echo "== 1. 匯入 root_agent（清掉金鑰與專案變數：只驗程式結構，不呼叫 Gemini）"
# 不設 GOOGLE_CLOUD_PROJECT 時代理人不掛 BigQueryToolset，工具數至少 8 個（核心 8 個；聲學等工具接上後更多）；
# 雲端（ADK 會設 GOOGLE_CLOUD_PROJECT）會多一個唯讀 BigQueryToolset。
(cd "$REPO/agent" && env -u GOOGLE_API_KEY -u GOOGLE_API_KEY_FILE -u GEMINI_API_KEY -u GOOGLE_CLOUD_PROJECT \
  PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 "$PY" -X utf8 -c '
from cutsound_normalizer.agent import root_agent
names = [getattr(t, "name", type(t).__name__) for t in root_agent.tools]
print(f"agent={root_agent.name} model={root_agent.model} tools={len(names)}")
for n in names:
    print("  -", n)
assert len(names) >= 8, f"expected at least 8 tools, got {len(names)}"
')

echo "== 2. 組裝上傳內容並檢查（deploy_agent.sh --stage-only）"
bash "$REPO/deploy/agent/deploy_agent.sh" --stage-only

if [ "${1:-}" = "--serve" ]; then
  cat <<'EOF'
== 3. 本機冒煙測試（手動執行；需要 Gemini 認證，二擇一）
   Vertex：gcloud auth application-default login
           export GOOGLE_GENAI_USE_ENTERPRISE=1 GOOGLE_CLOUD_PROJECT=<專案 ID> GOOGLE_CLOUD_LOCATION=global
   金鑰：  export GOOGLE_API_KEY_FILE=<金鑰檔路徑>   （代理人啟動時讀取，不印、不寫）
   在 repo 根目錄（提案會存到 NORMALIZER_WORK_DIR，預設 ./work，已被 .gitignore 排除）：
     adk web --port 8800 agent
   瀏覽 http://127.0.0.1:8800/dev-ui/ ，左上選 cutsound_normalizer，輸入例如：
     「請讀 Kaggle 資料集 nachiketsoni/cutting-tool-wear-audio-dataset 的說明，列出它缺哪些 MSM 欄位」
   通過條件：右側事件面板出現 get_msm_spec / kaggle_dataset_info 等工具呼叫與回傳。
   要模擬雲端容器的啟動方式（api_server + UI），對組裝目錄的上一層執行：
     adk api_server --with_ui --port 8800 <組裝目錄的上一層>
   結束：Ctrl+C。
EOF
fi
