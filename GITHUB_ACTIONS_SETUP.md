# GitHub Actions 雲端部署 SOP

> 本文件指引你把 AI_trade 部署到 GitHub Actions，達成「**不用本機開機，每週一到五自動執行**」的雲端模擬／正式交易。

---

## 前置檢查清單

在開始前，確認以下事項都已完成：

- [ ] 已 fork 專案到自己的 GitHub 帳號（你的位置：`https://github.com/wjdesign/AI_trade`）
- [ ] 已完成永豐金證券開戶
- [ ] 已申請 Shioaji API 權限並取得 `API_KEY` 和 `SECRET_KEY`
- [ ] 已下載永豐金憑證檔（`Sinopac.pfx`）並知道憑證密碼
- [ ] 已建立 Telegram Bot 並取得 `BOT_TOKEN` 和 `CHAT_ID`（選用）
- [ ] 已決定 repo 是 **公開** 還是 **私有**（會影響費用，見最後一節）
- [ ] **第一次啟動建議不設定 `SIMULATION` 與 `CONFIRM_REAL_MONEY` Secret**，讓 bot 安全預設為模擬模式
- [ ] 已知交易紀錄會被 Artifacts 自動備份保留 90 天（無需額外設定）

---

## Step 1：把 CA 憑證轉成 base64 字串

GitHub Secrets 不支援檔案上傳，必須把 `Sinopac.pfx` 轉成單行 base64 文字才能存。

### Windows（PowerShell）

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("D:\projects\wayne\AI_trade\Sinopac.pfx"))
```

執行後會印出**一大串**沒有換行的字元，全部複製下來（稍後貼到 GitHub Secret `CA_CERT_B64`）。

### macOS / Linux

```bash
base64 -i Sinopac.pfx | tr -d '\n'
```

### 驗證 base64 字串正確性

複製後可以先在本機解回原檔，比對檔案大小確認沒有複製錯：

```powershell
# Windows PowerShell：把字串貼到 $b64 變數
$b64 = "貼上你剛複製的 base64 字串"
[IO.File]::WriteAllBytes("test_restore.pfx", [Convert]::FromBase64String($b64))
# 原檔大小應該跟 Sinopac.pfx 一樣
```

---

## Step 2：在 GitHub 設定 7 個 Secrets

到你的 fork settings：

```
https://github.com/wjdesign/AI_trade/settings/secrets/actions
```

點擊 **「New repository secret」** 依序新增以下 7 個（前 4 個必要，後 3 個 Telegram 選用）：

### 必要 Secrets（缺一不可，否則 workflow 會失敗）

| Secret Name | 內容 | 範例格式 |
|------------|------|---------|
| `API_KEY` | 永豐金 Shioaji API Key | `eyJhbGciOiJIUzI1...`（長字串） |
| `SECRET_KEY` | 永豐金 Shioaji Secret Key | 32 bytes base64 字串 |
| `CA_CERT_B64` | Step 1 產生的 base64 字串 | `MIIKMQIBAzCCC...` |
| `CA_PASSWORD` | 永豐金憑證密碼 | 你開戶時設定的密碼 |

### 選用 Secrets（沒設不會壞，只是少功能）

| Secret Name | 內容 | 缺少時影響 |
|------------|------|----------|
| `OPENAI_API_KEY` | OpenAI API Key (sk-...) | 若 `SENTIMENT_ENABLED=False` 可不填，但 bot.py 第 225 行會 raise KeyError，**目前必填** |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | 不會推播交易訊號，但 bot 還是會跑 |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID（純數字） | 不會推播交易訊號，但 bot 還是會跑 |
| `SIMULATION` | 交易模式（`true` / `false`） | **不設定 = 預設模擬交易**（安全）；設 `false` 才會動用真實資金 |
| `CONFIRM_REAL_MONEY` | 正式交易雙重確認字串 | 不設定 = 無法切換正式交易；切正式時必設 `I_KNOW_THIS_IS_REAL_MONEY` |

> **重要**：`OPENAI_API_KEY` 目前在 bot.py 第 225 行用 `os.environ["OPENAI_API_KEY"]` 強制讀取，沒設會 crash。即使 `SENTIMENT_ENABLED=False`，你還是要設一個任意字串（例如 `dummy`）讓它能載入。

### 關於 `SIMULATION` 與 `CONFIRM_REAL_MONEY` 的雙重防呆設計

bot.py 啟動時用三層安全網判定要進入哪個模式：

```
第一層：SIMULATION 值
  ├─ 未設定 / true / 1 / yes / sim / simulation → 進入模擬模式 ✅
  ├─ 任何無法識別的值（亂碼、中文等）          → 進入模擬模式 ✅
  └─ false / 0 / no / real / production / prod  → 進入第二層

第二層：CONFIRM_REAL_MONEY 值
  ├─ 未設定 / 空字串                              → 印警告 + 進入模擬模式 ✅
  ├─ 不是 "I_KNOW_THIS_IS_REAL_MONEY"             → 印警告 + 進入模擬模式 ✅
  └─ 精確等於 "I_KNOW_THIS_IS_REAL_MONEY"         → 進入正式模式 🔴
```

這個設計的好處：
- **單一 Secret 失誤無法觸發正式交易**：你不會因為手滑把 SIMULATION 寫錯就用真錢
- **正式模式必須明確意圖**：必須複製貼上一段長字串，「不可能不小心輸入正確」
- **隨時可緊急停止**：刪掉 `CONFIRM_REAL_MONEY` Secret 就會立刻 fallback 回模擬

### Secrets 設定常見錯誤

1. **複製時夾帶換行**：`SECRET_KEY` 是長字串，複製時很容易在末尾多帶 `\n`。Workflow 第 36 行有驗證腳本會偵測這個問題，但設定時務必檢查。
2. **`CA_CERT_B64` 太長導致複製不完整**：建議用文字編輯器先檢查整段是否完整，再貼進 GitHub。
3. **`CA_PASSWORD` 含特殊字元**：如果密碼有 `$`、`!`、`'` 等字元，貼上時要原樣，**不要**自己加引號。

---

## Step 3：確認 workflow 已正確設定

檢查 `.github/workflows/trading_bot.yml` 沒有意外變動：

```yaml
on:
  schedule:
    - cron: '20 0 * * 1-5'    # 週一到五 00:20 UTC = 台北 08:20
  workflow_dispatch:           # 允許手動觸發
```

第 119 行的 base64 解碼：

```yaml
echo "${{ secrets.CA_CERT_B64 }}" | base64 -d > /tmp/sinopac.pfx
```

第 132 行執行 bot：

```yaml
run: python bot.py
```

如果這些都正常，往下一步。

---

## Step 4：手動觸發第一次測試

不要等到隔天早上才知道有沒有設對。先用「手動觸發」做一次 dry run：

1. 進入 `https://github.com/wjdesign/AI_trade/actions`
2. 左側選單點選 **「AI Trade Bot」** workflow
3. 右上角點擊 **「Run workflow」** 下拉
4. 在「手動觸發原因」填寫 `首次測試`
5. 點擊綠色 **「Run workflow」** 按鈕

### 預期看到的執行流程

```
✅ Checkout 程式碼           (~10 秒)
✅ 設定 Python 3.12           (~15 秒)
✅ 安裝套件                   (~3-9 分鐘，第一次最慢)
✅ 驗證 Secrets 格式           (~5 秒)
✅ 還原 CA 憑證                (~3 秒)
🟡 執行交易機器人              (持續執行，直到 timeout 或非交易時段結束)
```

第一次成功的話，「執行交易機器人」步驟會持續顯示「[心跳] 09:05:23 - 等待開盤」之類的 log。

如果是非交易時段（例如週末手動測試），bot 會啟動但只做新聞推播，不會下單。

### 第一次測試常見錯誤

| 錯誤訊息 | 原因 | 解決方法 |
|---------|------|---------|
| `KeyError: 'API_KEY'` | Secret 名稱拼錯 | 確認 GitHub Secrets 名稱大小寫完全符合 |
| `SECRET_KEY 含不可見字元` | 複製時夾帶空白/換行 | 重新從永豐金後台複製，貼到純文字編輯器確認後再貼上 |
| `Failed to decode CA cert` | base64 字串複製不完整 | 重新執行 Step 1 並完整複製 |
| `CA activation failed` | 憑證密碼錯誤 | 確認 `CA_PASSWORD` 是憑證密碼，不是登入密碼 |
| `連線數已達上限` | 同一帳號已有 5 條連線 | 等 5-10 分鐘讓舊連線釋放，或關閉本機正在跑的 bot |

---

## 交易紀錄持久化（Artifacts 自動備份）

GitHub Actions 每次執行都是全新的 Ubuntu VM，跑完就銷毀。如果不做特別處理，bot.py 寫到 `logs/trades_YYYYMMDD.csv` 的紀錄會跟著 VM 一起消失。

本 workflow 已內建**自動備份還原機制**，你不需要做任何設定就會自動運作：

### 運作流程

```
每次 workflow run：

[啟動]
  ├─ 還原 CA 憑證
  ├─ 還原歷史交易紀錄 ← 從前次 run 的 artifacts 下載 logs/ 到本地
  └─ 啟動 bot.py（可讀到歷史 CSV、繼續累計）

[執行中]
  └─ bot.py 寫入 logs/trades_YYYYMMDD.csv

[結束時，always 觸發，包含異常終止]
  ├─ 備份本次交易紀錄 → 上傳整個 logs/ 和 shioaji.log 成 artifact
  └─ 清除憑證
```

### 你可以在哪裡看到備份的紀錄

1. 進入 `https://github.com/wjdesign/AI_trade/actions`
2. 點擊任何一次 workflow run
3. 頁面最下方有 **「Artifacts」** 區塊，會看到 `trade-logs-{run_id}` 可下載

### 設計細節

- **保留期限**：90 天（GitHub 免費版上限）
- **大小限制**：每月 500MB 免費（你的 CSV 加起來遠低於此）
- **失敗時**：`if-no-files-found: warn` 不會讓 workflow 失敗
- **第一次跑**：沒有歷史 artifact 是正常的，`continue-on-error: true` 不會卡住

### 進階：手動下載查看所有歷史

```bash
# 用 GitHub CLI 一次下載所有歷史交易紀錄
gh run list --workflow trading_bot.yml --limit 30 --json databaseId -q '.[].databaseId' | \
  xargs -I {} gh run download {} --pattern 'trade-logs-*'
```

### 不適合 Artifacts 的東西

artifacts 不適合用來儲存：
- **持倉狀態** — bot.py 啟動時會從 Shioaji API 同步當前持倉，不需要從 artifact 還原
- **策略決策歷史** — 都已經透過 Telegram 推播了，Telegram 對話紀錄就是另一份備份
- **長期資料庫** — 90 天會過期，要長期儲存建議外接 Supabase 或 SQLite + Git LFS

---

## Step 5：監控與日誌

### 即時查看 log

GitHub Actions 頁面 → 點擊執行中的 workflow run → 點擊「執行交易機器人」步驟 → 右側即時 log

### 下載完整 log（執行結束後）

每次 workflow 結束後，可在頁面右上角「**...**」選單下載完整 log 壓縮檔（保留 90 天）。

### 重要警告：log 中的敏感資訊

bot.py 啟動時會印出 `API_KEY` 的長度（不是內容）和 `SECRET_KEY` 的遮蔽版本，這些寫在 public log 不會洩漏金鑰本身，但**如果你的 repo 是公開的，所有 workflow log 也是公開的**。建議：

- 把 repo 設為 **私有**（雖然會被收費，見最後一節）
- 或者把任何明顯的個資（例如 Telegram chat id）視為已公開資訊

---

## Step 6：日常運作

### 排程觸發時間

| 階段 | 時間（台北） | 說明 |
|------|------------|------|
| Cron 觸發 | 08:20 | GitHub Actions 設定的排程時間 |
| 實際啟動 | 08:20–09:00 | Cron 通常延遲 5-40 分鐘，所以提前 45 分鐘觸發 |
| 完成初始化 | +5-9 分鐘 | pip install + Shioaji 登入 + 訂閱 |
| 開始交易 | 09:05 | 主循環啟動 |
| 收盤後總結 | 13:30 | 推播當日交易摘要 |
| Workflow 結束 | 14:00 左右 | 安全結束、自動 logout |

### 暫停或停用

進入 Actions 頁面 → 選擇 workflow → 右上角「**...**」→ **「Disable workflow」** 即可。

要恢復就再點一次 **「Enable workflow」**。

### 緊急停止當前執行

在執行中的 workflow run 頁面，點擊右上角的 **「Cancel workflow」** 按鈕。bot.py 收到 SIGTERM 後會嘗試 graceful shutdown（登出 Shioaji + 推播總結）。

---

## ⚠️ 費用提醒（重要）

### GitHub Actions 計費規則

| Repo 類型 | 個人帳號免費額度 | 超出後計費 |
|----------|--------------|-----------|
| **公開 repo** | **無限制** | 不收費 |
| **私有 repo** | 2,000 分鐘/月 | $0.008/分鐘（Linux） |

### 你的預期用量

```
每天執行時間：約 5.7 小時 = 342 分鐘
每月交易日：約 22 天
每月總用量：342 × 22 ≈ 7,524 分鐘
```

**結論**：
- 公開 repo → **免費**
- 私有 repo → 第一個月就超量 5,524 分鐘 ≈ **$44 美元/月**

### 建議

如果你不希望公開程式碼：
1. **省錢方案**：把 repo 設為公開，但 **Secrets 永遠是加密的**，不會被洩漏。任何人能看你的程式碼，但看不到你的 API key、憑證、密碼
2. **完全隱私方案**：用 GitHub Actions Self-hosted Runner，在自己的伺服器或樹莓派上跑（這個 workflow 不需要改，只需要新增 runner 標籤）

如果你在意「策略邏輯不想公開」，可以把策略參數（如 `STOP_LOSS_PCT`、`MAX_POSITIONS`）也放進 Secrets，讓公開的程式碼只包含框架。

---

## 進階：本機與雲端共存的注意事項

如果你同時在本機開發測試、又開啟雲端 workflow，要避免：

1. **同帳號多連線**：Shioaji 限制單一帳號最多 5 條連線。本機跑的時候建議先 disable workflow，避免撞上限。
2. **重複下單**：兩邊都在跑 bot 會對同一檔股票各自評估、各自下單，可能造成意外多倉。
3. **本機修改未推上去**：你在本機改了參數但沒 push，雲端跑的還是舊版。建議養成 `git push` 後再啟動 workflow 的習慣。

---

## 切換正式交易檢查清單

當你確認模擬交易跑得穩定，要切換到正式交易時，**完全不需要改原始碼**，只需修改一個 Secret：

### 切換步驟

- [ ] 確認模擬交易已穩定運行至少 2 週，無重大異常
- [ ] 確認永豐金帳戶可用資金 ≥ `TOTAL_BUDGET`（46,000 元）
- [ ] 第一天降低風險：本機 commit 一個版本把 `MAX_POSITIONS` 改為 1（只試一檔），push 上去
- [ ] 進入 `https://github.com/wjdesign/AI_trade/settings/secrets/actions`
- [ ] **新增 Secret 1：名稱 `SIMULATION`，值 `false`**（沒有引號）
- [ ] **新增 Secret 2：名稱 `CONFIRM_REAL_MONEY`，值 `I_KNOW_THIS_IS_REAL_MONEY`**（精確匹配，區分大小寫）
- [ ] **手動觸發一次 workflow 驗證沒問題**，不要等隔天才發現失敗
- [ ] 在 workflow log 開頭確認看到：
      ```
      [初始化] ============================================================
      [初始化] 🔴 交易模式：正式交易（simulation=False，動用真實資金！）
      [初始化] ============================================================
      ```
- [ ] 如果你看到的是「⚠️ [防呆機制觸發] ... fallback 到模擬交易」，代表 `CONFIRM_REAL_MONEY` 設錯或沒設，**請去 Secrets 確認字串完全正確**
- [ ] 在 Telegram 上盯著第一次的買進通知和成交回報
- [ ] 第一週密切觀察成交紀錄、Telegram 通知、losses 是否在預期範圍

### 緊急切回模擬模式

如果發現策略行為有問題、想立刻停止真實下單：

**最快做法（10 秒）** — 利用防呆機制：
1. 進入 Secrets 頁面，**刪除 `CONFIRM_REAL_MONEY` 這個 Secret**
2. 進入 Actions 頁面，取消正在執行中的 workflow run
3. 下次 workflow 啟動時防呆機制會觸發 → 自動 fallback 到模擬模式

> 這個做法的好處：你**不需要記得回去把 `SIMULATION` 改回 true**。只要 `CONFIRM_REAL_MONEY` 缺一個，就絕對不會進入正式模式。

**完全停用做法（5 秒）**：
1. 進入 Actions 頁面 → 選擇 workflow → 右上角「**...**」→ **「Disable workflow」**

兩者選一，**都不需要 commit / push 任何程式碼變更**。

---

## 故障排除快速索引

| 症狀 | 對應章節 |
|------|---------|
| 9:05 沒看到 bot 啟動 | Cron 延遲，看 Step 6「實際啟動 08:20–09:00」 |
| Workflow 一直失敗在「驗證 Secrets」 | 看 Step 4「常見錯誤」 |
| Telegram 沒收到通知 | 確認 `TELEGRAM_BOT_TOKEN` 和 `CHAT_ID` 都正確；先用 curl 測試 Bot |
| 連線數已達上限 | 等 5-10 分鐘或檢查本機是否也有跑 |
| 收費爆炸 | 改成公開 repo 或用 self-hosted runner |
| 收盤後沒看到當日總結 | Workflow 可能在收盤前被 timeout 切斷，看 Step 4 預期流程 |

---

## 附錄：本次審查發現的部署前必修問題

詳見同目錄下 [`AUDIT_REPORT.md`](./AUDIT_REPORT.md)，已修正：

1. ✅ `bot.py:448` — 預設改回 `simulation=True`（避免第一次跑就動真錢）
2. ✅ `strategy.py:32` — 波動率閾值從測試值 `0.015` 改為正式值 `0.18`
3. ✅ **新增 `SIMULATION` 環境變數機制** — 切換正式/模擬交易不再需要改原始碼，改 Secret 即可
4. ✅ **新增 `CONFIRM_REAL_MONEY` 雙重防呆** — 單一 Secret 失誤無法觸發正式交易（必須同時兩個 Secret 都對才會用真錢）
5. ✅ **新增 Artifacts 自動備份還原** — 交易紀錄持久化保留 90 天，跨 workflow run 自動串接

未修正、需你決策：

- README 描述的 `TIME_STOP_MINUTES=30` vs 程式碼的 `TIME_STOP_BDAYS=5` 是根本性的策略差異，需確認你想用哪一個
- 監控股票清單有 60+ 檔未經回測，建議瘦身
- 詳細清單見 `AUDIT_REPORT.md`「建議的後續處理」
