# BDD：第 2 步建庫（投件版，2026-10-14，GB10）

> 依據：`model_map.html` 第 2 步、`design/11` G 節與 B5／D3／D9、`design/04` §2／§3／§11.1。
> 範圍：只做投件版「簡化三庫」。不做切分（第 3 步）、不抽 BEATs（第 4 步）。
> 輸入：`金屬中心/datasets/MSM/raw/`（10/14 已驗證 388 檔）。輸出：`金屬中心/datasets/MSM/db/`（不在專案資料夾，不進公開 repo）。

## 0. 建庫前已核實的資料事實（主幹 10/14 實測）
- 三台的 label.csv 列彼此**不重疊**；VF-10 異常區間是獨立列，不嵌在正常路徑內 → 每列 label＝一個事件。
- 同場所有感測器檔名時間戳相同 → 名目偏移 0；mtc 結束比 s0 早 0.04–0.14 秒；所有 label 都在 s0 時長內。
- 負功率只在 VMX30Ui：579 筆、17 場；VM20i 0 筆；VF-10 無功率欄。
- mtc 欄位：VM20i 與 VMX30Ui 幾乎相同（VMX 多 A／C 軸與溫度）；VF-10 只有 `s_load`、`s_speed`、`execution`、`program`、`tool_num`、位置，無進給、無功率、無各軸負載、無振動。

## 1. 三庫與權限

| 檔 | 內容 | 誰能讀 |
|---|---|---|
| `db/main.sqlite` | sessions（不透明編號）、events、context_ts、alignment；**零標籤、零來源資訊** | 模型、代理人工具（唯讀烘進映像） |
| `db/source/source.sqlite` | 場次編號對照（機台＋dataset 號＋檔名）、程式名、NC 全文、dataset_summary 備註、刀具原始字串（含星號）、mtc 的 `Current_Tool`／`tool_num`／`Block_Number`／程式名欄 | 只有主幹分析；**不進映像、不給任何工具** |
| `db/sealed/labels.sqlite` | 每事件 `label_std`（0／1／2／−1）、`label_native`、`tool_defective` | 只有第 3 步切分與第 7 步封存評估程式 |
| `db/features_allowlist.json` | 准進模型的欄位清單（design/04 §11.1） | 模型載入器唯一依據 |

## 2. 場景

**S1 事件分域**
- Given raw 三台 69 場 When 建庫 Then `events` 中 `cut_state='cutting'` 的列數＝label.csv 總列數 2575（VM20i 1585、VMX30Ui 927、VF-10 63），每列帶 `domain`＝`lab`（imi_*）或 `line`（tmf_vf10）。
- 驗收：SQL 計數逐機台相符。

**S2 非切削另成一類**
- Given 場次內相鄰 label 之間、首段之前、末段之後的未標時間 When 長度 ≥ 0.5 秒 Then 寫成 `cut_state='non_cutting'` 事件；其標籤在封存庫為 −1，**不得為 0**。
- 驗收：labels.sqlite 中 non_cutting 事件的 label_std 全為 −1；每場 cutting＋non_cutting（含 <0.5 秒捨去量）總長＝s0 時長 ±0.5 秒×(段數+1)。

**S3 主庫零標籤**
- Given main.sqlite When 掃描全部表的欄名 Then 不含 `label`、`defect`、`anomaly`、`remark`、`program`、`nc`、`tool_raw`、`block`、`current_tool`、`tool_num`（大小寫不拘）。
- 驗收：自動 schema 掃描 0 命中。

**S4 洩漏字串掃描**
- Given source.sqlite 中所有程式名、NC 首行、備註、刀具原始字串、原始檔名、dataset 號 When 對 main.sqlite 每個文字格做子字串比對 Then 0 命中；main 中任何文字格不含 `*`、`EXP`、`defect`（大小寫不拘）。
- 驗收：自動掃描 0 命中。

**S5 允許工況欄**
- Given features_allowlist.json When 比對 events 與 context_ts 欄名 Then 每個欄名屬於 allowlist、或屬於明列的「識別／時間／品質」欄（event_id、session_id、start、end、dur、domain、cut_state、clip_ratio、rms_dbfs 等），沒有第三類。
- 允許工況：`N`、`F`、`ap`、`ae`、`direction`、`Z`、`D`、`material`、`tool_class`（去星號）、`coolant`；反應欄（只給觀察者二與 C2）：主軸負載、各軸負載、功率、振動。

**S6 平凡基準線 T 的欄位**
- Given 每個事件 When 讀 s0 Then 記 `clip_ratio`（滿刻度樣本比例）、`rms_dbfs`、`dur`。
- 驗收：cutting 事件的 clip_ratio 與 `inventory/paths.csv` 逐列相等（誤差 < 1e-5）；以 label 1 對 0 重算削波 AUC＝0.991／0.859／0.571。

**S7 對齊偏移、漂移、信心**
- Given 每場 s0 與 mtc When 以音訊 log-RMS 包絡（0.1 秒格）分別對「主軸轉速」與「主軸負載」做前半、後半兩段互相關（延遲 ±6 秒），取 conf 較高的錨點 Then 寫 `alignment(session_id, nominal_offset_s, offset_head_s, offset_tail_s, drift_s_per_h, conf, halves_used, at_lag_bound, anchor)`；conf＝有資訊半段（錨點有變化）峰值相關的較小者。
- 驗收：69 場都有一列；conf 低於 0.3 的場次列出（第 3 步決定是否排除於觀察者二與 C2）。此步只記錄，**不修正時間軸**。
- **實作中修正（10/14 主幹實測）**：原寫只用主軸負載，結果實驗室 49／56 場 conf < 0.3。抽 3 場查證：實驗室濕切鋁件主軸負載近乎平坦，音訊包絡跟主軸轉速相關 0.66–0.95、跟負載 −0.2–0.01。改為轉速與負載兩錨點擇優（即 model_map 原文「主軸啟停與負載變化」）；延遲由 ±3 改 ±6 秒後實驗室無場次卡在邊界。
- **已知限制**：VF-10 主軸長時間連續運轉，相關峰寬，conf 高但偏移估計不穩（延遲範圍 ±3→±6 秒時前半中位偏移由 −1.1 變 +0.6 秒）。conf 量的是相關強度，不是峰的銳利度；VF-10 偏移值只作參考。

**S8 負功率設缺值**
- Given mtc 的 watt When 值 < 0 Then context_ts 的 `power_w` 為 NULL。
- 驗收：context_ts 中 power_w < 0 的筆數＝0；VMX30Ui 的 power_w NULL 數比原始缺值多 579。

**S9 控制器時序正規化**
- Given 三台不同欄名 When 寫入 context_ts Then 統一為 `t, spindle_speed_rpm, feed_rate, executing, spindle_load, x_load, y_load, z_load, power_w, s_vrms, x_vrms, y_vrms, z_vrms`；機台沒有的欄為 NULL。程式名、區塊號、刀號只進 source.sqlite。

**S10 不透明場次編號**
- Given 機台與 dataset 號 When 產生 session_id Then 形如 `vmx30ui_3f9a1c`（機台短名＋SHA1 前 6 碼），對照只在 source.sqlite；同輸入重跑得同編號。

**S11 可重現與不寫入 raw**
- Given 同一份 raw When 連跑兩次 Then main.sqlite 每表內容的 SHA256 相同；raw 底下檔案的 mtime 與數量不變。

**S12 權限**
- Given 建庫完成 Then `db/source/`、`db/sealed/` 權限 0700，內含檔 0600。
- 已知限制：GB10 只有一個使用者帳號且無 sudo，0700 擋不住同帳號程式；**真正護欄是部署時 Cloud Run 映像只放 main.sqlite**，第 8 步打包時須驗證映像內不含 source／sealed。

## 3. 積木對照表

| 需要的能力 | 既有積木 | 動作 |
|---|---|---|
| 讀 wav 基本資訊 | `wav_info`（inventory.py:34） | **複用**：搬到新模組 `msm_io.py`，inventory.py 改為 import（第 2 個呼叫者出現＝真接縫） |
| 片段削波比例 | `clip_ratio`（inventory.py:39） | **複用**：同上搬入 msm_io；S6 用 inventory 輸出逐列比對證明行為不變 |
| 讀 dataset_summary.xlsx | inventory.py:26–31 頂層程式碼 | **擴充**：抽成 `read_summary()` 放 msm_io（原為頂層程式，無法 import） |
| 走訪場次資料夾、讀 label.csv | inventory.py:52–68 頂層程式碼 | **擴充**：抽成 `iter_sessions()`、`read_labels()` 放 msm_io |
| 讀 mtc 單欄並容錯轉數字 | `col`（align_plot.py:46） | **不複用**：它是閉包、逐列 Python 迴圈、只服務繪圖；建庫改用 pandas `to_numeric(errors='coerce')`，並在 msm_io 提供 `read_mtc()`。align_plot.py 不動（單一呼叫者，不抽） |
| 主軸負載對音訊對齊 | 無 | **新建** `estimate_alignment()`（build_db.py） |
| 三庫寫入、洩漏掃描、權限 | 無 | **新建** build_db.py；洩漏掃描另寫 `check_db.py` 作為驗收程式（獨立於建庫程式，避免自評） |

重構後驗收：inventory.py 重跑，`sessions.csv`、`paths.csv`、`summary.md` 與重構前逐位元相同。

## 4. 不在本步範圍
切分與測試集指定（第 3 步）；標籤 2 怎麼用（第 3 步）；BEATs 嵌入（第 4 步，權重未到）；acc、影片（未下載）。

## 5. v2：多根目錄與正規化場次（2026-10-16，harness agent1-gemini，Generator G2）
介面合約見 `AI_Builder_Cup_2026/harness/agent1-gemini/scenarios.md`。只加最小橋接，MSM 建庫行為不變。
- 用法：`build_db.py <raw_dir> <out_dir> [--extra-raw DIR ...]`；每個根目錄都是 `<machine>/dataset<N>/`。
- `plan_sessions()` 先走完全部根目錄、驗完才讀音檔與寫庫；任何錯誤 → `build_db: …`、exit 1、不產生任何庫。
- 場次資料夾有 `session.json`（正規化輸出）→ 機台短名＝`machine_short`、場域＝`domain`、冷卻＝`coolant`、工件／備註取自 session.json；沒有 session.json 的 MSM 資料夾照舊查 SHORT／DOMAIN／COOLANT 字典。`dataset_summary.xlsx` 改為可缺。
- session_id 仍是「短名＋SHA1(`<machine>/dataset<N>`) 前 6 碼」：MSM 雜湊輸入不變，正規化場次只換短名。
- `session_source` 新增 `root`（場次所屬根目錄絕對路徑）；main.sqlite、labels.sqlite schema 不變。

**DAT-001 MSM 建庫不變**：只給 MSM raw 時三庫逐列與改版前相同（source 排除 `root` 欄）。驗收：逐表 `SELECT * ORDER BY` 全欄 dump 後 diff。
**DAT-002 session.json 覆蓋字典**：sessions.machine＝`machine_short`，domain／coolant 取自 session.json。
**DAT-003 無 summary 也能建**：根目錄沒有 `dataset_summary.xlsx` 不報錯。
**DAT-004 多根目錄**：sessions 列數＝各根目錄場次和；`session_source.root` 指向各自根目錄（check_db **S13**：root＋dir 都是存在的資料夾）。
**DAT-005 數值欄空白**：label.csv 的 N／F／ap／ae／Z／D 空白 → NULL；Tool、direction、Workpiece 空白 → NULL（Tool 空白時 tool_defective 也是 NULL，不寫入 event_source）。
**DAT-006 短名合法**：`machine_short` 不合 `^[a-z0-9]+$` → 錯誤訊息指名該 session.json 與欄位；`domain`、`coolant` 不在合約值內同樣報錯。
**DAT-007 extra-raw 不存在**：建庫前報錯，不產生任何庫。
**DAT-008 重複**：session_id 或 machine/dataset 在根目錄間重複 → 報錯並列出兩個資料夾（驗證頁以 machine＋dataset 取場次，兩者都必須唯一）。
- 邊界：全部場次都沒有控制器檔時 context_ts 仍建同 14 欄的空表；全部刀具欄空白時 event_source 仍建表。

## 6. v3：未標註切削側檔與數值防呆（2026-10-16，harness agent1-gemini Sprint 3，Generator G4）
- 正規化場次資料夾可有 `<session_id>_unlabeled.csv`（`start,end`；只在有「切削中但標籤未定」區間時才有）。MSM 原始資料沒有此檔。
- **DAT-009 未標註切削**：Given 側檔有一列 When 建庫 Then 該區間為 `events.cut_state=cutting`、`labels.label_std=-1`、`label_native=NULL`，工況與刀具欄 NULL（不編造）；計入 `sessions.n_cut`；與 label.csv 列一起排序後才算非切削空隙，所以這段**不會**被當成非切削。MSM 無側檔 → 三庫逐列與改版前相同（DAT-001 重跑 7/7 identical）。
- **DAT-010 不可用數值**：label.csv 的 N／F／ap／ae／Z／D 為 `nan`、`inf`、`-inf`、`1e400` 或非數字（如 `abc`）→ NULL，不丟例外。
- **DAT-011 根目錄內的非場次檔**：`<machine>/` 下只有名稱恰為 `dataset<N>` 的**資料夾**算場次（正規化根目錄另有 `_manifest/dataset_index.csv`，不得被當成場次）。
- **DAT-012 session.json 格式錯**：不是合法 JSON 或不是 JSON 物件 → `build_db: <檔案路徑>: …`、exit 1、不產生任何庫。
- check_db：多根目錄的庫，S1／S6／S8／S9 的 MSM 期望值只對 MSM 場次算；正規化場次另驗「切削事件數＝label.csv 列數＋側檔列數」（S1）；S8 依 `session_source.root` 找原始檔（不再需要 `<db>/../raw`）；S10 正規化場次的短名取自其 session.json（修 `KeyError: 'ctwa_mill'`）。
- **S14 標籤值域**：切削事件 label_std ∈ {0,1,2,-1}；非切削全為 -1；label_std=-1 的切削事件數＝全部側檔列數，且不出現在 MSM 場次。

## 7. v4：切削狀態未知與側檔驗證（2026-10-16，harness agent1-gemini Sprint 3 收尾，Generator G5；E2-6／E2-1／E2-2）
- 側檔改為 `<session_id>_unlabeled.csv`（`start,end,cut_state`）；`cut_state` ∈ {`cutting`, `unknown`}，兩者標籤都是 -1。convert 對「標籤 -1 且 cut_state 為 cutting 或 unknown」的每個事件寫一列（含對映沒有規則的檔，以及 cut_state=unknown 卻給了 0／1／2 的規則——後者記 warning、以 unknown／-1 寫入）；`non_cutting` 照舊不寫，成為空隙。
- **DAT-013 切削狀態未知**：Given 側檔一列 `cut_state=unknown` When 建庫 Then `events.cut_state='unknown'`（照抄，不改成 cutting 或 non_cutting）、`labels.label_std=-1`、工況與刀具 NULL；這段**不**被當成非切削空隙；計入 `sessions.n_cut`（n_cut＝cutting＋unknown，MSM 只有 cutting，DAT-001 不變）。
- **DAT-014 側檔驗證**（`build_db.check_unlabeled`，在 `plan_sessions()` 內、讀音檔與寫庫之前；validate.py 同規則）：下列任一 → `build_db: <session_id> (<側檔路徑>) row <n>: …`、exit 1、不產生任何庫：
  - start／end 非數字、`nan`、`inf`（`start/end must be finite numbers`）；
  - start ≥ end；
  - end 超過 s0 音長＋0.05 秒；
  - cut_state 不是 cutting／unknown；
  - 與 label.csv 任一列重疊，或與同一側檔的另一列重疊。
- check_db：S1（正規化）改為「cutting 事件數＝label.csv 列數＋側檔 cutting 列數；unknown 事件數＝側檔 unknown 列數」（逐場）。
- **S14**：cutting 的 label_std ∈ {0,1,2,-1}；unknown 全為 -1；non_cutting 全為 -1；cut_state 只有這三種；label=-1 的 cutting 數＝側檔 cutting 列數、unknown 數＝側檔 unknown 列數，兩者都不出現在 MSM 場次。
