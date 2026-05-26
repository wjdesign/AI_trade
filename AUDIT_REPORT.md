# AI_trade 程式碼審查報告

審查日期：2026-05-25
審查範圍：`bot.py` / `src/ai_trade/strategy.py` / `src/ai_trade/chips.py` / `README.md` / `.github/workflows/trading_bot.yml`
審查目的：(1) 找出程式碼與 README 不一致處 (2) 識別潛在 bug 與配置風險 (3) 確認 `_calc_position_size()` 的衝突處理

---

## 緊急問題（已修正）

審查過程發現兩個會直接影響上線安全與策略行為的問題，已當場修復。

### 🔴 [CRITICAL-1] bot 預設為「正式交易」模式

**檔案位置**：`bot.py:448`

**修正前**：
```python
self._simulation = False   # ← 切換正式交易時改為 False
```

**問題嚴重性**：**極高**。
- README 第 354 行明確說「切換正式交易時要把 `simulation=True` 改為 `simulation=False`」，暗示預設應為 `True`
- bot.py 第 3 行的 docstring 寫「模式：simulation=True（模擬盤）」
- 但實際程式碼中 `self._simulation` 已經是 `False`

任何照 README 流程設定 GitHub Actions、填好 Secrets 並啟動 workflow 的使用者，**會立刻用真實資金進行自動交易**，而不是預期的模擬。

**修正後**：
```python
# ⚠️ 重要：True = 模擬交易（不動用真實資金）；False = 正式交易（會真的下單買賣）
# 預設保留 True，確認所有流程穩定後再手動改為 False 切換正式交易。
self._simulation = True   # ← 切換正式交易時改為 False
self.api = sj.Shioaji(simulation=self._simulation)
```

---

### 🔴 [CRITICAL-2] 市場狀態偵測的波動率閾值為測試值

**檔案位置**：`src/ai_trade/strategy.py:32`

**修正前**：
```python
_VOL_THRESHOLD = 0.015  # 模擬測試用 1.5%（正式交易改回 0.18）
```

**問題嚴重性**：**高**。台股 0050 近 20 日年化波動率歷史上幾乎都在 10%~25% 區間，閾值設為 1.5% 意味著 `detect_regime()` **永遠回傳 `RANGING`（盤整市）**。

導致的實際行為：
- `StrategyAllocator` 永遠分配「動能 30% / 均值回歸 70%」
- 動能策略長期被壓制，永遠不會進入「動能 80% / 均值回歸 20%」的趨勢市配置
- README 第 211-217 行描述的「雙引擎動態切換」**從來沒有真的雙向切換過**

**修正後**：
```python
# 0050 近 20 日年化波動率閾值
# 台股年化波動率歷史上約落在 15%~25% 區間，0.18 (18%) 是長期歷史中位數的合理切點。
# 高於 0.18 視為高波動（盤整/震盪市，均值回歸占優），低於 0.18 視為低波動（趨勢市，動能占優）。
# ⚠️ 1.5% (0.015) 為過去模擬測試用的極小值，會使策略永遠判定為盤整市，正式上線前已修正。
_VOL_THRESHOLD = 0.18
```

---

## `_calc_position_size()` 衝突分析

### 問題背景

README 描述 `MAX_POSITIONS = 3`，但實際程式碼是 `MAX_POSITIONS = 7`，配合 `MIN_ORDER_VALUE = 11000`、`TOTAL_BUDGET = 46000`，理論上 46000 ÷ 7 = 6,571 元/部位，遠低於 11,000 元最低下單門檻 → 永遠進不了場。

### 實際處理機制

`bot.py:76-96` 的 `_calc_position_size()` 用了一層動態退化邏輯：

```python
def _calc_position_size(total_budget: float) -> int:
    affordable          = int(total_budget // MIN_ORDER_VALUE)   # 46000 // 11000 = 4
    effective_positions = min(affordable, MAX_POSITIONS)         # min(4, 7) = 4
    if effective_positions <= 0:
        return int(total_budget // MAX_POSITIONS)
    return int(total_budget // effective_positions)              # 46000 // 4 = 11500
```

**結論**：衝突有被處理，但結果不直觀。

| 名義設定 | 實際運作 |
|---------|---------|
| `MAX_POSITIONS = 7`（程式碼寫死） | 因為 `MIN_ORDER_VALUE = 11000` 的限制，**實際最多只能同時持有 4 個部位** |
| `POSITION_SIZE = 6571`（46000/7） | 被退化邏輯覆寫為 **11,500 元/部位** |

### 潛在問題

1. **使用者預期 vs 實際不符**：你以為 bot 會分散到 7 檔，但實際只會持有 4 檔
2. **`MAX_POSITIONS = 7` 的設定值名存實亡**——無論你把它改成 5、6、7、8、9 都沒差，最後都是 4 檔
3. **持倉部位數會隨總預算變動**：如果 `TOTAL_BUDGET` 降到 33,000，會自動退化成 3 部位（33000÷11000=3）

### 建議

**選項 A**（最保守）：把 `MAX_POSITIONS` 改成 4，讓設定值跟實際行為一致，README 也順便更新

**選項 B**（恢復原始策略）：把 `MIN_ORDER_VALUE` 降回 9,000，讓 7 部位 × 6,571 變成可行（但要重新評估手續費影響）

**選項 C**（不動）：保留現狀但在 README 和註解明確說明「實際部位數會被 MIN_ORDER_VALUE 動態退化」

---

## 程式碼 vs README 差異對照表

### 參數差異

| 參數 | README 描述 | 程式碼實際 | 影響等級 |
|------|------------|----------|---------|
| `INITIAL_CAPITAL` | 未提及 | `68000`（用於累計損益計算） | 低，但 README 該補 |
| `TOTAL_BUDGET` | `45,000` | `46,000` | 低 |
| `MAX_POSITIONS` | `3` | `7`（實際退化為 4） | **高** |
| `POSITION_SIZE` | `15,000` | `11,500` | **高**，影響每筆風險 |
| `MIN_ORDER_VALUE` | `9,000` | `11,000` | 中 |
| `TRAILING_PULLBACK` | `1%` | `0.015`（1.5%） | 低 |
| `TIME_STOP` | `TIME_STOP_MINUTES = 30`（30 分鐘日內停損） | `TIME_STOP_BDAYS = 5`（5 工作天波段停損） | **極高，根本性策略差異** |
| 監控標的數 | `12 檔精選股票` | `70+ 檔`（包含未回測的圖片辨識自選清單） | **高** |
| `STOP_LOSS_PCT` 註解 | README 沒明寫，bot.py 寫「折衷取 2.5%」 | 實際值是 `0.03`（3%） | 中，註解誤導 |

### 概念差異

#### 1. 時間停損機制根本不同（最嚴重）

README 描述：
> **D. 時間停損（TIME_STOP_MINUTES > 0 時啟用）**
> 進場後 30 分鐘仍在成本 ±0.5% 以內 → 動能消失，主動出場

實際程式碼（`bot.py:1893-1906`）：
```python
# D. 時間停損
# 持有滿 TIME_STOP_BDAYS 工作天內未觸發 A/B/C，且獲利無明顯成長 → 強制出場
# 「無明顯成長」定義：歷史高點未曾突破移動止盈啟動價（entry × (1+TRAILING_START)）
TIME_STOP_BDAYS = 5
```

**兩者完全不同的策略概念**：
- README 描述的是「**當沖式日內動能停損**」：30 分鐘沒動就跑
- 實際程式碼是「**波段資金釋放停損**」：5 個工作天沒漲到 1.5% 就跑

這意味著整個 bot 的「持倉週期」跟 README 描述差了 100 倍以上——你以為它是日內當沖，實際上它願意持有一個部位整週。**這對你的資金週轉率、心理預期、與正式交易時的稅務計算（當沖 vs 一般證券交易）都有重大影響**。

#### 2. 監控標的清單已大幅膨脹且未經回測

README 第 227 行：
> 共 12 檔，以 yfinance 日 K 回測 2021–2026 驗證

實際 `PINNED_STOCKS`（`bot.py:134-212`）有 **70+ 檔**，分三段：
- 第 1 段：原有回測驗證清單（7 檔）
- 第 2 段：0050 成分股回測驗證通過（5 檔）
- 第 3 段：**「圖片辨識」自選清單，待回測驗證**（60+ 檔）

第 3 段的 60+ 檔（從 6664 群翊到 2408 南亞科）**完全沒經過任何回測**，註解明說「待回測驗證」。混進去就會被每分鐘掃描、每天評估進場。

#### 3. 漏斗掃描器：README 說已不用，但實際每天都跑

README 第 414 行：
> `scanner.py # 三層漏斗掃描器（備用，bot.py 已不使用）`

實際程式碼：
- `bot.py:39` 仍 import `FunnelScanner`
- `bot.py:125-128` 定義 `FUNNEL_SCAN_HOUR = 9`、`FUNNEL_SCAN_MINUTE = 20`、`FUNNEL_MAX_RESULTS = 5`
- `bot.py:545` 實例化 `self.funnel = FunnelScanner(...)`
- `bot.py:578-609` 每日 09:20 觸發漏斗掃描，結果合併至當日 watch_list

每日 09:20 漏斗掃描會**自動加入最多 5 檔新標的**到當日監控清單，這個機制完全沒在 README 出現。你可能不知道為什麼某天突然出現意料外的進場訊號。

#### 4. LONG_TERM_HOLD 機制 README 完全沒提

`bot.py:216-223` 定義了 `LONG_TERM_HOLD` 清單：
```python
LONG_TERM_HOLD: frozenset[str] = frozenset([
    "0050",   # ★★ 元大台灣50（長期核心持倉，包含台積電等優質藍籌）
])
```

註解說明「列在此處的股票不納入止損/止盈監控」。目前只有 0050 在裡面，影響不大，但這個機制在 README 完全沒提，使用者可能不知道它存在。

#### 5. MA20 大盤過濾的遲滯帶設計 README 沒提

`bot.py:1370-1374`：
```python
HYST = 0.001  # 遲滯帶 0.1%
if current > ma20 * (1 + HYST):
    self._market_trend_up = True
elif current < ma20 * (1 - HYST):
    self._market_trend_up = False
# 介於 ±0.1% 之間維持上次判斷，避免每分鐘翻轉
```

這個遲滯帶設計是好的（避免在 MA20 附近震盪時每分鐘翻來覆去），但 README 第 138-140 行只寫「0050 現價 > MA20 → 繼續」，沒有提這個 0.1% 緩衝帶。

#### 6. README 修改正式交易的指引行號錯誤

README 第 354 行：
> | `bot.py` | **339** | `sj.Shioaji(simulation=True)` → `sj.Shioaji(simulation=False)` |

實際的 simulation 設定在 **第 448 行**（且不是直接寫 `sj.Shioaji(simulation=True)`，而是透過 `self._simulation` 變數）。照 README 指引去改 339 行會改錯地方。

---

## 其他可疑/過時的程式碼

### bot.py 第 5 行 docstring 過時

```python
"""
- 策略：大盤月線過濾 + OpenAI 情緒分析 + VWAP 進場
        + 滑點保護 + 移動止盈 + 2% 強制止損
"""
```

- 「大盤**月線**」實際是 MA20 日均線
- 「2% 強制止損」實際是 3%（`STOP_LOSS_PCT = 0.03`）

### bot.py 第 53 行註解前後矛盾

```python
STOP_LOSS_PCT = 0.03   # 強制止損：虧損 3%（回測驗證：2% 橫盤假止損過多，3% 最大回撤控制較優，折衷取 2.5%）
```

值是 `0.03`（3%），但註解末尾說「折衷取 2.5%」，前後不符。

### bot.py 第 448 行註解誤導

```python
self._simulation = False   # ← 切換正式交易時改為 False  ← 註解和值都已經是 False
```

這已經是「正式交易模式」的狀態，但註解寫法看起來像是「目前是模擬，要切正式才改成 False」。**已隨 CRITICAL-1 一併修正**。

---

## 已通過審查的部分

以下檔案/邏輯經審查沒有明顯問題，可放心使用：

- ✅ `chips.py` 智能日期回溯邏輯（盤中用昨日、盤後用今日、最多回溯 5 天）
- ✅ `chips.py` TWSE API 解析（已處理 int / str 混合型別）
- ✅ `MA20` 遲滯帶防抖動設計（雖然 README 沒寫，但設計合理）
- ✅ `_calc_position_size()` 退化邏輯本身正確（雖然結果不直觀）
- ✅ `_business_days_between()` 工作天計算（不含週末，已知不考慮國定假日）
- ✅ GitHub Actions workflow 結構完整、Secrets 處理正確
- ✅ Telegram 通知邏輯與時機完整

---

## 本次審查已修正的檔案

| 檔案 | 修正內容 |
|------|---------|
| `bot.py` (line 448) | `self._simulation = False` → `self._simulation = True`，並補強註解 |
| `src/ai_trade/strategy.py` (line 32) | `_VOL_THRESHOLD = 0.015` → `_VOL_THRESHOLD = 0.18`，並補完整註解 |

---

## 建議的後續處理（依優先順序）

### 立即處理（上線前必做）

1. **全面更新 README** 以反映實際參數：`TOTAL_BUDGET=46000`、`MAX_POSITIONS=7`（並註明實際退化為 4）、`MIN_ORDER_VALUE=11000`、`POSITION_SIZE=11500`
2. **決定時間停損策略方向**：要回到 README 描述的 30 分鐘日內停損，還是接受程式碼現狀的 5 工作天波段停損？這是策略哲學決策，不是 bug
3. **修正 README 第 354 行的行號錯誤**（339 → 448），並改為描述變數修改而非整行替換

### 短期處理（上線後 1 個月內）

4. **PINNED_STOCKS 瘦身**：60+ 檔未回測的自選股建議移到註解中或獨立清單，避免污染主監控池
5. **將 `STOP_LOSS_PCT` 註解修正**，移除「折衷取 2.5%」這段矛盾說明
6. **將 `MAX_POSITIONS` 改為 4**（與實際退化值一致），或在 README 中清楚說明「實際部位數會被 MIN_ORDER_VALUE 動態退化」
7. **將 README 第 414 行「scanner.py（備用，bot.py 已不使用）」改為實際說明漏斗掃描器的角色**
8. **在 README 補上 `LONG_TERM_HOLD` 機制說明**

### 中期處理（穩定運行 3 個月後）

9. **考慮把 `simulation` 改為環境變數讀取**，例如從 `.env` 讀 `SIMULATION=true/false`，避免每次切換都要改原始碼
10. **將監控股票清單抽到外部 YAML/JSON 設定檔**，不要寫死在 `bot.py`
11. **考慮把策略參數搬到 `pyproject.toml` 或獨立 `config.py`**，bot.py 121KB 已經太肥，難以維護
