# AGENTS.md

> 給未來在這個專案工作的 AI agent（Claude、其他 LLM 助手）的注意事項。
> 重點是「**從過去的痛點學到的規則**」，而不是專案通論（後者在 [CLAUDE.md](CLAUDE.md) 和 [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)）。
>
> 想了解專案脈絡先讀 PROJECT_CONTEXT.md → 想知道參數設定讀 CLAUDE.md → 想動 code 之前讀本檔。

---

## 🚨 最重要：Shioaji session 不可強制中斷

永豐金 Shioaji 後台對同一組 `API_KEY` 只認**一個 exclusive session**。`fetch_contracts()` 等 API 會檢查這個鎖。

### Session 何時釋放

| 退出方式 | 後台 session 狀態 |
|---------|----------------|
| `api.logout()` 主動呼叫 | ✅ **立刻**釋放 |
| `Ctrl+C` 中斷（bot.py 有 signal handler 會跑 graceful shutdown） | ✅ 立刻釋放 |
| `TaskStop` / `kill -9` / 強制關 terminal | ❌ **後台不知道**，要等永豐金 **timeout 10-15 分鐘** |
| bot.py 異常 raise 但沒有 logout | ❌ 同上 |

### Don'ts（重要）

- ❌ **不要用 `TaskStop`/`kill -9` 砍** 還在跑的 bot.py（除非真的卡死沒救）
- ❌ **不要連續 trigger 雲端 workflow**：失敗的 session 還沒釋放，下一次又會撞
- ❌ **不要本機 + 雲端同時跑** bot.py（會搶 exclusive access）
- ❌ **不要連續啟動 bot.py 多次而沒間隔**（每次至少間隔 5-10 分鐘）

### Do's

- ✅ 想結束跑中的 bot.py → 按 `Ctrl+C`（會走 graceful shutdown）
- ✅ 在 Claude Code 環境（無 stdin 可按 Ctrl+C）測試 bot.py：用 `timeout 30 uv run python bot.py`，timeout 會送 SIGTERM 讓 bot.py 跑 signal handler
- ✅ 想測連線（不要長跑），用 `python main.py` 階段 1（自帶 `api.logout()`）
- ✅ bot.py 任何提早 raise 的路徑都要先 `try: self.api.logout() except: pass`
- ✅ 雲端 workflow 失敗後，**等 15 分鐘**再重 trigger

### 如果 session 已經卡住

通常徵兆：log 出現 `ShioajiTimeoutError: fetch_contracts: exclusive access lost (concurrent API call started)`，且本機確認沒任何 Python process 在跑。

唯一可靠的解法：**等永豐金後台自然 timeout（10-15 分鐘）**。沒有快速踢掉 session 的 API（連我們嘗試新的 login 都會被擋）。

---

## 🛠 修改商業邏輯前的安全檢查

> 修文件 / 註解隨便，但動 `bot.py` 內任何商業邏輯（買賣決策、預算、出場規則）前：

### 必跑

```bash
# 1. 語法檢查
uv run python -c "import ast; ast.parse(open('bot.py', encoding='utf-8').read()); print('syntax OK')"

# 2. 啟動煙霧測試（會用 1 個 Shioaji session，跑完一定要 graceful 結束）
timeout 30 uv run python bot.py
```

煙霧測試要看到的訊號：
- `[初始化] CA 憑證啟用成功`
- `[報價訂閱] BidAsk 訂閱完成：成功 68 檔，失敗 0 檔`
- `[XX:XX:XX +08:00] 非交易時間  部位：無`

任何 traceback 或 `❌` 訊息 → 修好再 commit。

### 跑前提醒

- 跑一次 bot.py = 用一個 Shioaji session ≈ 占用後台 5-10 分鐘
- **每天最多跑 3-5 次煙霧測試**，避免後端壓力 + session 衝突
- 連續測試之間**間隔 ≥ 5 分鐘**

---

## 📐 動 bot.py 的範圍規則

| 區段 | 規則 |
|------|------|
| 商業策略（進場/出場/排序） | ⚠️ 改動前先讓 Wayne 確認；改完後一定要跑回測 [batch_backtest.py](batch_backtest.py) 驗證 |
| 預算/部位計算 | ⚠️ 雙層保護（`budget_cap` + `strict_cap`）+ 第三層 `broker_buy_limit`（若可用）的設計不可破壞 |
| `PINNED_STOCKS` / `CANDIDATE_STOCKS` | ⚠️ **不要動 bot.py！** 改 [config/watchlist.yaml](config/watchlist.yaml) — 改之前用 batch_backtest.py 驗證 |
| `LONG_TERM_HOLD` | ⚠️ **不要動 bot.py！** 改 watchlist.yaml — 列在此處的股票不會被自動停損 |
| Shioaji API 呼叫（login/logout/place_order） | ⚠️ 任何 raise 路徑都要先 try logout |
| 文件 / 註解 / docstring | ✅ 自由改 |
| 環境變數讀取 / 設定驗證 | ✅ 自由改（保持向下相容） |
| Telegram 通知文字 / 格式 | ✅ 自由改 |

---

## ⚙️ 修改策略參數 / 監控清單的正確方式

不同類型的設定有不同的修改路徑。**走錯路徑會繞遠路或破壞東西**。

### A. 5 個常調策略參數（雲端 UI 改最快）

`MAX_POSITIONS`、`TOTAL_BUDGET`、`STOP_LOSS_PCT`、`MIN_ORDER_VALUE`、`SENTIMENT_ENABLED` 已透過 env vars 暴露。

| 環境 | 修改路徑 |
|------|---------|
| 雲端（推薦） | Repo Settings → Secrets and variables → Actions → **Variables** tab → 新增/編輯 |
| 本機 | `.env` 加同名 env var |

bot.py 內 `_env_int / _env_float / _env_bool` helpers 處理 fallback：env var 沒設 / 值不合法 → 印警告 → 用 [bot.py](bot.py) 內預設值。

**不要動 bot.py 內 `MAX_POSITIONS = _env_int(...)` 那行**。要改預設值才動程式碼。

### B. 其他策略參數（要動 bot.py）

`TRAILING_START`、`TRAILING_PULLBACK`、`TRAILING_ATR_MULT`、`BREAKEVEN_TRIGGER`、`TIME_STOP_BDAYS`、`SLIPPAGE_LIMIT`、`RSI_OVERBOUGHT`、`RVOL_MIN`、`VWAP_MAX_GAP`、`ATR_MAX_PCT`、`MA_TREND_PERIOD`、`MARKET_INDEX`、`SCAN_INTERVAL` 等。

→ 編輯 bot.py → commit → push → 隔天 cron 用新版

### C. 監控清單（任何修改都改 yaml）

`PINNED_STOCKS` / `CANDIDATE_STOCKS` / `LONG_TERM_HOLD` 三個清單**全部在 [config/watchlist.yaml](config/watchlist.yaml)**。

| 動作 | 怎麼做 |
|------|--------|
| 加股票 | 在對應 section 加 `- { code: "1234", name: "...", industry: "..." }` |
| 移除股票 | 刪 / 註解掉那行 |
| 重分類（PINNED ↔ CANDIDATE） | 在兩個 section 間搬動 |
| **雲端立即改** | 用 GitHub 網頁 UI 編輯 yaml → 直接 commit on web |

**不要動 bot.py 內 `_load_watchlist()` 函式**（除非要改 schema）。

### D. 雲端跑用什麼版本？

- **環境變數 / Variables**：即時生效（下次 cron 跑就用新值，**不用 commit**）
- **bot.py 程式碼**：必須 commit + push（雲端 checkout main 最新版）
- **watchlist.yaml**：必須 commit + push（檔案在 repo 內）

---

## 🌿 跟原作者 fork 的關係

- 原作者 repo：`yinyaoqing/AI_trade`（upstream remote）
- Wayne fork：`wjdesign/AI_trade`（origin remote）
- **不主動 sync** 原作者：commit message 品質差（大量「update」），雙方都改 bot.py，自動 merge 必衝突
- 想看上游改了什麼：`git fetch upstream && git log upstream/main --not origin/main --oneline`
- 想 cherry-pick：先確認原作者 commit 有明確意圖，不要 sync trial-and-error 類的「update」

### Wayne 已選擇保留的差異化修補（不要被上游覆蓋）

| 修補 | 位置 | 為什麼保留 |
|------|------|----------|
| `SIMULATION` env var + `CONFIRM_REAL_MONEY` 雙重防呆 | `bot.py:_parse_simulation_env` | 避免改原始碼切換正式 |
| `type(a).__name__ == "StockAccount"` 找預設帳戶 | `bot.py:__init__` | 純證券戶能跑（原寫死 `accounts[1]`） |
| UTF-8 stdout（emoji safe） | `bot.py` 頂部 | Windows cp950 / capture stdout 都不會掛 |
| `SENTIMENT_ENABLED=False` 時 `get_ai_sentiment` early return | `bot.py:get_ai_sentiment` | `OPENAI_API_KEY=dummy` 不會打 API 報 401 |
| 模擬模式隱藏 -100% PnL | `bot.py:calc_total_pnl` | 模擬戶 settlements 不支援，避免顯示誤導 |
| 監控清單「代號 名稱、...」單行格式 | `bot.py:format_watch_list` | 比 68 行縮排版本短 |
| `CST` → `+08:00 / (UTC+8)` | `bot.py:2558, 2582, 2673` | CST 有 4 種意思，國際讀者會誤解 |
| `PINNED_STOCKS` 68 檔（移除 7 檔嚴重虧損 + 加 3264） | `bot.py:PINNED_STOCKS` | 用 batch_backtest 驗證過 |
| `CANDIDATE_STOCKS` 13 檔候選池 | `bot.py:CANDIDATE_STOCKS` | 追蹤不交易；不在主迴圈引用 |
| 失敗時主動 logout | `bot.py` raise 前 | 避免後端 session 殘留卡 10-15 分鐘 |

上游可能有的東西可考慮 sync（要 Wayne 同意）：
- `_broker_buy_limit` 三層預算保護 ← **已 sync (commit `6754601`)**
- `0d140bd 更新合約下載失敗問題` ← 未 sync，看實作差異再決定

---

## 🔒 涉及金鑰 / 憑證的禁止事項

- ❌ **永遠不要**在對話、訊息、commit message、log 中**輸出任何金鑰**（API_KEY / SECRET_KEY / CA_PASSWORD / TELEGRAM_BOT_TOKEN / OPENAI_API_KEY）
- ❌ **不要** 用 `cat .env` 或類似命令印出 .env 內容
- ✅ 驗證 .env 設定用「遮罩」格式：`{val[:3]}***{val[-3:]} (len={len(val)})`
- ✅ `.pfx` 檔案永遠不可 commit（`.gitignore` 已排除 `*.pfx *.p12 *.pem *.key *.crt`）
- ✅ 雲端用 `CA_CERT_B64` GitHub Secret + workflow 啟動時 base64 解碼到 `/tmp/sinopac.pfx`

如果 Wayne 在對話中不小心貼出真金鑰：
1. 立刻提醒他這是嚴重洩漏
2. 引導他到永豐金後台**刪掉那把 Key**重建一把新的
3. 自己**絕對不寫到任何檔案**

---

## 📦 Git 流程

- `main` 是 default branch，預設**禁止直接 push**（規則保護）
- 任何 commit 走分支 → push → PR → merge 流程
- 例外：Wayne 自己授權直接 merge（如 `801d8eb` 那次）
- Commit message 寫得詳細：what + why + 影響範圍（前面幾個 commit `0033ced` `65b0a7b` `7a1aa67` 是好例子，可參考）
- **不主動 push**：commit 後等 Wayne 自己 push（避免不可逆操作）

---

## 🤖 Auto Mode 下的行為

Wayne 常開 Auto Mode 鼓勵 prefer action。但以下情境必須**停下來問**：

| 情境 | 為什麼 |
|------|------|
| 涉及金錢的決策（充值、跑正式交易） | 不可逆 + 直接花錢 |
| 改商業策略邏輯（出場 / 進場條件） | 影響真實交易結果 |
| 刪除 / 重置 git 資料（reset --hard、強制 push） | 可能丟失工作 |
| 連續觸發雲端 workflow | 會堆積 Shioaji session |
| 改 `SIMULATION` / `CONFIRM_REAL_MONEY` 相關防呆 | 可能繞過安全機制 |

可以直接做的事：
- 改文件、註解、style
- 加 try/except 包裝（不影響邏輯）
- 跑回測 / 跑單元測試
- 修明顯 bug（syntax error、import error）
- 跑 bot.py 煙霧測試（但要 graceful 結束）

---

## 📚 相關文件交叉引用

- [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — 專案全貌、Wayne 的角色、歷史決策
- [CLAUDE.md](CLAUDE.md) — Shioaji API 範例、參數設定、執行指令
- [HANDOFF.md](HANDOFF.md) — 之前的待辦事項與決策紀錄
- [AUDIT_REPORT.md](AUDIT_REPORT.md) — 第一次 audit 找到的 bug 與修正
- [GITHUB_ACTIONS_SETUP.md](GITHUB_ACTIONS_SETUP.md) — 雲端部署 SOP
- [README.md](README.md) — 給人類讀的專案說明
