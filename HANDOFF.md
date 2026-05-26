# AI_trade 待辦事項交接文件

> 這份文件給接手的 Claude（或開發者）使用。把 Cowork 對話的脈絡完整封裝下來，照這份做下去即可無縫接續。

---

## 專案位置與環境

- **本機路徑**：`D:\projects\wayne\AI_trade`
- **GitHub Repo**：`https://github.com/wjdesign/AI_trade`（公開 fork，原作者：`yinyaoqing`）
- **遠端設定**：`origin` 指向 `git@github.com:wjdesign/AI_trade.git`
- **目前分支**：`main`
- **環境**：Python 3.12，使用 `uv` 套件管理
- **券商 API**：永豐金 Shioaji（Wayne 尚未完成開戶與 API 申請）
- **部署目標**：GitHub Actions（公開 repo，免費執行）

---

## 已完成的工作（不要重做）

### 1. Code Audit + 緊急 Bug 修正
詳見 `D:\projects\wayne\AI_trade\AUDIT_REPORT.md`。

**已修正的兩個關鍵 bug：**
- ✅ `bot.py:450` — `self._simulation = False` → `True`（避免第一次跑就動真錢）
- ✅ `src/ai_trade/strategy.py:32` — `_VOL_THRESHOLD = 0.015` → `0.18`（修復策略永遠判定為盤整市）

### 2. SIMULATION 環境變數機制
- `bot.py` 新增 `_parse_simulation_env()` 函式（lines 63-78 區間）
- `.env.example` 加上 `SIMULATION=true` 範例與說明
- `.github/workflows/trading_bot.yml` 加上 `SIMULATION` 環境變數傳遞

### 3. CONFIRM_REAL_MONEY 雙重防呆機制
- bot.py 的 `_parse_simulation_env()` 已重寫為兩層防呆
- 必須**同時**滿足 `SIMULATION=false` 和 `CONFIRM_REAL_MONEY=I_KNOW_THIS_IS_REAL_MONEY` 才會走正式
- 任一缺漏 → 印警告 + fallback 到模擬
- 已通過 17 項邊界測試（含大小寫、空白、亂碼、中文等）

### 4. Artifacts 自動備份還原
- workflow 加上 `actions/upload-artifact@v4`：結束時備份 `logs/` 和 `shioaji.log`，保留 90 天
- workflow 加上 `actions/download-artifact@v4`：啟動時還原前次紀錄
- 跨 workflow run 自動串接，無需額外設定

### 5. 部署文件
詳見 `D:\projects\wayne\AI_trade\GITHUB_ACTIONS_SETUP.md`，包含：
- 7 個 Secrets 設定清單（含 `SIMULATION`、`CONFIRM_REAL_MONEY`）
- CA 憑證 base64 轉換指令（Windows / Mac / Linux）
- 切換正式交易檢查清單
- Artifacts 章節
- 緊急切回模擬模式 SOP

### 已修改的檔案（共 5 個）

```
bot.py                            ← _parse_simulation_env + __init__ 模式判定
src/ai_trade/strategy.py          ← _VOL_THRESHOLD 修正
.env.example                      ← 新增 SIMULATION 與 CONFIRM_REAL_MONEY
.github/workflows/trading_bot.yml ← SIMULATION + CONFIRM_REAL_MONEY + Artifacts
GITHUB_ACTIONS_SETUP.md           ← (新檔案)
AUDIT_REPORT.md                   ← (新檔案)
```

> ⚠️ **這些變更目前都還沒 commit**，請在動下面待辦之前先 commit 一個 baseline，方便回滾。

---

## 待 Wayne 決定的 3 件策略決策

**請務必先讓 Wayne 在以下三題各選一個答案，再開始動程式碼。**

### 決策 1：TIME_STOP 機制方向

**背景**：README 描述「30 分鐘日內動能停損」，但實際程式碼是「5 工作天波段停損」。兩者根本性不同。

| 選項 | 說明 |
|------|------|
| **A. 保留現狀 5 工作天波段版**（Cowork 推薦） | 改 README 描述匹配程式碼，適合 Wayne 目前的低頻交易心態 |
| B. 改回 30 分鐘日內版 | 重寫 bot.py 出場邏輯為被動式當沖風格，週轉快但手續費高 |
| C. 兩個都來 | 同時保留 5 工作天作為未動式出場保障，再加 30 分鐘入場手動動能檢查（代碼量最大） |

### 決策 2：PINNED_STOCKS 60+ 檔未驗證股處理

**背景**：`bot.py:134-212` 的 `PINNED_STOCKS` 從 README 描述的 12 檔回測驗證版本，膨脹到 70+ 檔（含註解明說「待回測驗證」的 60+ 檔）。

| 選項 | 說明 |
|------|------|
| **A. 只保留原始 12 檔回測驗證過的**（Cowork 推薦） | 刪掉 60+ 未驗證股，保留 PF≥1.1、Sharpe≥0.6 的 12 檔，統計期望可控 |
| B. 全部保留但抽到另一個「候選池」 | 12 檔繼續被 bot 掃，60+ 檔移到 `CANDIDATE_STOCKS` 不被掃描，保留讓 Wayne 手動期般檢視使用 |
| C. 全部保留並並交給 bot 掃描 | 保現狀不動，README 加強說明「這 60 檔未驗證、進場風險自負」 |

### 決策 3：MAX_POSITIONS 處理

**背景**：`MAX_POSITIONS=7` 但因 `MIN_ORDER_VALUE=11000` 退化為實際 4 部位。設定值名存實亡。

| 選項 | 說明 |
|------|------|
| **A. 改為 4，設定與實際一致**（Cowork 推薦） | 既然被退化為 4，不如直接設為 4，同時讓 `_calc_position_size` 變簡單 |
| B. 保留 7，只在 README 說明動態退化機制 | 保留「未來加預算時可以自動變 7 部位」的彈性，但在文件說明目前只能 4 部位 |
| C. 改為 4，並下調 MIN_ORDER_VALUE 回 9000 | 重設進「7 部位 × 6,571 元」原始設定，但要接受手續費佔比提高 |

---

## 11 項待辦清單（依優先順序）

### 立即處理（上線前必做）

#### 任務 1：全面更新 README
**檔案**：`README.md`
**內容**：把實際參數和機制全部反映到 README。具體要改：
- 參數對照表更新（`TOTAL_BUDGET=46000`、`MAX_POSITIONS=` [依決策 3]、`MIN_ORDER_VALUE=11000`、`POSITION_SIZE=11500`、`INITIAL_CAPITAL=68000`）
- 第 211-217 行的「市場狀態」表格：把 `< 1.5%` 改為 `< 18%`，`≥ 1.5%` 改為 `≥ 18%`
- 第 200-203 行的「TIME_STOP_MINUTES」描述：依決策 1 改寫
- 第 227 行的「共 12 檔」描述：依決策 2 更新
- 第 354 行「切換正式交易」改為「設定 GitHub Secret」流程
- 第 354 行的「`bot.py` | 339」修正為「`bot.py` | 450」（且改為描述變數修改）
- 補上 `LONG_TERM_HOLD` 機制說明（`bot.py:216-223`）
- 補上漏斗掃描器（FunnelScanner）說明 — 它每天 9:20 跑、加最多 5 檔
- 補上 MA20 大盤過濾的 0.1% 遲滯帶說明（`bot.py:1370-1374`）
- 補上 `SIMULATION` 和 `CONFIRM_REAL_MONEY` 環境變數使用說明
- 第 414 行「scanner.py（備用，bot.py 已不使用）」改為實際說明

#### 任務 2：TIME_STOP 機制方向（依決策 1）
**檔案**：`bot.py`、`README.md`
- 若選 A：不動程式碼，只改 README
- 若選 B：重寫 `bot.py` 出場邏輯（lines 1893-1906 + 相關支援碼），加回 `TIME_STOP_MINUTES = 30` 和 `TIME_STOP_BAND = 0.005`，改用分鐘計算而非工作天
- 若選 C：保留現有 5 工作天邏輯，並另加新的 30 分鐘入場手動動能檢查邏輯

#### 任務 3：README 第 354 行行號錯誤
**檔案**：`README.md`
- 把「`bot.py` | 339」改為「`bot.py` | 450」
- 把整行替換語法改為「設定 GitHub Secret `SIMULATION=false` + `CONFIRM_REAL_MONEY=I_KNOW_THIS_IS_REAL_MONEY`」
- 引用 `GITHUB_ACTIONS_SETUP.md` 的切換正式交易章節

### 短期處理（上線後 1 個月內）

#### 任務 4：PINNED_STOCKS 瘦身（依決策 2）
**檔案**：`bot.py:134-212`
- 若選 A：刪掉「圖片辨識自選清單」60 檔，只留前 12 檔
- 若選 B：新增 `CANDIDATE_STOCKS` 變數，把 60 檔移過去，但不放進 `bot.scan` 流程
- 若選 C：保現狀，僅補強註解

#### 任務 5：修正 STOP_LOSS_PCT 註解
**檔案**：`bot.py:53`
```python
STOP_LOSS_PCT = 0.03   # 強制止損：虧損 3%（回測驗證：2% 橫盤假止損過多，3% 最大回撤控制較優，折衷取 2.5%）
```
- 移除「折衷取 2.5%」這段（值就是 3%，前後矛盾）
- 改為單純說明「3% 為回測驗證後的最佳止損點」

#### 任務 6：MAX_POSITIONS 與 _calc_position_size 對齊（依決策 3）
**檔案**：`bot.py:49, 76-96`
- 若選 A：`MAX_POSITIONS = 4`，並簡化 `_calc_position_size`（不再需要動態退化邏輯）
- 若選 B：保現狀，在 README 說明
- 若選 C：`MAX_POSITIONS = 4` 且 `MIN_ORDER_VALUE = 9000`，但要重新估算手續費影響

#### 任務 7：修正 README 第 414 行 scanner.py 說明
**檔案**：`README.md:414`
- 從「scanner.py（備用，bot.py 已不使用）」改為說明它是漏斗掃描器
- 補上實際運作：每天 09:20 觸發一次、結果合併到當日 watch_list、最多加 5 檔

#### 任務 8：README 補上 LONG_TERM_HOLD 機制
**檔案**：`README.md`
- 在「監控標的」段落後新增一段
- 說明 `bot.py:216-223` 的 `LONG_TERM_HOLD` 機制
- 說明列在此清單的股票不會被自動停損/停利監控
- 目前清單：`0050` 元大台灣 50

#### 任務 9：修正 bot.py 第 5 行 docstring 過時
**檔案**：`bot.py:1-7` 的模組級 docstring
- 「大盤月線過濾」改為「大盤 MA20 過濾」
- 「2% 強制止損」改為「3% 強制止損」
- 補上「Artifacts 自動備份」「雙重確認防呆」等近期新增機制

### 中期處理（穩定運行 3 個月後）

#### 任務 10：監控股票清單抽到外部 YAML
**新檔案**：`config/watchlist.yaml`
**重構檔案**：`bot.py:134-212`
- 建立 `config/watchlist.yaml` 用 YAML 格式描述：
  ```yaml
  pinned:
    - code: "2330"
      name: "台積電"
      pf: 1.33
      sharpe: 1.93
      notes: "0050 成分"
  long_term_hold:
    - code: "0050"
      name: "元大台灣50"
  candidates:  # 若決策 2 選 B
    - code: "6664"
      name: "群翊"
  ```
- 在 `bot.py` 啟動時用 `yaml.safe_load()` 讀取
- 移除程式碼中 70 行的清單

#### 任務 11：策略參數抽到 config.py 或 pyproject.toml
**新檔案**：`src/ai_trade/config.py`
**重構檔案**：`bot.py:43-128`
- 把所有 `XXX_PCT`、`XXX_THRESHOLD`、`SCAN_INTERVAL`、`FUNNEL_*` 等參數搬到 `src/ai_trade/config.py`
- `bot.py` 改為 `from src.ai_trade.config import *`
- 目標：把 `bot.py` 從 121KB 降到合理大小，易於維護

---

## 建議的執行順序

1. **先 commit 一個 baseline**：把目前已修正的內容 commit 起來，方便回滾
   ```bash
   git add bot.py src/ai_trade/strategy.py .env.example .github/workflows/trading_bot.yml AUDIT_REPORT.md GITHUB_ACTIONS_SETUP.md HANDOFF.md
   git commit -m "Add audit fixes, env-var simulation switch, dual confirmation, artifacts backup"
   git push
   ```

2. **問 Wayne 三個策略決策**（決策 1、2、3）

3. **依決定的方向動工**：建議用 TaskCreate 把這 11 個任務全部列上，逐項完成 + 提交

4. **每完成 2-3 個任務就 commit + push 一次**，並請 Wayne 在 GitHub 上 review，避免一次大爆炸難以審閱

5. **最後一步**：所有改動完成後，請 Wayne 手動觸發一次 workflow（模擬模式）做端到端驗證

---

## 待辦執行小提醒

- bot.py 是個 121KB 的大檔，動它時要小心，建議用 Read 局部 + Edit 局部，避免大規模重寫
- README.md 約 420 行，建議分區段更新，每改一段檢查一段
- 切忌一口氣做完所有任務再 commit，會難以 review 且難以回滾
- 任何改動都應該保留「商業邏輯不變」原則，只動文件 / 註解 / 抽離 config / 修明顯 bug，不要私自改動策略行為
- 若遇到不確定的策略行為改動，**先停下來問 Wayne**

---

## 參考資料

### 重要檔案位置
- 主程式：`D:\projects\wayne\AI_trade\bot.py`（121KB）
- 策略框架：`D:\projects\wayne\AI_trade\src\ai_trade\strategy.py`
- 籌碼分析：`D:\projects\wayne\AI_trade\src\ai_trade\chips.py`
- 完整 audit 報告：`D:\projects\wayne\AI_trade\AUDIT_REPORT.md`
- 部署 SOP：`D:\projects\wayne\AI_trade\GITHUB_ACTIONS_SETUP.md`
- GitHub Actions workflow：`D:\projects\wayne\AI_trade\.github\workflows\trading_bot.yml`

### 已知不要動的東西
- `chips.py` 的 TWSE 日期智能回溯邏輯 — 經審查正確
- MA20 遲滯帶設計（`bot.py:1370-1374`） — 經審查合理
- `_business_days_between()` — 經審查正確（不含週末，已知不考慮國定假日）

### Wayne 的策略哲學摘要
- 短線、保守、靠分散和早出場活著
- 不預測方向，只跟風（VWAP 突破 + 法人籌碼 + 量能放大）
- 進場慢、出場快的非對稱設計
- 知道自己劣勢是手續費 → 用 `MIN_ORDER_VALUE` 把關
