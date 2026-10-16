# BDD：Agent 1 線上提對映與 V6 評分（2026-10-16）

> 範圍：第一支代理人（資料轉換與正規化）的線上部分：`scripts/propose_mapping.py` 讓 Gemini 對新資料提出對映、`scripts/eval_mapping.py` 對人工參照逐檔評分。離線轉換（`scripts/normalize.py`）的場景見 README。
> 不變式：代理人只提對映，不改資料；提案永遠不會自動套用；金鑰只以環境變數名稱出現在文件，值不進 repo、log 或報告。
> 示範資料：Kaggle Cutting Tool Wear Audio（CTWA，402 檔）；參照答案 `agent/mappings/ctwa_reference.json`（不得修改）。

## 場景

**A1 提案存檔、等待核准**
- Given 有效的 Gemini 憑證（`GOOGLE_API_KEY` 或 `GOOGLE_API_KEY_FILE`）與 CTWA 資料夾 When 執行 `propose_mapping.py <CTWA> --kaggle nachiketsoni/cutting-tool-wear-audio-dataset --source-id ctwa` Then 代理人自行呼叫 `get_msm_spec`、`get_mapping_schema`、`inspect_source`、`kaggle_dataset_info`、`save_mapping`；`save_mapping` 回 `status=saved_pending_approval`；`$NORMALIZER_WORK_DIR/mappings/ctwa.json` 通過 `Mapping` schema；`approved_by` 為 null；過程中**沒有**呼叫 `convert_source`、`load_tables`；exit 0。
- 驗收：`--log` 檔內有 `== tool sequence:` 一行且含上述工具、不含 `convert_source`；`Mapping.model_validate_json(檔案)` 不報錯；`approved_by is None`。

**A2 來源不明的標籤要列為待答問題**
- Given Kaggle 說明頁只說「cutting tool wear」與刀具字母，沒有說明中度磨損或斷刀時切削本身是否異常 When 代理人提出 MODERATE、BROKEN 的標籤規則 Then 這兩條的 `msm_label` 為 -1，且 `unresolved` 至少各有一題點名 MODERATE、BROKEN；若給了 0／1／2 卻沒有對應問題，判為誠實性失敗。
- 驗收：讀提案 JSON，`{r.native: r.msm_label}` 中 MODERATE、BROKEN 為 -1，或 `unresolved` 中有含該 token 的問題。

**A3 來源未提供的工況不得編造**
- Given 說明頁未寫進給、切深、切寬、刃數、銑削方向 When 代理人提出欄位規則 Then `ap`、`ae`、`F`、`Z`、`direction` 的 `source` 為 `unknown`；`N`、`D`、`Workpiece` 有值時 `evidence` 引用說明頁原文。
- 驗收：欄位規則逐條檢查 `source`；N／D／Workpiece 的 evidence 引句片段為 `kaggle_dataset_info` 回傳 description 的子字串。

**A4 沒有憑證時明說未執行、不產生假檔**
- Given 環境中沒有 `GOOGLE_API_KEY`、`GOOGLE_API_KEY_FILE`，也沒有 Vertex 設定 When 執行 `propose_mapping.py` Then 印出 `NOT RUN: no Gemini credentials ...`、exit 非 0（3），且 `mappings/ctwa.json` 不被建立、修改時間不變。
- 變體：`GOOGLE_API_KEY_FILE` 指向不存在的檔案 → `NOT RUN: agent could not be loaded (FileNotFoundError ...)`，exit 3；模型名稱錯誤或 API 錯誤 → `FAILED: Gemini run raised ...`，exit 1；代理人沒存出合格對映 → exit 2。任一失敗皆不寫提案檔。
- 驗收：執行前後比對提案檔 mtime 與 sha256 相同；exit code ≠ 0。

**A5 評分報告欄位**
- Given 代理人的提案與參照對映 When 執行 `eval_mapping.py <CTWA> <提案> agent/mappings/ctwa_reference.json --report v6_report.json` Then 報告 JSON 含 `n_paths`（402）、`audio_selection_recall`、`extra_paths_selected`、`agreement` 的 `label`、`cut_state`、`N`、`ap`、`ae`、`F`、`Z`、`D`、`Tool`、`Workpiece`、`direction` 各一個 0–1 數值、`label_rules`（reference 與 proposed）、`proposed_unresolved`、各欄前 3 筆 `disagreement_examples`（檔名＋兩邊的值）。
- 驗收：`json.load` 後檢查上述鍵齊全，agreement 11 個鍵皆為數值。

**A6 金鑰不外流**
- Given 執行過 A1–A5 When 以程式從環境取金鑰前 6 字元，對 repo 工作樹與 harness 資料夾逐檔搜尋 Then 命中數為 0；log 與報告只出現環境變數名稱。
- 驗收：搜尋程式只印命中數，不印金鑰或前綴。

## 已知限制
- `eval_mapping.py` 只比檔案層級規則，不比 `interval_labels`。
- label 一致率把「-1＋待答問題」（棄權）與「給錯數字」都算不一致；報告需另外分開說明。

## Sprint 3 追加場景（2026-10-16，E1 缺陷修正，Generator G4）

**A7 評分只對文字欄寬鬆**（E1 D3／MAJOR-3）
- Given 提案 `Workpiece="Mild-Steel"`、`D=16.5`，參照 `"mild steel"`、`16` When 執行 `eval_mapping.py` Then `agreement.Workpiece=1.0`（Tool、Workpiece、direction 忽略大小寫與 `-`／`_`／空白差異），`agreement.D=0.0`（N、ap、ae、F、Z、D 經單位換算後須完全相同）；報告 `comparison` 鍵寫明哪些欄寬鬆、哪些精確。

**A8 編號穩定**（E1 D1／MAJOR-1）
- Given MSM 恆等對映（grouping `msm_session`）When 正規化 Then 輸出 `dataset<N>` 與原始資料夾編號相同（37/37），`_manifest/dataset_index.csv` 37 列；來源資料夾不叫 `dataset<N>` 或兩個資料夾給出同一 N → 轉換報錯、不產生輸出。
- Given CTWA（grouping `per_file`）When 正規化 Then N＝sorted(session_id) 的名次（不透明）；`dataset_index.csv` 402 列（`dataset,session_id,source_relative_path`），來源路徑只在 manifest，不進 session.json。同一檔案集合重跑結果相同；增刪檔會重編，穩定鍵是 session_id（README 已註明）。
- validate：`msm_session` 檢查 N 等於來源資料夾編號；`per_file` 檢查 1..n 無缺號且依 session_id 排序；`dataset_index.csv` 與資料夾一一對應。

**A9 棄權的切削不得變成非切削**（E1 D2／MAJOR-2）
- Given 對映把 MODERATE 定為 `cut_state=cutting`、`msm_label=-1`（`eval_e1/fixture/mapping_abstain.json`）When 正規化並建庫 Then `label.csv` 只有表頭（仍只含 0／1／2），另有 `<session_id>_unlabeled.csv`（`start,end`）；庫內該區間 `events.cut_state=cutting`、`labels.label_std=-1`；驗證頁顯示紫色「未標註切削」。沒有這種區間的場次不寫側檔（CTWA 參照對映 402 場 0 個側檔）。

**A10 來源資料夾的分析檔不得被當成資料**
- Given 來源下有 `_analysis/…/*.wav` 或 `.cache/*.wav`，對映 glob 為 `**/*.wav` When 正規化 Then 這些檔不成為場次（與 `inspect_source` 同規則：任何路徑元件以 `_` 或 `.` 開頭即略過）。

**A11 不可用的數值**
- Given 對映常數或路徑擷取到 `nan`、`inf`、非數字 When 轉換 Then 該欄留空；validate 對 label.csv 的 `nan`／`inf` 回報 `not a finite number`。

## Sprint 3 收尾追加場景（2026-10-16，E2 缺陷修正，Generator G5）

**A12 direction 精確比對**（E2-3；取代 A7 中 direction 寬鬆的部分）
- Given 提案 `direction="up"`、參照 `"UP"` When 執行 `eval_mapping.py` Then `agreement.direction=0.0`（direction 是 UP／DOWN 列舉，validate 也拒收 `up`，評分不得高估）；`Workpiece="mild-steel"` 對 `"mild steel"` 仍為 1.0（只有 Tool、Workpiece 寬鬆）。報告 `comparison` 為 `text_fields_loose=[Tool, Workpiece]`、`enum_fields_exact=[direction]`、`numeric_fields_exact=[N, ap, ae, F, Z, D]`。

**A13 評分與轉換選同一批檔**（E2-4）
- Given 來源下有 `_scratch/x.wav`（或任何以 `_`／`.` 開頭的路徑元件）When 執行 `eval_mapping.py` Then 這些檔不計入 `n_paths`、也不計入提案選檔；`n_paths` 等於 convert 的場次數（同一個 `_skipped()` 規則）。

**A14 切削狀態未知不得變成非切削**（E2-6）
- Given 對映規則 `cut_state=unknown`（或檔名沒有對應規則）When 正規化並建庫 Then 側檔 `<session_id>_unlabeled.csv` 有一列 `start,end,unknown`；庫內 `events.cut_state=unknown`、`label_std=-1`；驗證頁顯示「切削狀態未知」，不是非切削。
