# AI Trade — 台股 AI 模擬交易機器人

基於 [Shioaji（永豐金 API）](https://sinotrade.github.io/) 建立的自動交易系統，結合技術指標與法人籌碼分析，**預設於模擬環境**執行零股交易策略（可透過環境變數切換正式交易，雙重防呆）。每分鐘自動掃描 68 檔監控標的（PINNED_STOCKS），動態判斷進出場，並全程透過 Telegram 推播交易訊號與新聞摘要。

---

## 目錄

- [系統需求](#系統需求)
- [安裝步驟](#安裝步驟)
- [環境設定](#環境設定)
- [執行方式](#執行方式)
- [系統運作說明](#系統運作說明)
- [進出場規則](#進出場規則)
- [多策略框架](#多策略框架)
- [監控標的](#監控標的)
- [回測引擎](#回測引擎)
- [參數說明](#參數說明)
- [新聞來源說明](#新聞來源說明)
- [Telegram 通知設定](#telegram-通知設定)
- [切換正式交易](#切換正式交易)
- [注意事項](#注意事項)
- [專案結構](#專案結構)

---

## 系統需求

| 項目 | 需求 |
|------|------|
| Python | **3.12 以上** |
| 作業系統 | Windows / Linux / macOS |
| 帳戶 | 永豐金證券帳戶（含 API 申請） |
| 外部服務 | OpenAI API（選用）、Telegram Bot（選用） |

---

## 安裝步驟

### 1. 安裝 Python（若尚未安裝）

```powershell
winget install Python.Python.3.12
```

### 2. 安裝套件

```bash
# 使用 uv（推薦）
winget install astral-sh.uv
uv sync

# 或使用 pip
pip install shioaji python-dotenv pandas pandas-ta yfinance openai requests feedparser beautifulsoup4
```

---

## 環境設定

複製範本並填入您的金鑰：

```bash
copy .env.example .env
```

編輯 `.env`：

```env
# 永豐金 API（必要）
API_KEY=您的_API_Key
SECRET_KEY=您的_Secret_Key
CA_CERT_PATH=C:\path\to\your\cert.pfx
CA_PASSWORD=您的憑證密碼

# OpenAI（SENTIMENT_ENABLED=True 時必要，False 時可省略）
OPENAI_API_KEY=sk-...

# Telegram（選用，未填仍可執行）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321

# Proxy（選用，網路封鎖 Telegram 時設定）
# HTTPS_PROXY=http://127.0.0.1:7890
```

> **注意**：`TELEGRAM_BOT_TOKEN` 必須寫在同一行，中間不得有換行符號。

---

## 執行方式

### 模擬交易機器人（主程式）

```bash
uv run python bot.py
```

### 回測引擎（不需登入）

```bash
# 單標的 5 年回測（yfinance 免費資料）
uv run python backtest.py --code 2330 --start 2021-01-01 --yf

# 多標的比較
uv run python backtest.py --code 2330,2454,2317 --start 2021-01-01 --yf
```

### API 連線測試

```bash
uv run python main.py
```

---

## 系統運作說明

### 啟動流程

```
程式啟動
  ├─ 訂閱 68 檔股票的即時買賣報價（BidAsk），快取供滑點判斷
  ├─ 查詢帳戶餘額（模擬帳戶回傳 0 時沿用設定值 46,000 元）
  ├─ 同步已有持倉（重啟後不遺漏部位）
  └─ Telegram 推播：系統設定 + 目前持倉 + 啟動情緒分析 + 最新新聞
```

### 交易時間主循環（09:05–13:25，每 60 秒一輪）

```
每輪固定執行：
  │
  ├─ Step 1  出場監控（最優先，不受任何過濾影響）
  │            逐一檢查每個持倉 → 觸發止損/止盈立即賣出
  │            ※ 零股 T+1：當日買進的部位當日不賣
  │
  ├─ Step 2  大盤過濾（含 0.1% 遲滯帶，避免在 MA20 附近震盪時每分鐘翻轉）
  │            0050 現價 > MA20 × (1 + 0.1%) → 上升趨勢，繼續
  │            0050 現價 < MA20 × (1 - 0.1%) → 下跌趨勢，跳過本輪進場掃描
  │            介於 ±0.1% 之間            → 維持上一次的判斷
  │
  ├─ Step 3  情緒評分（可關閉）
  │            SENTIMENT_ENABLED=False（預設）→ 固定分數 1.0，直接進入下一步
  │            SENTIMENT_ENABLED=True → GPT-4o 分析新聞，< 0.6 分則跳過
  │
  ├─ Step 4  市場狀態判斷（雙引擎切換）
  │            0050 近 20 日年化波動率 < 18% → 趨勢市（動能 80% / 均值回歸 20%）
  │            0050 近 20 日年化波動率 ≥ 18% → 盤整市（動能 30% / 均值回歸 70%）
  │
  └─ Step 5  掃描 PINNED_STOCKS（68 檔）+ FunnelScanner 動態加入（每日 09:20）
               全部評估完 → 依綜合分排序 → 高分優先買進 → 最多 MAX_POSITIONS=4 個部位
```

### 非交易時間

```
每 30 分鐘推播今日最新新聞摘要至 Telegram
```

按 `Ctrl+C` 安全結束，自動登出並推播今日交易總結（成交紀錄 + 損益）。

---

## 進出場規則

### 進場條件（動能策略，以下全部須通過）

| 條件 | 說明 |
|------|------|
| 未持有此股 | 同一標的不重複進場 |
| 滑點 OK | 即時買賣價差 ≤ 1%（優先使用 BidAsk 訂閱，退回 snapshot） |
| 現價 > MA50 | 長期趨勢向上（回測驗證：加入後最大回撤從 -43% 降至 -19%） |
| ATR/股價 ≤ 3% | ATR 過熱保護，排除跳空缺口風險 |
| RSI < 70 | 避免追高；趨勢市中動態放寬至 RSI < 75 |
| 0 < VWAP 乖離 ≤ 3% | 日內強勢但未過熱追高 |
| RVOL ≥ 1.5 | 現量為 5 日均量 1.5 倍以上，確認量能放大 |
| 法人態度 ≥ -0.3 | 三大法人未大幅賣超（自動使用最新有效日資料） |
| 下單金額 ≥ 11,000 元 | 避免手續費侵蝕（最低手續費陷阱） |

> **法人資料時效**：台股三大法人資料每日約 14:30 公布。盤中（14:40 前）系統自動使用前一交易日資料；遇假日或非交易日最多往前回溯 5 天，確保不會因抓不到資料而判斷失準。

### 出場邏輯（四種條件，依優先順序）

```
每輪必跑，不受大盤/情緒/策略條件影響

A. ATR 止損（最高優先）
     止損價 = min(max(進場價 - 1.5×ATR, 進場價 × 97%), 進場價 × 97.5%)
     → ATR 過大時上限 -3%；ATR 過小時下限 -2.5%，避免假止損
     現價跌破 → 立即賣出

B. 成本保衛
     獲利達 2% 後，止損線自動上移至進場成本
     → 確保不從獲利轉為虧損

C. 動態移動止盈
     獲利達 1.5% 後啟動追蹤
     從歷史高點回吐 max(0.6×ATR, 1%) → 觸發賣出

D. 時間停損（TIME_STOP_BDAYS 工作天，波段釋放資金）
     持有滿 5 個工作天且未觸發 A/B/C，且歷史高點未曾突破移動止盈啟動價
     （entry × (1 + TRAILING_START = 1.5%）→ 強制出場、釋放資金
     ※ 不是日內當沖式的「30 分鐘沒動就跑」，而是波段風格的「一週沒漲透就換」
```

**零股 T+1 規定**：台灣盤中零股當日買進、當日不得賣出。系統已自動偵測並跳過當日新建部位的出場監控。

---

## 多策略框架

`StrategyAllocator` 根據 0050 近 20 日年化波動率自動判斷市場狀態，動態配置策略比重：

| 市場狀態 | 判斷條件 | 動能策略 | 均值回歸 |
|----------|----------|---------|---------:|
| TRENDING（趨勢市） | 年化波動率 < 18% | 80% | 20% |
| RANGING（盤整市） | 年化波動率 ≥ 18% | 30% | 70% |

> 📐 **閾值說明**：18% 是台股 0050 年化波動率的歷史中位數合理切點（歷史上多落在 15%~25% 區間）。`src/ai_trade/strategy.py` 中的 `_VOL_THRESHOLD` 控制此值。

- **動能策略**：VWAP 突破 + RSI 未超買 + 量能放大（RVOL ≥ 1.5）
- **均值回歸**：RSI < 30 + 現價 < VWAP（超賣後反彈）
- 兩種策略同時評估，同一標的取分數較高者
- 策略配置改變時才推播 Telegram，避免每分鐘重複通知

---

## 監控標的

`PINNED_STOCKS`（共 **68 檔**）= 12 檔原始回測驗證股 + 56 檔自選（含產業分類註解）+ 1 檔 2026-05-26 新增驗證通過。bot 每 60 秒掃描一輪，找符合進場條件的標的。

### 12 檔原始回測驗證股（yfinance 2021–2026，PF ≥ 1.1、夏普 ≥ 0.6）

| 代碼 | 名稱 | 獲利因子 | 夏普 | 備註 |
|------|------|---------|------|------|
| 2059 | 川湖 | 3.21 | 4.52 | |
| 1590 | 亞德客 | 3.15 | 6.83 | 0050 成分 |
| 2603 | 長榮 | 2.41 | 5.13 | 0050 成分 |
| 3324 | 雙鴻 | 1.69 | 3.60 | |
| 8210 | 上緯 | 1.87 | 3.63 | |
| 2609 | 陽明 | 1.58 | 2.55 | 0050 成分 |
| 2454 | 聯發科 | 1.53 | 2.73 | 0050 成分 |
| 3017 | 奇鋐 | 1.50 | 2.32 | |
| 2357 | 華碩 | 1.28 | 1.35 | 0050 成分 |
| 2330 | 台積電 | 1.33 | 1.93 | 0050 成分 |
| 8996 | 高力 | 1.20 | 1.14 | |
| 2379 | 瑞昱 | 1.13 | 0.63 | 0050 成分 |

0050 成分股流動性最佳，零股交易較容易成交。

### 56 檔自選清單（2026-05-26 已批次驗證）

集中在 AI / 半導體題材：IC 設計、晶圓代工、半導體封測、PCB、光通訊、散熱、AI 伺服器、自動化等。原 62 檔「圖片辨識」清單中，2026-05-26 用 [batch_backtest.py](batch_backtest.py) 重跑回測，移除 7 檔嚴重虧損股至 `CANDIDATE_STOCKS`：

| 移除股 | PF | Sharpe | 原因 |
|--------|----|--------|------|
| 2303 聯電 | 0.74 | -2.14 | 賠錢 |
| 6213 聯茂 | 0.53 | -3.91 | 賠錢 |
| 4991 環宇-KY | 0.24 | -10.75 | 賠錢 |
| 6147 頎邦 | 0.58 | -4.05 | 賠錢 |
| 2103 台橡 | 0.61 | -3.10 | 賠錢 |
| 2337 旺宏 | 0.61 | -2.72 | 賠錢 |
| 8046 南電 | 0.00 | -74.22 | 嚴重虧損 |

完整 56 檔含產業分類見 [bot.py:208-271](bot.py#L208-L271)。

### Wayne 自選新增（2026-05-26 通過驗證）

| 代碼 | 名稱 | 獲利因子 | 夏普 | 類型 |
|------|------|---------|------|------|
| 3264 | 欣銓 | 1.51 | 2.77 | 半導體封測測試 |

### CANDIDATE_STOCKS 候選池（13 檔，僅追蹤不交易）

存在於 `bot.py` 但**不會被主迴圈掃描下單**。內容為：6 檔 Wayne 自選但回測未通過 + 7 檔自 PINNED_STOCKS 移出的嚴重虧損股。未來可手動調整策略參數重驗，或徹底刪除。

### LONG_TERM_HOLD 長線持有清單

除了 `PINNED_STOCKS` 之外，[bot.py](bot.py) 還定義了 `LONG_TERM_HOLD`（位於 PINNED_STOCKS 之後），列在此清單的股票**不會被自動停損/停利監控**，由人工決定出場時機。適合基本面持股、核心持倉等不希望被短期波動觸發自動賣出的標的。

**目前清單**：

| 代碼 | 名稱 | 備註 |
|------|------|------|
| 0050 | 元大台灣50 | 長期核心持倉，包含台積電等優質藍籌 |

> 💡 出場監控迴圈讀到 `code in LONG_TERM_HOLD` 時會直接跳過該部位（見 [bot.py:1909](bot.py#L1909)），這檔股票必須手動下單賣出。

---

## 回測引擎

`backtest.py` 支援兩種資料來源：

| 模式 | 指令 | 資料年限 | 需登入 |
|------|------|---------|--------|
| **yfinance**（推薦） | `--yf` | 5+ 年 | 否 |
| Shioaji 模擬帳戶 | `--sim`（預設） | ~1 年 | 是 |

### 快速執行

```bash
uv run python backtest.py --code 2330 --start 2021-01-01 --yf
uv run python backtest.py --code 2330,2454,1590 --start 2021-01-01 --yf
```

### 回測參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `STOP_LOSS_PCT` | 3% | 固定止損（與 ATR 止損取嚴格者，與 bot.py 一致） |
| `ATR_MAX_PCT` | 3% | ATR 過熱保護上限 |
| `MA_TREND_PERIOD` | 50 | 趨勢過濾：股價需在 MA50 之上才進場 |
| `TRAILING_ATR_MULT` | 0.6 | 動態移動止盈回撤倍數 |
| `BREAKEVEN_TRIGGER` | 2% | 成本保衛啟動門檻 |
| `POSITION_SIZE` | 15,000 元 | 每筆預算（回測單檔用；批次回測用 [batch_backtest.py](batch_backtest.py) 跑 PINNED + CANDIDATE 全清單） |

---

## 參數說明

**主程式（bot.py）**

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `INITIAL_CAPITAL` | 68,000 元 | 原始投入資金（用於計算累計損益） |
| `TOTAL_BUDGET` | 46,000 元 | 總可用資金 |
| `MAX_POSITIONS` | 4 | 最多同時持有部位數（46,000 ÷ 11,000 = 4 與 _calc_position_size 動態退化結果一致） |
| `POSITION_SIZE` | 11,500 元 | 單次進場金額（自動計算：TOTAL_BUDGET ÷ MAX_POSITIONS） |
| `SENTIMENT_ENABLED` | `False` | 情緒評分開關：`False` → 跳過 AI 新聞分析（節省費用） |
| `STOP_LOSS_PCT` | 3% | 強制止損（與 1.5×ATR 取嚴格者） |
| `SLIPPAGE_LIMIT` | 1% | 最大允許買賣價差（零股市場較大） |
| `MIN_ORDER_VALUE` | 11,000 元 | 最小下單金額（避免手續費侵蝕） |
| `TRAILING_START` | 1.5% | 移動止盈啟動獲利點 |
| `TRAILING_PULLBACK` | 1.5% | 固定回吐觸發賣出（ATR 不足時的保底值） |
| `TRAILING_ATR_MULT` | 0.6 | 動態回撤倍數：從高點回落 0.6×ATR 觸發 |
| `BREAKEVEN_TRIGGER` | 2% | 成本保衛啟動獲利門檻 |
| `TIME_STOP_BDAYS` | 5 | 波段時間停損（工作天）：滿 5 天未漲透 → 釋放資金 |
| `ATR_MAX_PCT` | 3% | ATR 過熱保護（跳空缺口風險） |
| `MA_TREND_PERIOD` | 50 | 趨勢過濾均線 |
| `RVOL_MIN` | 1.5 | 相對成交量下限（現量 / 5 日均量） |
| `RSI_OVERBOUGHT` | 70 | RSI 超買門檻 |
| `RSI_OVERBOUGHT_LAX` | 75 | RSI 放寬門檻（趨勢市，RSI_DYNAMIC=True 時適用） |
| `RSI_DYNAMIC` | `True` | 動態 RSI：趨勢市中放寬超買門檻至 75 |
| `VWAP_MAX_GAP` | 3% | VWAP 乖離率上限，超過視為過熱不追 |
| `MARKET_INDEX` | `"0050"` | 大盤指數代碼 |
| `SCAN_INTERVAL` | 60 秒 | 主循環掃描間隔 |
| `NEWS_DIGEST_INTERVAL` | 1,800 秒 | 非交易時間推播間隔 |
| `FUNNEL_SCAN_HOUR/MINUTE` | 09:20 | 每日漏斗掃描觸發時間 |
| `FUNNEL_MAX_RESULTS` | 5 | 漏斗掃描每日最多加入監控的標的數 |

### 環境變數（影響執行模式）

| 環境變數 | 預設 | 說明 |
|---------|------|------|
| `SIMULATION` | `true` | 交易模式。`true` = 模擬不動真錢；`false` = **正式交易（需配合 CONFIRM_REAL_MONEY）** |
| `CONFIRM_REAL_MONEY` | （未設） | 正式交易雙重防呆字串，必須精確設為 `I_KNOW_THIS_IS_REAL_MONEY` 才會切換正式 |

任一缺漏或設錯都會 fallback 到模擬模式並印警告。詳見 [.env.example](.env.example) 與 [GITHUB_ACTIONS_SETUP.md](GITHUB_ACTIONS_SETUP.md)。

---

## 新聞來源說明

新聞由 `src/ai_trade/news.py` 的 `NewsAggregator` 自動聚合：

| 來源 | 類型 |
|------|------|
| 鉅亨網 | JSON API（全市場大盤情緒） |
| Yahoo 奇摩股市 | RSS（依股票代號） |
| Google News | RSS（依股票代號） |

---

## Telegram 通知設定

### 建立 Bot

1. 在 Telegram 搜尋 `@BotFather`
2. 傳送 `/newbot`，依指示命名
3. 取得 **Bot Token**（格式：`123456:ABC-DEF...`）

### 取得 Chat ID

1. 對 Bot 傳送任意訊息
2. 開啟瀏覽器前往 `https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates`
3. 從回傳 JSON 取得 `message.chat.id`

### Telegram 通知時機

| 事件 | 通知內容 |
|------|----------|
| 程式啟動 | 系統設定 + 目前持倉 + 啟動情緒分析 + 新聞摘要 |
| 策略配置**變更**時 | 市場狀態（TRENDING/RANGING）+ 波動率 + 策略比重 |
| 買進成交 | 股票代號、成交價、數量、VWAP、RSI、法人分、止損/止盈價 |
| 出場觸發 | 出場原因 + 損益百分比 + 淨損益（元） |
| 非交易時間 | 每 30 分鐘推播最新新聞摘要 |
| 程式結束 | 今日交易總結（成交紀錄 + 已實現損益） |

---

## 切換正式交易

> ⚠️ 正式交易會動用真實資金，請確認以下每一項再上線。

### 切換方式（透過環境變數，無需修改原始碼）

bot.py 透過 `_parse_simulation_env()` 讀取環境變數判斷模式，**必須同時設定下列兩個變數才會切換為正式交易**（任一缺漏自動 fallback 到模擬模式）：

| 環境變數 / Secret | 模擬模式（預設） | 正式交易 |
|------------------|----------------|---------|
| `SIMULATION` | `true`（或未設定） | `false` |
| `CONFIRM_REAL_MONEY` | （未設定） | `I_KNOW_THIS_IS_REAL_MONEY` |

> 🔧 **本機執行**：在 `.env` 加上這兩行（參考 [.env.example](.env.example)）
>
> ☁️ **GitHub Actions**：到 Repo Settings → Secrets and variables → Actions 新增上述 2 個 Secret。完整設定流程見 [GITHUB_ACTIONS_SETUP.md](GITHUB_ACTIONS_SETUP.md)。

> ⚠️ **不要直接改 `bot.py` 程式碼**。`self._simulation` 的值由 `_parse_simulation_env()` 動態決定（位於 [bot.py:450](bot.py#L450) 附近），手改會被環境變數覆蓋。

### 環境設定確認

| 項目 | 確認重點 |
|------|---------|
| `SECRET_KEY` | 啟動日誌顯示「base64解碼長度 33 bytes（需要 32）」，需重新從永豐金 API 管理頁複製正確金鑰 |
| `CA_CERT_PATH` | `.env` 中路徑須指向本機實際憑證檔案（非 `/tmp` 暫存路徑） |
| `CA_PASSWORD` | 確認憑證密碼正確，CA 啟用成功才能下單 |
| 帳戶可用資金 | 建議 ≥ `TOTAL_BUDGET`（46,000 元），正式模式下系統會自動讀取實際餘額 |

### 風險參數核對

| 參數 | 目前值 | 說明 |
|------|--------|------|
| `MAX_POSITIONS` | 4 | 最多同時持有 4 檔，單筆約 11,500 元 |
| `STOP_LOSS_PCT` | 3% | 每筆最大虧損上限（與 1.5×ATR 取嚴格者） |
| `MIN_ORDER_VALUE` | 11,000 元 | 低於此金額不進場（避免最低手續費侵蝕） |
| `TIME_STOP_BDAYS` | 5 | 波段時間停損（工作天）：滿 5 天未漲透則釋放資金 |
| `SIMULATION` | `false` | 切換正式必須設此值 |
| `CONFIRM_REAL_MONEY` | `I_KNOW_THIS_IS_REAL_MONEY` | 雙重防呆，缺漏會 fallback 到模擬 |

### 上線後首日建議

1. 觀察 Telegram 推播是否正常（買進/賣出通知）
2. 確認 GitHub Actions Artifacts 有正確備份 `logs/trades_YYYYMMDD.csv`
3. 首日建議手動暫時把 `MAX_POSITIONS=1` 試跑，確認整個下單流程無誤後再恢復 4 檔
4. 觀察 `_parse_simulation_env()` 的開頭 log，確認確實切換到「正式模式」

---

## 注意事項

- **模擬模式（預設）** — `SIMULATION` 環境變數未設或為 `true` 時，所有委託均為模擬，不會動用真實資金。切換正式交易須**同時**設 `SIMULATION=false` + `CONFIRM_REAL_MONEY=I_KNOW_THIS_IS_REAL_MONEY`，缺一不可
- **金鑰安全** — `.env` 已加入 `.gitignore`，請勿 commit 憑證或金鑰
- **時區** — 所有時間判斷均以台灣時間（UTC+8）為基準，可安全部署至任何伺服器
- **連線限制** — 同一帳號最多 5 條同時連線，避免短時間重複啟動
- **零股 T+1** — 台灣盤中零股當日買進不得當日賣出，系統已自動處理
- **流量限制** — 市場資料每 5 秒最多 50 筆請求；訂閱上限 200 檔（目前訂閱 68 檔 PINNED_STOCKS，遠低於上限）
- **OpenAI 費用** — 建議設定 API 用量上限；`SENTIMENT_ENABLED = False` 可完全停用 AI 分析
- **回測限制** — yfinance 提供日 K 資料，無法模擬盤中 VWAP 訊號；回測結果為策略方向性參考

---

## 專案結構

```
AI_trade/
├── bot.py                    # 主交易機器人
├── backtest.py               # 日K回測引擎（yfinance / Shioaji 雙模式）
├── minute_backtest.py        # 分鐘K回測引擎（Shioaji，需模擬帳戶）
├── main.py                   # 連線測試 & API 測試狀態查詢
├── src/ai_trade/
│   ├── __init__.py
│   ├── client.py             # ShioajiClient 封裝類別
│   ├── news.py               # 新聞聚合器（鉅亨網 / Yahoo / Google News）
│   ├── scanner.py            # 三層漏斗掃描器 — bot.py 每日 09:20 觸發一次，動態加入最多 5 檔當日強勢股至 watch_list
│   ├── strategy.py           # 多策略框架（StrategyAllocator）
│   └── chips.py              # 籌碼流向分析（智能日期回溯）
├── pyproject.toml            # 套件設定
├── .env                      # 金鑰設定（git-ignored）
└── .env.example              # 金鑰範本
```
