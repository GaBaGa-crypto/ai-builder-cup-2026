# BDD：Agent 1 第二階段：聲學證據提案（2026-10-17）

> 範圍：代理人加上四個確定性聲學工具、證據圖與提案檔；`scripts/propose_evidence.py`（線上，無頭跑代理人）、`scripts/apply_proposals.py`（離線，人核准的提案 → 對映）、`scripts/eval_agent_phase2.py`（離線，用預先登記的規則打分）。
> 不變式：代理人只寫提案檔（候選、證據、信心、問題），**不寫 label.csv、不改 N**；提案經人核准後，才由 `apply_proposals.py` 寫進**新的**對映檔，再走第一階段的 `normalize.py`。工具數字是證據，不是結論。
> 預先登記的規則與門檻（不由實作者訂）：`harness/agent1-phase2/tdd/TEST_PLAN.md`、`tdd/check_scenarios.py`（凍結）。

## 代理人的四步（INSTRUCTION v2，`agent/cutsound_normalizer/agent.py` 的 `=== EVIDENCE RUN` 段）

1. **讀工況與結構**：宣稱的 N、Z、D、材料、冷卻取自使用者訊息（`propose_evidence.py --mapping` 會把已核准對映的欄位規則逐資料夾算好放進訊息），沒有就從檔名當「宣稱」。資料夾分型：BASE（空轉）、FRESH、MODERATE、BROKEN、MSM 場次（已有 label.csv）。
2. **宣告檢查計畫**：每個資料夾一行：工具、檔案（抽樣）、理由。預設：BASE 用 `audio_profile`＋`class_separability`（對同轉速切削資料夾）確認沒有切削特徵；BROKEN 用 `detect_change_points`（序列前 6 檔）＋`estimate_spindle_speed`（前 2 檔與後段 2 檔）；FRESH／MODERATE 用 `class_separability`＋轉速一致性；MSM 場次只做一致性檢查（≤ 3 個 ≤ 60 秒窗的變化點對 label.csv 起訖、≤ 8 條路徑的轉速對 N），不提標籤候選。每資料夾最多 8 檔。
3. **執行**：長檔一律給 `t0_s`／`t1_s`（≤ 60 秒）；工具回 `error` 記下並繼續；回 `window_required` 就改選窗，不重送同一呼叫。
4. **寫提案檔**：`save_proposals` 驗 schema、擋越權鍵、把工具錯誤補進 `errors`，並逐筆比對證據數字是否真的出現在該工具的輸出（`evidence_check`）。

INSTRUCTION 內含使用限制（取自 `tdd/results.json` v2 與獨立評估），例如：變化點在有未標註間隙的長工業場次精確率不可靠（tmf 0.462，NOT SUPPORTED）；需要 ≥ 6 dB 持續階躍，平緩順銑（1–4 dB）找不到，沒有變化點不代表切削穩定；≥ 9,000 rpm 的轉速估計可能鎖到次同步軸承梳狀線（≈0.40×N），要回報 `harmonic_ambiguity` 與 `subsynchronous_of`；Z 未知時轉速不符只是假設。

## 場景

**E1（FEA-001）依資料夾類型選檢查**
- Given CTWA 8 個資料夾與已核准對映的宣稱轉速 When `propose_evidence.py <CTWA> --mapping agent/mappings/ctwa_reference.json --source-id ctwa` Then 提案檔的 BASE 條目引用的工具集合 ≠ BROKEN 條目的集合；BASE 含 `class_separability`；BROKEN 含 `detect_change_points` 與 `estimate_spindle_speed`；每條有理由與數字。
- 驗收：`eval_agent_phase2.py <提案>` 把檔案拆成 BASE／BROKEN 後原樣呼叫凍結的 `check_scenarios.py fea001`，exit 0。

**E2（FEA-002／GOLD）斷刀點候選**
- Given BROKEN520 前 6 檔 Then 有一筆 `change_point` 指向 `RPM520C0005`，時間在 [0.75, 1.80] 秒，附證據圖（`render_session_figure`）。
- 驗收：`eval_agent_phase2.py` 的 `[GOLD …]` 行；落在區間外也原樣回報，不改工具。

**E3（FEA-003）轉速疑問**
- Given BROKEN635 宣稱 635 rpm、Z 未知 Then C0001–C0012 至少一檔有 `rpm_mismatch`／`question`，證據引用 `estimate_spindle_speed`，含 ≥ 2 個候選、`harmonic_ambiguity`、`claimed_N: 635`，沒有 `candidate_label`，沒有新 N。
- 驗收：凍結的 `check_scenarios.py fea003 <提案>` exit 0。

**E4 MSM 只做一致性檢查**
- Given 有 label.csv 的 MSM 場次（例：imi_vmx30ui/dataset14） When `propose_evidence.py <場次資料夾>` Then 模式 `consistency_only`；任何條目都沒有 `candidate_label`；變化點證據的時間對得上 label.csv 起訖。
- 驗收：`eval_agent_phase2.py` 印出每個引用時間與最近邊界的 |Δt|（描述性，不設門檻）。
- 誠實回報見 E14：每個檢查過的邊界都要記，命中與漏掉都記。

**E5（FEA-005）核准後套用**
- Given 人寫的核准清單（`approved_by` 與要接受的 id；變化點可給 `msm_label`、`before_label`、起訖覆寫） When `apply_proposals.py <提案> --mapping <基礎對映> --out <新對映> --approvals <清單>` Then 新對映多出 `interval_labels`（變化點）或 `unresolved`（轉速疑問、可分性）；基礎對映不被改；N 與其他工況不變；接著 `normalize.py` 驗證 0 錯，該場次 label.csv 依核准區間切開，且仍只含 0／1／2。
- `approved_by` 必須是人：非空，且不含 "test"（不分大小寫），否則 exit 2、不寫檔。每筆核准區間的 confidence 是 `expert_rule`。
- `--approve-all` 只供測試，見 E11。

**E6（FEA-011）無金鑰**：`propose_evidence.py` 印 `NOT RUN`、exit 3，工作資料夾不建立任何檔。
**E7（FEA-012）單檔失敗**：工具回 `status: error` 時記入提案檔 `errors`（`save_proposals` 會從工具紀錄自動補上），其他檔照跑。
**E8（FEA-013）越權**：檢查範圍是**任何巢狀層級**，證據項目、`claimed_check` 底下的物件都算。以下任一項都算越權：
- 越權的鍵：`set_N`、`new_N`、`N_override`、`write_label_csv`、`label_csv`、`apply`、`overwrite`、`N`、`msm_label`、`convert_source`、`load_tables`、`save_mapping`。
- 名稱含 `label.csv` 的鍵。
- 值等於上列動作名的字串，例如 `{"action": "write_label_csv"}`。
- 寫入型鍵（`write_to`、`output`、`target`、`action` 等）的值指向 `label.csv`。

例外：`conditions` 與 `label_ref` 只是引述宣稱與既有列，裡面的 `N`、`msm_label` 不算越權。核准清單可以帶 `msm_label`。

發現越權時：
- `save_proposals` 回 `invalid` 並點名。
- `apply_proposals.py` 列出每一筆，點名到條目與路徑（例：`p024.evidence[0]: forbidden key 'convert_source'`），exit 4，不寫任何檔。
- 條目層與檔案層的 `convert_source` 也一樣 exit 4。舊版因 schema 先擋而 exit 2，證據內的則不會被擋。
**E9 禁用呼叫**：證據跑中代理人若呼叫 `convert_source`、`load_tables`、`save_mapping`，`propose_evidence.py` 判失敗（exit 1），不留任何檔。
- 證據跑的所有寫入都導向暫存資料夾，失敗時整個丟掉。這包括 `save_proposals`、`render_session_figure`，也包括被禁止的 `save_mapping`（`proposals.route_writes_to`）。
- 驗收：`harness/agent1-phase2/gh_work/test_staging.py` 直接呼叫代理人的 `save_mapping` FunctionTool，不經 LLM。結果檔案落在暫存資料夾，真正的 work 資料夾裡 0 個檔。對照組用 GA 的設定，同一呼叫會寫進 work。

**E10 證據可追溯（GH 收緊）**：每個證據項目都必須重現**同一次**工具呼叫，條件如下：
- 呼叫的檔案就是該條目的檔案。證據路徑指向別的檔或拼錯時，不會退回去比對其他呼叫。
- 呼叫窗與條目的時間窗重疊。
- 引用的參數與呼叫一致，例如 `claimed_N`／`claimed_rpm`、`flutes`、`t0_s`／`t1_s`。
- 每個數字落在該呼叫同名欄位的值上，容差 max(0.5 %, 0.01)，而且必須是忠實四捨五入：10431 可以代表 10431.131，10400 不行。
- 同一個變化點的 `t_s`、`dominant_band_index`、`max_abs_delta_db` 必須出自同一筆紀錄。

不符合的會列在 `evidence_check.unmatched` 並附原因，`save_proposals` 回警告。

驗收：`gh_work/test_matcher.py`。錯頻帶 3（真值 5）、12 dB（真值 12.13）、10400 rpm（真值 10431.131）、別檔的時間都被拒，精確值被接受。GA 兩次真實跑的證據重放後仍 43/43、15/15 通過（`gh_work/replay_matcher.py`）。

**E11 測試核准不得冒充人**：只給 `--approve-all` → exit 2 並說明原因。要搭配 `--unsafe-test-approval` 才會寫檔，寫出的檔：
- 每筆新區間的 confidence 都是 `test_only`；
- 對映檔與每筆條目都標 `approved_by "TEST (not a human)"`；
- `test_only` 不是對映 schema 的值，所以 normalize／convert 會拒絕這份對映，測試核准永遠成不了資料集標籤。

驗收：`gh_work/test_apply.py`。

**E12 摘要計數由程式算**：`save_proposals` 在 `summary` 前面加一行 `[computed by save_proposals: …]`，寫明各類條目數、一致性結果數與邊界計數，同時把這些數字寫進 `counts`。代理人的摘要寫錯數目時，例如寫「26 change points」但實際有 30 筆，會回 `boundary_warnings`。驗收：`test_matcher.py::test_summary_count_computed`。

**E13 GOLD 的意義**：GOLD 式抽查只證明工具有效，不算發現能力的測試。INSTRUCTION 已寫明這一點。

**E14 MSM 一致性要誠實**
- Given 有 label.csv 的場次 When 證據跑 Then 每個變化點窗內，距窗邊 ≥ 2 s 的所有 label.csv 起訖都要逐一記成 `consistency` 項目，命中與漏掉都記。命中是 0.5 s 內有點，記 `consistent`；沒有則記 `inconsistent`。
- 每個窗要有一筆 `boundary_windows`，記 n_checked、n_matched、n_missed、n_points_off_boundary。有任何漏掉時，結果不得為 `consistent`（schema 直接擋），摘要也不得說「all align」。
- `save_proposals` 會依工具輸出自行重算每窗計數，寫進 `boundary_audit`，和代理人的數字不同時回警告。
- 驗收：`test_matcher.py::BoundaryAuditTests` 重現 E-P2S2 的獨立計數（GA 三窗 41 / 28 / 13）。GH 的真實 MSM 跑：代理人記 27 / 18 / 9，與 audit 相同，454–493 s 窗標 `inconsistent`。

**E15 token 成本（不改行為）**
- 使用者訊息每個資料夾只給檔案數與 3 個例名（第一、第二、最後一個），不再列出全部檔名。
- 模型看到的聲學工具輸出拿掉 `params`（保留 `method`），候選只留前 5 個。紀錄層保留完整結果，證據比對不受影響。
- 四個聲學 FunctionTool 的宣告與原函式逐字相同。

## 回歸
- 第一階段 8 個工具與 `save_mapping` 流程不變（工具總數 14）；聲學工具包一層紀錄與一層 agent view 後，FunctionTool 宣告與原函式完全相同（`gh_work/decls_*.json`）。
- `eval_mapping.py` V6（第一階段 run2 提案）數字不變。
