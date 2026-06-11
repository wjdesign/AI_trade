"""
AI 模擬交易機器人
- 模式：由 SIMULATION 環境變數決定，預設模擬盤（不動用真實資金）；
        正式交易需同時設 SIMULATION=false 與 CONFIRM_REAL_MONEY=I_KNOW_THIS_IS_REAL_MONEY 雙重確認
- 策略：大盤 MA20 過濾（含 0.1% 遲滯帶）+ OpenAI 情緒分析（可關閉）+ VWAP 進場
        + 滑點保護 + 移動止盈（ATR 動態回撤）+ 3% 強制止損 + 5 工作天時間停損
- 支援：多標的同時監控（PINNED_STOCKS）+ 每日 09:20 漏斗掃描動態加股
        + LONG_TERM_HOLD 長線持有清單（不納入停損監控）+ 部位上限 MAX_POSITIONS
- 部署：GitHub Actions 自動執行 + Artifacts 跨 run 備份還原 logs/
"""

import math
import os
import sys
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml

os.environ.setdefault("PYTHONUNBUFFERED", "1")
# Windows cp950 console 無法輸出 emoji (🟢/🔴 等)，強制 UTF-8 避免崩潰
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, TypeError):
    pass

import shioaji as sj
import pandas as pd
import pandas_ta as ta
import requests
from openai import OpenAI
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

TZ_TW = timezone(timedelta(hours=8))  # 台灣時間 UTC+8


def now_tw() -> datetime:
    """回傳台灣當地時間（不論伺服器在哪）"""
    return datetime.now(TZ_TW)
from src.ai_trade.news import NewsAggregator
from src.ai_trade.chips import chips_sentiment, chips_summary           # 2.2 籌碼流向
from src.ai_trade.strategy import (                                      # 3.2 多策略
    StrategyAllocator, mean_reversion_signal, MarketRegime, AllocationResult
)
from src.ai_trade.scanner import FunnelScanner                          # 漏斗掃描器

load_dotenv()

# =============================================================================
# 1. 參數設定
# =============================================================================

# ── 環境變數 helper：5 個常調參數可透過 env / GitHub Variables 覆蓋預設值 ──
# 本機：.env 加 MAX_POSITIONS=3 之類
# 雲端：Repo Settings → Variables → 加同名 Variable（不是 Secret，因為不敏感）
# 任一變數未設或值不合法時 fallback 到下方預設值
def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        print(f"[警告] 環境變數 {key}='{val}' 不是有效整數，沿用預設值 {default}")
        return default

def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        print(f"[警告] 環境變數 {key}='{val}' 不是有效浮點數，沿用預設值 {default}")
        return default

def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in {"true", "1", "yes", "on"}


INITIAL_CAPITAL   = 68000                                # 原始投入資金（元），用於計算累計損益
TOTAL_BUDGET      = _env_int("TOTAL_BUDGET", 46000)      # 總預算（元）。可透過環境變數覆蓋
MAX_POSITIONS     = _env_int("MAX_POSITIONS", 4)         # 最多同時持有部位數。可透過環境變數覆蓋
POSITION_SIZE     = TOTAL_BUDGET // MAX_POSITIONS        # 初始值，MIN_ORDER_VALUE 定義後由 _calc_position_size() 修正


STOP_LOSS_PCT        = _env_float("STOP_LOSS_PCT", 0.03) # 強制止損%。可透過環境變數覆蓋（例如 "0.025" = 2.5%）
TRAILING_START       = 0.015   # 移動止盈啟動點：獲利達 1.5%
TRAILING_PULLBACK    = 0.015    # 移動止盈觸發（ATR 不足時的保底固定回撤）
TRAILING_ATR_MULT    = 0.6     # 動態回撤：從最高點回落 0.6×ATR 時出場（ATR 夠大時優先）
BREAKEVEN_TRIGGER    = 0.02    # 成本保衛：獲利達 2% 時自動將止損上移至成本價
TIME_STOP_BDAYS      = 5       # 時間停損：持有超過 N 個工作天仍未觸發其他出場條件 → 強制出場，釋放資金
SLIPPAGE_LIMIT       = 0.01    # 滑點保護：買賣價差 > 1%（零股市場天生價差較大，原 0.5% 過嚴）
MIN_ORDER_VALUE      = _env_int("MIN_ORDER_VALUE", 11_000)  # 最小下單金額（元）。可透過環境變數覆蓋


# 正式交易雙重確認字串。長度刻意設計到「不可能不小心打對」的程度，
# 唯一通過方式：使用者從 .env.example 或 SOP 文件複製貼上完整字串。
REAL_MONEY_CONFIRMATION = "I_KNOW_THIS_IS_REAL_MONEY"


def _parse_simulation_env() -> bool:
    """
    從環境變數讀取交易模式設定，**含雙重確認防呆機制**。

    模擬模式判定 (回傳 True)：
      - SIMULATION 未設定，或值為：true / 1 / yes / sim / simulation
      - 任何無法識別的值（亂碼、中文、空白等）

    正式模式判定 (回傳 False)，必須**同時滿足**：
      1. SIMULATION 設為：false / 0 / no / real / production / prod
      2. CONFIRM_REAL_MONEY 設為：I_KNOW_THIS_IS_REAL_MONEY（精確匹配，區分大小寫）

    ⚠️ 多層安全網：
      - 未設定 SIMULATION → 模擬
      - SIMULATION 值無法識別 → 模擬
      - SIMULATION=false 但 CONFIRM_REAL_MONEY 沒設 → 模擬 + 大警告
      - SIMULATION=false 但 CONFIRM_REAL_MONEY 值錯誤 → 模擬 + 大警告
      - 兩者皆正確 → 正式（會印出醒目紅色警告）
    """
    val = os.environ.get("SIMULATION", "").strip().lower()
    real_values = {"false", "0", "no", "real", "production", "prod"}

    # 第一層：SIMULATION 沒明確要求正式 → 直接回模擬
    if val not in real_values:
        return True

    # 第二層：要切換正式必須通過雙重確認
    confirm = os.environ.get("CONFIRM_REAL_MONEY", "").strip()
    if confirm != REAL_MONEY_CONFIRMATION:
        print("")
        print("⚠️ " * 30)
        print("⚠️  [防呆機制觸發] SIMULATION=false，但雙重確認未通過！")
        print(f"⚠️  目前 CONFIRM_REAL_MONEY = {confirm!r}")
        print(f"⚠️  需設定為                = {REAL_MONEY_CONFIRMATION!r}")
        print("⚠️  ")
        print("⚠️  為保護你的資金安全，**自動 fallback 到模擬交易模式**。")
        print("⚠️  若確實要切換正式交易，請：")
        print("⚠️    1. 本機：在 .env 加入 CONFIRM_REAL_MONEY=I_KNOW_THIS_IS_REAL_MONEY")
        print("⚠️    2. GitHub Actions：在 Secrets 新增 CONFIRM_REAL_MONEY = 上述字串")
        print("⚠️ " * 30)
        print("")
        return True

    # 兩道關卡都通過：確認進入正式交易
    return False


_SHIOAJI_OBJ_DIAG_PRINTED = False  # module-level flag，只印 1 次診斷避免雲端 log 爆量


def _shioaji_obj_to_dict(obj) -> dict:
    """
    將 Shioaji 物件（KBars / Ticks）轉成 dict 給 pandas.DataFrame 使用。
    Shioaji 1.3.x: 有 model_dump()
    Shioaji 1.5.x: 移除了 model_dump()，需 fallback。可能用 Pydantic v2、
                   也可能是別的容器類型。各 Tier 依序嘗試。

    若所有 Tier 都失敗或回傳空 dict，會印一次 [DIAG] 詳細 obj 結構，
    給開發者抓 1.5.x 的實際介面。
    """
    global _SHIOAJI_OBJ_DIAG_PRINTED

    # Tier 1: Shioaji 1.3.x
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass

    # Tier 2: Pydantic v1 .dict()
    if hasattr(obj, "dict") and callable(obj.dict):
        try:
            return obj.dict()
        except Exception:
            pass

    # Tier 3: Pydantic v2 model_fields
    if hasattr(obj, "model_fields"):
        try:
            return {f: getattr(obj, f, None) for f in obj.model_fields}
        except Exception:
            pass

    # Tier 4: Pydantic v1 __fields__
    if hasattr(obj, "__fields__"):
        try:
            return {f: getattr(obj, f, None) for f in obj.__fields__}
        except Exception:
            pass

    # Tier 5: 公開非 callable 屬性逐一取值
    result = {}
    for a in dir(obj):
        if a.startswith("_"):
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if callable(v):
            continue
        result[a] = v

    # 若 result 為空或顯然沒對齊 → 印 1 次診斷
    expected_cols = {"ts", "Open", "High", "Low", "Close", "Volume"}
    if not _SHIOAJI_OBJ_DIAG_PRINTED and (not result or not expected_cols & set(result.keys())):
        _SHIOAJI_OBJ_DIAG_PRINTED = True
        try:
            type_name = type(obj).__name__
            module = type(obj).__module__
            attrs = [a for a in dir(obj) if not a.startswith("_")][:30]
            print(f"[DIAG] _shioaji_obj_to_dict 無法解析 obj：")
            print(f"  type     = {module}.{type_name}")
            print(f"  attrs    = {attrs}")
            print(f"  got keys = {list(result.keys())[:20]}")
            # 試一些常見欄位
            for sample_key in ("Close", "close", "ts", "ts_array", "Open"):
                if hasattr(obj, sample_key):
                    v = getattr(obj, sample_key, None)
                    print(f"  {sample_key}: type={type(v).__name__}, len={len(v) if hasattr(v, '__len__') else 'n/a'}")
        except Exception as e:
            print(f"[DIAG] 印診斷時例外（忽略）: {e}")

    return result


def _fetch_twse_kbars(code: str, days: int = 90) -> pd.DataFrame:
    """從 TWSE 官方 STOCK_DAY API 抓日 K。對 GitHub Actions runner IP（Azure / AWS）
    比 yfinance 穩定 — TWSE 對所有 IP 開放，yfinance / Yahoo Finance 對非美國 IP
    經常 rate-limit 或 503。
    一次抓一個月，跨月迴圈累積到所需天數。
    回傳欄位跟 Shioaji KBars 一致：ts/Open/High/Low/Close/Volume/Amount。
    """
    end_dt = now_tw()
    months_needed = max(2, (days // 30) + 2)

    rows = []
    for i in range(months_needed):
        month_dt = end_dt - timedelta(days=i * 30)
        date_str = month_dt.strftime("%Y%m") + "01"
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
        params = {"date": date_str, "stockNo": code, "response": "json"}
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("stat") != "OK":
                continue
            for row in data.get("data", []):
                try:
                    # row 格式：[日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數]
                    date_str_tw = row[0].strip()
                    y, m, d = date_str_tw.split("/")
                    ts = pd.Timestamp(f"{int(y) + 1911}-{m.zfill(2)}-{d.zfill(2)}")
                    rows.append({
                        "ts":     ts,
                        "Open":   float(row[3].replace(",", "")),
                        "High":   float(row[4].replace(",", "")),
                        "Low":    float(row[5].replace(",", "")),
                        "Close":  float(row[6].replace(",", "")),
                        "Volume": int(row[1].replace(",", "")) // 1000,  # 股 → 張
                        "Amount": int(row[2].replace(",", "")),
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"[TWSE/{code}] 抓 {date_str[:6]} 失敗: {type(e).__name__}: {e}")
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def _fetch_yfinance_kbars(code: str, days: int = 90) -> pd.DataFrame:
    """從 yfinance 抓台股日 K 作為 Shioaji 模擬戶 kbars() 不支援時的 fallback。
    Shioaji 1.5.x 模擬戶 api.kbars() 對所有 ticker 都回空 list（已驗證）。
    回傳欄位跟 Shioaji KBars 一致：ts/Open/High/Low/Close/Volume/Amount，
    呼叫端可直接做 pd.DataFrame() 風格的處理。
    抓不到回空 DataFrame，讓上層自行 fallback。
    """
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()

    end_dt = now_tw()
    start_dt = end_dt - timedelta(days=days)
    end_str = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")  # yfinance end 為排他
    start_str = start_dt.strftime("%Y-%m-%d")

    raw = pd.DataFrame()
    for suffix in (".TW", ".TWO"):
        ticker = f"{code}{suffix}"
        try:
            raw = yf.download(ticker, start=start_str, end=end_str,
                              auto_adjust=True, progress=False)
            if not raw.empty:
                break
        except Exception:
            continue

    if raw.empty:
        return pd.DataFrame()

    # 攤平 yfinance >= 0.2 的 MultiIndex
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    # Normalize 到 Shioaji KBars schema
    raw = raw.reset_index()
    raw = raw.rename(columns={"Date": "ts"})
    if "Amount" not in raw.columns:
        raw["Amount"] = 0.0
    cols = ["ts", "Open", "High", "Low", "Close", "Volume", "Amount"]
    return raw[[c for c in cols if c in raw.columns]]


def _business_days_between(start_date, end_date) -> int:
    """計算兩日期間的工作天數（不含週末，不考慮國定假日）"""
    if start_date >= end_date:
        return 0
    days = 0
    d = start_date
    while d < end_date:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon, 4=Fri
            days += 1
    return days


def _calc_position_size(total_budget: float) -> int:
    """
    計算單筆可用預算，依實際可承接部位數動態退化：

      affordable       = total_budget // MIN_ORDER_VALUE  （預算最多能開幾個有效部位）
      effective_positions = min(affordable, MAX_POSITIONS)

      effective > 0 → POSITION_SIZE = total_budget // effective_positions
      effective = 0 → 回傳原始值（< MIN_ORDER_VALUE，進場時會被擋下）

    當前預設組合（TOTAL_BUDGET=46,000、MIN_ORDER_VALUE=11,000、MAX_POSITIONS=4）：
      affordable=4, effective=4, POSITION_SIZE=11,500

    動態退化範例（MIN_ORDER_VALUE=11,000）：
      46,000 / MAX_POS=4 → affordable=4, effective=4, size=11,500
      33,000 / MAX_POS=4 → affordable=3, effective=3, size=11,000
      22,000 / MAX_POS=4 → affordable=2, effective=2, size=11,000
      11,000 / MAX_POS=4 → affordable=1, effective=1, size=11,000
      10,000 / MAX_POS=4 → affordable=0, blocked
    """
    affordable          = int(total_budget // MIN_ORDER_VALUE)
    effective_positions = min(affordable, MAX_POSITIONS)
    if effective_positions <= 0:
        return int(total_budget // MAX_POSITIONS)   # < MIN_ORDER_VALUE，進場時自動擋下
    return int(total_budget // effective_positions)


POSITION_SIZE = _calc_position_size(TOTAL_BUDGET)

# Phase 1 優化參數
SENTIMENT_ENABLED  = _env_bool("SENTIMENT_ENABLED", False)   # 新聞情緒評分開關。可透過環境變數覆蓋（"true" 或 "false"）
# 模擬戶限制：TWSE 日 K 收盤後才更新 → bot 整天用「昨日收盤」判斷大盤
# 0050 跟 MA20 差距常常 < 0.5% → 遲滯帶內隨機決定 → 整天卡在「趨勢偏弱」
# 開啟此開關直接跳過大盤過濾，讓 bot 任何時候都掃個股（適合模擬測試 / 觀察 bot 行為）
# 正式戶下不建議開啟（大盤過濾是保護資金的關鍵）
SKIP_MARKET_FILTER = _env_bool("SKIP_MARKET_FILTER", False)
SENTIMENT_SMOOTH_N = 3      # 1.1 情緒平滑：保留最近 N 次分數取均值
RISK_PER_TRADE     = TOTAL_BUDGET * STOP_LOSS_PCT   # 1.2 ATR 動態部位：每筆承擔最大損失 (元)
RSI_OVERBOUGHT     = 70                             # 1.3 RSI 超買門檻：超過不進場

# Phase 2 優化參數
TRADE_COST_PCT     = 0.004   # 2.3 手續費+證交稅估算（買0.1425%+賣0.1425%+賣0.3% ≈ 0.585%，保守用0.4%）

# 進場條件強化參數
RVOL_MIN           = 1.5     # 相對成交量門檻：現量須為 5 日均量的 1.5 倍以上（量能確認突破）
RSI_DYNAMIC        = True    # 動態 RSI：上升趨勢中允許放寬至 RSI_OVERBOUGHT_LAX
RSI_OVERBOUGHT_LAX = 75      # 動態 RSI 放寬門檻（RSI 持續向上時適用）
VWAP_MAX_GAP       = 0.03    # VWAP 乖離率上限：現價超過 VWAP 3% 視為過熱，不追
ATR_MAX_PCT        = 0.03    # ATR 過熱保護：ATR/股價 > 3% 視為跳空風險過高，不進場
MA_TREND_PERIOD    = 50      # 趨勢過濾均線：個股現價需在 MA50 之上才進場（回測驗證有效）
MARKET_INDEX       = "0050"  # 大盤指數代碼（主板用 0050，中小型股可改 0051）

SCAN_INTERVAL           = 60    # 主循環間隔（秒）
NEWS_DIGEST_INTERVAL    = 1800  # 非交易時間新聞推播間隔（秒）
PENDING_ORDER_TIMEOUT   = _env_int("PENDING_ORDER_TIMEOUT", 600)   # 委託逾時自動撤單（秒）：超過此時間未成交則向交易所送出取消
BUDGET_REFRESH_INTERVAL = 600   # 預算重查間隔（秒）：每 N 秒重查 settlements() 並推播
STATUS_REPORT_INTERVAL  = 1800  # 部位狀態推播間隔（秒）：每 N 秒推播委託 + 部位狀態

# 漏斗掃描觸發時間（每日一次，開盤 15 分鐘後）
FUNNEL_SCAN_HOUR   = 9    # 09:20 觸發
FUNNEL_SCAN_MINUTE = 20
FUNNEL_MAX_RESULTS = 5    # 漏斗最多取幾檔加入當日監控清單

# ─────────────────────────────────────────────────────────────
# 監控標的：從 config/watchlist.yaml 載入
#   pinned         → PINNED_STOCKS（每輪必掃，符合條件自動下單）
#   candidates     → CANDIDATE_STOCKS（僅追蹤不交易；主迴圈不引用）
#   long_term_hold → LONG_TERM_HOLD（不納入停損/停利監控）
# 改清單方式：編輯 config/watchlist.yaml + commit + push（不用動 bot.py）
# 完整 schema 說明見該 yaml 檔頂部註解
# ─────────────────────────────────────────────────────────────
def _load_watchlist() -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str], dict[str, str]]:
    """從 config/watchlist.yaml 載入監控清單。
    回傳 (pinned, candidates, long_term_hold, stock_names)。
    yaml 不存在或 schema 錯誤 → 立刻 raise，不要靜默回空（會讓 bot 沒監控標的）。
    """
    yaml_path = Path(__file__).parent / "config" / "watchlist.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"監控清單檔案不存在：{yaml_path}\n"
            f"請從 git repo checkout 完整檔案，或建立 config/watchlist.yaml"
        )
    with yaml_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    stock_names: dict[str, str] = {}

    def _extract_codes(section: str) -> list[str]:
        items = data.get(section, []) or []
        codes = []
        for item in items:
            if isinstance(item, dict) and "code" in item:
                code = str(item["code"])
                codes.append(code)
                if "name" in item and item["name"]:
                    stock_names[code] = str(item["name"])
            elif isinstance(item, str):
                codes.append(item)
            else:
                print(f"[警告] watchlist.yaml {section} 內項目格式無法解析：{item!r}")
        return codes

    pinned         = tuple(_extract_codes("pinned"))
    candidates     = tuple(_extract_codes("candidates"))
    long_term_hold = frozenset(_extract_codes("long_term_hold"))
    print(
        f"[配置] watchlist.yaml 載入完成："
        f"pinned={len(pinned)}、candidates={len(candidates)}、long_term_hold={len(long_term_hold)}"
    )
    return pinned, candidates, long_term_hold, stock_names


PINNED_STOCKS, CANDIDATE_STOCKS, LONG_TERM_HOLD, STOCK_NAMES = _load_watchlist()


def stock_label(code: str) -> str:
    """股號 + 名稱（Telegram 訊息用）；watchlist.yaml 無 name 時退回純股號。"""
    name = STOCK_NAMES.get(code, "")
    return f"{code} {name}" if name else code

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
tg_token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")


# =============================================================================
# 2. 資料結構
# =============================================================================

@dataclass
class Position:
    code: str
    entry_price: float
    qty: int
    # 2.1 ATR 自適應止損：進場時計算，取代固定百分比
    atr:         float = 0.0   # 進場時 ATR 值（元）
    stop_price:  float = 0.0   # 動態止損價（entry - 1.5×ATR，最多 -2%）
    trail_price: float = 0.0   # 移動止盈啟動價（entry + 1.0×ATR，最少 1.5%）
    # 進場輔助資訊（供績效日誌 2.3 使用）
    entry_score: float = 0.0
    entry_rsi:   float = 0.0
    entry_vwap:  float = 0.0
    entry_chips: float = 0.0   # 法人淨買超（股，2.2）
    max_price:   float = field(init=False)
    entry_time:  datetime = field(init=False)   # 進場時間（time stop 使用）

    def __post_init__(self):
        self.max_price  = self.entry_price
        self.entry_time = now_tw()
        # 若未帶入 ATR，退回固定百分比
        if self.stop_price == 0.0:
            self.stop_price = self.entry_price * (1 - STOP_LOSS_PCT)
        if self.trail_price == 0.0:
            self.trail_price = self.entry_price * (1 + TRAILING_START)

    def update_max(self, current: float) -> None:
        if current > self.max_price:
            self.max_price = current

    def profit_pct(self, current: float) -> float:
        return (current - self.entry_price) / self.entry_price

    def pullback_pct(self, current: float) -> float:
        if self.max_price <= 0:
            return 0.0
        return (self.max_price - current) / self.max_price


@dataclass
class BuyCandidate:
    """掃描階段收集的候選進場標的，尚未下單"""
    code:        str
    strategy:    str    # "momentum" | "mean_reversion"
    price:       float
    qty:         int
    vwap:        float
    rsi:         float
    chip_score:  float
    atr_val:     float
    stop_price:  float
    trail_price: float
    score:       float  # 排序依據：VWAP 突破幅度 × 0.5 + 法人情緒 × 0.5
    rvol:        float = 0.0  # 相對成交量（次要排序鍵）

    def describe(self) -> str:
        tag = "動能" if self.strategy == "momentum" else "均值回歸"
        return (f"[候選/{tag}] {self.code}  "
                f"價={self.price}  VWAP={self.vwap:.2f}  "
                f"RSI={self.rsi:.1f}  法人={self.chip_score:+.2f}  "
                f"綜合分={self.score:.3f}")

    def pullback_pct(self, current: float) -> float:
        return (self.max_price - current) / self.max_price


# =============================================================================
# 3. 工具函數
# =============================================================================

def _telegram_post(token: str, chat_id: str, msg: str) -> None:
    proxies = {}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            proxies=proxies or None,
            timeout=15,
        )
        if not resp.ok:
            print(f"[Telegram] 回應錯誤 {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"[Telegram] 通知失敗: {e}")


def send_notify(msg: str) -> None:
    print(f"[Telegram] {msg}")
    if not tg_token or not tg_chat_id:
        return
    token = tg_token.strip()
    if "\n" in token or ":" not in token:
        print("[Telegram] 錯誤：TELEGRAM_BOT_TOKEN 格式不正確，請確認 .env 無換行。")
        return
    threading.Thread(
        target=_telegram_post,
        args=(token, tg_chat_id.strip(), msg),
        daemon=True,
    ).start()


def get_ai_sentiment(news_text: str) -> tuple[float, str]:
    """OpenAI 語意分析：回傳 (情緒分數 -1.0~1.0, 繁中摘要)
    SENTIMENT_ENABLED=False 時 early return 1.0，避免「啟動分析」之類
    無條件呼叫者打到 OpenAI 浪費費用或在 API key 為 dummy 時報 401。
    """
    if not SENTIMENT_ENABLED:
        return 1.0, "情緒分析已停用 (SENTIMENT_ENABLED=False)"
    try:
        prompt = (
            "你是台股分析師。請根據以下新聞標題分析對整體台股的影響，"
            "回傳格式如下（共兩行）：\n"
            "第一行：一個數字（-1.0 至 1.0），1.0 代表極度利多\n"
            "第二行：50 字以內的繁體中文分析摘要\n\n"
            "新聞：\n" + news_text
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content.strip()
        lines = content.splitlines()
        score = float(lines[0].strip())
        analysis = lines[1].strip() if len(lines) > 1 else ""
        return score, analysis
    except Exception as e:
        print(f"AI 分析失敗: {e}")
        return 0.0, ""


def ticks_to_df(ticks) -> pd.DataFrame:
    """將 Shioaji ticks 轉為 DataFrame，統一欄位名稱為 pandas_ta 所需格式（大寫）"""
    df = pd.DataFrame(_shioaji_obj_to_dict(ticks))
    df["datetime"] = pd.to_datetime(df["ts"])
    df = df.set_index("datetime").sort_index()
    # Shioaji ticks 欄位皆為小寫，pandas_ta.vwap 需要大寫
    rename = {"open": "Open", "high": "High", "low": "Low",
              "close": "Close", "volume": "Volume", "amount": "Amount"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    # ticks 只有成交價（close），補齊 High/Low/Open 供 VWAP 計算
    for col in ("High", "Low", "Open"):
        if col not in df.columns and "Close" in df.columns:
            df[col] = df["Close"]
    return df


def sentiment_label(score: float) -> str:
    if score > 0.3:
        return "利多"
    if score < -0.3:
        return "利空"
    return "中性"


# =============================================================================
# 4. 核心交易邏輯
# =============================================================================

def _get_git_sha() -> str:
    """取 git commit SHA（短）。雲端 GitHub Actions 用 GITHUB_SHA env var；
    本機 fallback 讀 .git/HEAD。"""
    # 雲端
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha:
        return sha[:7]
    # 本機 fallback
    try:
        head_file = Path(__file__).parent / ".git" / "HEAD"
        if head_file.exists():
            ref = head_file.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                ref_path = Path(__file__).parent / ".git" / ref[5:]
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:7]
            return ref[:7]
    except Exception:
        pass
    return "unknown"


def _debug_env() -> None:
    """啟動時印出環境變數摘要（敏感值遮蔽），協助診斷 GitHub Actions 問題"""
    import base64

    git_sha = _get_git_sha()
    print(f"[Debug] Git commit: {git_sha}  ← 雲端跑的就是這個版本，若 bug 對應的 commit 已 merge，這裡應該顯示對應 SHA")

    def mask(v: str, show: int = 2) -> str:
        """敏感資料遮蔽：露頭 show + 尾 show 個字元，其餘用 *** 取代。
        預設 show=2 → 對 44 字元 API key 只露 4/44 = 9%（之前 show=4 露 18%）。
        """
        v = v.strip()
        if not v:
            return "(未設定)"
        if len(v) <= show * 2:
            return "***"
        return v[:show] + "***" + v[-show:]

    # ── 敏感資料（已遮罩） ──────────────────────────────────────
    secrets_info = {
        "API_KEY":            os.environ.get("API_KEY", ""),
        "SECRET_KEY":         os.environ.get("SECRET_KEY", ""),
        "CA_CERT_PATH":       os.environ.get("CA_CERT_PATH", ""),
        "CA_PASSWORD":        os.environ.get("CA_PASSWORD", ""),
        "OPENAI_API_KEY":     os.environ.get("OPENAI_API_KEY", ""),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID":   os.environ.get("TELEGRAM_CHAT_ID", ""),
    }

    print("[Debug] ── 敏感變數（已遮罩） ──────────────────────")
    for k, v in secrets_info.items():
        stripped = v.strip()
        print(f"  {k:<22}: {mask(stripped)}  (len={len(stripped)})")

    # ── 模式控制變數 ────────────────────────────────────────
    sim = os.environ.get("SIMULATION", "").strip()
    confirm = os.environ.get("CONFIRM_REAL_MONEY", "").strip()
    print("[Debug] ── 模式控制 ────────────────────────────────")
    print(f"  {'SIMULATION':<22}: {sim or '(未設定 → 預設 true=模擬)'}")
    print(f"  {'CONFIRM_REAL_MONEY':<22}: {mask(confirm) if confirm else '(未設定 → 防呆生效)'}")

    # ── 策略參數（GitHub Variables；未設則 bot.py 內 fallback 預設值）──
    # 每個參數附「用途說明」，方便看 log 時無需翻文件就懂
    strategy_vars = [
        ("MAX_POSITIONS",         "最多同時持有部位數"),
        ("TOTAL_BUDGET",          "總可用資金（元）"),
        ("STOP_LOSS_PCT",         "強制止損% (與 1.5×ATR 取嚴格者)"),
        ("MIN_ORDER_VALUE",       "最小下單金額（元，避免手續費侵蝕）"),
        ("SENTIMENT_ENABLED",     "AI 新聞情緒分析開關 (需 OpenAI API)"),
        ("SKIP_MARKET_FILTER",    "跳過大盤 0050/MA20 過濾（模擬戶測試用）"),
        ("PENDING_ORDER_TIMEOUT", "委託逾時自動撤單秒數（零股流動性低可調 1800+）"),
    ]
    print("[Debug] ── 策略參數（GitHub Variables）─────────────")
    for k, desc in strategy_vars:
        val = os.environ.get(k, "").strip() or "(未設定 → 用 bot.py 預設)"
        print(f"  {k:<22}: {val:<32} # {desc}")

    # ── bot.py 實際生效值（重要：這才是 bot 真的用的值）──────
    actual_values = [
        ("MAX_POSITIONS",         str(MAX_POSITIONS),                "最多同時持有部位數"),
        ("TOTAL_BUDGET",          f"{TOTAL_BUDGET:,}",               "總可用資金（元）"),
        ("STOP_LOSS_PCT",         f"{STOP_LOSS_PCT}",                f"強制止損 ({STOP_LOSS_PCT:.1%})"),
        ("MIN_ORDER_VALUE",       f"{MIN_ORDER_VALUE:,}",            "最小下單金額（元）"),
        ("SENTIMENT_ENABLED",     str(SENTIMENT_ENABLED),            "AI 新聞情緒分析開關"),
        ("SKIP_MARKET_FILTER",    str(SKIP_MARKET_FILTER),           "True=跳過大盤過濾（模擬戶測試用）"),
        ("PENDING_ORDER_TIMEOUT", f"{PENDING_ORDER_TIMEOUT} 秒",     f"委託逾時自動撤單（{PENDING_ORDER_TIMEOUT//60} 分鐘）"),
    ]
    print("[Debug] ── 實際生效值 ──────────────────────────────")
    for name, val, desc in actual_values:
        print(f"  {name:<22}: {val:<32} # {desc}")

    # SECRET_KEY 額外診斷
    sk = os.environ.get("SECRET_KEY", "").strip()
    if sk:
        print("[Debug] ── SECRET_KEY 診斷 ─────────────────────────")
        try:
            decoded = base64.b64decode(sk + "==")
            print(f"  base64解碼長度  : {len(decoded)} bytes (Shioaji 需要 32)")
            if len(decoded) != 32:
                print(f"  建議            : 重新從永豐金 API 管理頁複製正確的 SECRET_KEY")
        except Exception as ex:
            print(f"  base64解碼失敗  : {ex}")
        has_newline = "\n" in sk or "\r" in sk
        non_b64 = [c for c in sk if c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="]
        print(f"  含換行符號      : {has_newline}")
        print(f"  非base64字元    : {non_b64 if non_b64 else '無'}")

    # CA 憑證檔案檢查
    ca_path = os.environ.get("CA_CERT_PATH", "").strip()
    if ca_path:
        import pathlib
        p = pathlib.Path(ca_path)
        exists = p.exists()
        size   = p.stat().st_size if exists else 0
        print(f"[Debug] CA憑證     : {ca_path}  存在={exists}  大小={size} bytes")

    print("[Debug] ─────────────────────────────────────────────")


class AITradingBot:
    def __init__(self):
        _debug_env()

        # 交易模式由環境變數 SIMULATION 控制（True=模擬 / False=正式）
        # 安全預設：未設定環境變數時為 True（模擬），避免任何意外動用真實資金
        # 切換正式交易方式：
        #   - 本機：編輯 .env，加入 SIMULATION=false
        #   - GitHub Actions：在 Repository Secrets 設定 SIMULATION=false
        self._simulation = _parse_simulation_env()

        if self._simulation:
            print("[初始化] 🟢 交易模式：模擬交易（simulation=True，不動用真實資金）")
        else:
            print("[初始化] " + "=" * 60)
            print("[初始化] 🔴 交易模式：正式交易（simulation=False，動用真實資金！）")
            print("[初始化] " + "=" * 60)

        self.api = sj.Shioaji(simulation=self._simulation)
        print("[初始化] Shioaji 實例建立完成")

        # 清除環境變數中可能夾帶的空白、換行（GitHub Actions Secrets 常見問題）
        api_key    = os.environ["API_KEY"].strip()
        secret_key = os.environ["SECRET_KEY"].strip()

        print(f"[初始化] 嘗試登入（API_KEY 長度={len(api_key)}，SECRET_KEY 長度={len(secret_key)}）")
        # login(fetch_contract=True, contracts_timeout=N) 一次完成登入 + 合約下載
        # contracts_timeout 讓 login 阻塞等待合約完整載入，避免後續打 fetch_contracts 觸發
        # APISUB/V1/SYS/CONTRACT concurrent 衝突。timeout=0 為非阻塞，這裡用 30 秒等候。
        # 從 upstream sync (yinyaoqing/AI_trade)：解決 GitHub Actions 雲端 runner 跑
        # fetch_contracts 100% 失敗（"exclusive access lost (concurrent API call started)"）的問題。
        accounts = self.api.login(
            api_key=api_key,
            secret_key=secret_key,
            fetch_contract=True,
            contracts_timeout=30000,
            contracts_cb=lambda security_type: print(
                f"[初始化][contracts_cb] {security_type} 載入完成"
            ),
        )
        print(f"[初始化] 登入回應：{accounts}")

        # 檢查合約是否已透過 login 載入（避免重複下載觸發 concurrent 衝突）
        # 用 PINNED_STOCKS 抽樣驗證合約是否可用（ContractCategory 不支援 iter/len）
        contracts_loaded = False
        # 環境診斷
        try:
            print(f"[初始化][診斷] Shioaji 版本: {getattr(sj, '__version__', 'unknown')}")
        except Exception as e:
            print(f"[初始化][診斷] 取得 Shioaji 版本失敗: {e}")
        try:
            status = getattr(self.api.Contracts, "status", "unknown")
            print(f"[初始化][診斷] Contracts.status = {status}")
            if str(status).endswith("Fetched"):
                contracts_loaded = True
        except Exception as e:
            print(f"[初始化][診斷] 取得 Contracts.status 失敗: {e}")

        # 抽樣驗證：嘗試從 PINNED_STOCKS 取得前 5 檔合約，確認可正常存取
        sample_codes = list(PINNED_STOCKS)[:5]
        sample_ok = 0
        for code in sample_codes:
            try:
                c = self.api.Contracts.Stocks[code]
                if c is not None:
                    sample_ok += 1
            except Exception as e:
                print(f"[初始化][診斷] 取得 {code} 失敗: {type(e).__name__}: {e}")
        print(f"[初始化][診斷] 抽樣驗證：{sample_ok}/{len(sample_codes)} 檔可正常取得")
        if sample_ok >= 3:
            contracts_loaded = True
            print(f"[初始化] ✅ 合約抽樣驗證通過")
        else:
            print(f"[初始化] ⚠️ 合約抽樣未通過（{sample_ok}/{len(sample_codes)}）")

        # login 已自動下載合約，不再呼叫 fetch_contracts（會引發 concurrent 衝突）
        # 若抽樣未通過，多等幾秒讓 contracts_cb 完成，再驗證一次
        if not contracts_loaded:
            for wait_sec in (5, 10, 20):
                print(f"[初始化] 等 {wait_sec}s 後重新抽樣驗證...")
                time.sleep(wait_sec)
                sample_ok2 = sum(
                    1 for code in sample_codes
                    if self._safe_get_contract(code) is not None
                )
                print(f"[初始化][診斷] 再次抽樣：{sample_ok2}/{len(sample_codes)} 檔")
                if sample_ok2 >= 3:
                    contracts_loaded = True
                    break

        if not contracts_loaded:
            err_msg = (
                f"❌ Shioaji 合約載入失敗\n"
                f"login(contracts_timeout=30000) 後 PINNED_STOCKS 抽樣仍無法取得合約\n"
                f"Shioaji 版本：{getattr(sj, '__version__', 'unknown')}\n"
                f"建議：稍後重試，或檢查 Shioaji 版本相容性"
            )
            print(f"[初始化] {err_msg}")
            try:
                send_notify(err_msg)
            except Exception:
                pass
            # 失敗前主動 logout，避免後端殘留 session 阻塞下次啟動。
            try:
                print("[初始化] 主動 logout 釋放後端 session...")
                self.api.logout()
                print("[初始化] logout 成功，session 已釋放")
            except Exception as logout_err:
                print(f"[初始化] logout 失敗（忽略）: {type(logout_err).__name__}: {logout_err}")
            raise RuntimeError(err_msg)

        ca_path = os.environ["CA_CERT_PATH"].strip()
        ca_pass = os.environ["CA_PASSWORD"].strip()
        print(f"[初始化] 啟用 CA 憑證：{ca_path}")
        self.api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)
        print("[初始化] CA 憑證啟用成功")

        # 找證券帳戶當預設（bot 只交易股票/零股）。
        # 原作者寫死 accounts[1] 假設「[0]=期貨、[1]=證券」，
        # 但純證券戶只有 1 個帳戶會 IndexError。改用 type 名稱過濾，
        # 沒申請期貨帳戶也能正常運作。Shioaji 1.3.2 沒有 sj.constant.AccountType，
        # 所以用 type(a).__name__ 比對最穩。
        stock_acc = next(
            (a for a in accounts if type(a).__name__ == "StockAccount"),
            accounts[0],
        )
        self.api.set_default_account(stock_acc)
        print(f"[初始化] 預設帳戶：{stock_acc}")
        print(f"[初始化] 所有帳戶：{[str(a.account_id) for a in accounts]}")

        self.positions: dict[str, Position] = {}
        self.watch_list: list[str] = list(PINNED_STOCKS)
        self._sentiment_scores: deque[float] = deque(maxlen=SENTIMENT_SMOOTH_N)  # 1.1 情緒平滑
        self.allocator = StrategyAllocator(self.api)                             # 3.2 多策略分配器
        self._last_regime: str = ""              # 策略配置上次推播的 regime，相同則不重複推播
        self._market_trend_up: bool = False      # 大盤趨勢狀態（含遲滯帶）
        self.funnel = FunnelScanner(self.api, get_ai_sentiment)                  # 漏斗掃描器
        self._funnel_done_today: bool = False    # 當日是否已執行漏斗掃描
        # 賣出冷卻：key=stock_code, value=可重新買入的最早時間（epoch）
        # 一般冷卻 30 分鐘；若為跳空止損則延長至次日 09:30
        self._sell_cooldown: dict[str, float] = {}

        # 零股即時報價快取：key=股票代碼, value=(bid, ask)
        # 由 _subscribe_odd_quotes() 持續更新，check_slippage_safe() 優先使用
        self._odd_quotes: dict[str, tuple[float, float]] = {}

        # 委託追蹤：key=stock_code, value={"action","price","qty","amount","trade_obj"}
        # 用於凍結已委託但未成交的資金 / 部位
        self._pending_orders: dict[str, dict] = {}
        # 賣單部位備份：若賣單取消可恢復
        self._pending_sell_positions: dict[str, "Position"] = {}
        # 即時成交累積（callback 寫入）：key=ordno, value=[deals]
        self._deal_buffer: dict[str, list] = {}
        # 違約交割警告冷卻：上次推播時間
        self._last_critical_alert: float = 0.0
        # 異常通知節流：key=alert_key, value=last_sent_ts。同 key 訊息預設 30 分鐘冷卻
        self._anomaly_alerts: dict[str, float] = {}
        # 大盤健康監控：check_market_trend() 上次成功的時間
        # 主迴圈會檢查 stale time，超過閾值推 Telegram 警示
        self._last_market_check_success: float = 0.0
        # 日 K 快取：key=code, value=(timestamp, df)。24 小時內不重抓
        # Shioaji 模擬戶 kbars() 受限，需要 yfinance fallback；快取避免 rate limit
        self._kbar_cache: dict[str, tuple[float, "pd.DataFrame"]] = {}

        # 註冊 Shioaji 委託 / 成交即時回呼
        self._register_order_callback()

        # 查詢帳戶餘額，動態決定實際可用預算
        self._init_budget()

        # 訂閱 PINNED_STOCKS BidAsk 報價（供零股滑點判斷使用）
        self._subscribe_odd_quotes()

        # 啟動時同步實際持倉
        self._sync_positions_from_api()

    # ------------------------------------------------------------------
    # 漏斗掃描：每日 09:20 執行一次，結果合併至 watch_list
    # ------------------------------------------------------------------
    def run_funnel_if_needed(self) -> None:
        """
        在主循環中呼叫。條件：
          1. 現在時間 ≥ FUNNEL_SCAN_HOUR:FUNNEL_SCAN_MINUTE
          2. 今日尚未執行過
        執行後將精選標的加入 watch_list（PINNED_STOCKS 永遠保留）。
        跨日（日期變更）自動重置旗標。
        """
        now = now_tw()

        # 跨日重置
        if hasattr(self, "_funnel_last_date") and self._funnel_last_date != now.date():
            self._funnel_done_today = False
            self.watch_list = list(PINNED_STOCKS)   # 重置為基本清單
        self._funnel_last_date = now.date()

        if self._funnel_done_today:
            return
        if now.hour < FUNNEL_SCAN_HOUR or (
            now.hour == FUNNEL_SCAN_HOUR and now.minute < FUNNEL_SCAN_MINUTE
        ):
            remaining = (FUNNEL_SCAN_HOUR * 60 + FUNNEL_SCAN_MINUTE) - (now.hour * 60 + now.minute)
            print(f"[漏斗] 尚未到 {FUNNEL_SCAN_HOUR:02d}:{FUNNEL_SCAN_MINUTE:02d}，剩約 {remaining} 分鐘。")
            return

        print(f"[漏斗] {now.strftime('%H:%M')} 觸發每日漏斗掃描...")
        try:
            results = self.funnel.run(max_results=FUNNEL_MAX_RESULTS)
        except Exception as e:
            print(f"[漏斗] 掃描失敗: {e}")
            self._funnel_done_today = True
            return

        self._funnel_done_today = True

        # 合併：PINNED_STOCKS 優先，漏斗結果補充（不重複）
        pinned = list(PINNED_STOCKS)
        funnel_codes = [r.code for r in results if r.code not in pinned]
        self.watch_list = pinned + funnel_codes

        if funnel_codes:
            codes_str = "、".join(funnel_codes)
            print(f"[漏斗] 精選 {len(funnel_codes)} 檔新增至監控：{codes_str}")
            send_notify(
                f"[漏斗掃描完成] {now.strftime('%H:%M')}\n"
                f"精選 {len(results)} 檔（新增 {len(funnel_codes)} 檔）：\n"
                + "\n".join(f"  {r}" for r in results)
                + f"\n當日監控清單：{self.watch_list}"
            )
        else:
            print("[漏斗] 本日無新增標的（已全在 PINNED_STOCKS 內或未通過篩選）。")

    # ------------------------------------------------------------------
    # 委託 / 成交即時回呼：替代輪詢，秒級更新 _pending_orders
    # ------------------------------------------------------------------
    def _register_order_callback(self) -> None:
        """
        註冊 Shioaji 的 set_order_callback。每當委託狀態變化（送出/部分成交/全部成交/取消）
        交易所即推送一筆事件，無需輪詢 list_trades。
        """
        def _on_order_event(stat, msg):
            try:
                stat_str = str(stat)
                # OrderState.StockDeal → 實際成交
                if "Deal" in stat_str:
                    code   = msg.get("code", "")
                    action = msg.get("action", "")
                    price  = float(msg.get("price", 0))
                    qty    = int(msg.get("quantity", 0))
                    ordno  = msg.get("ordno", "")
                    print(f"[即時成交] {action} {code} {qty}股 @ {price}  ordno={ordno}")

                    # 累積該訂單所有成交細節，計算加權均價與已成交數量
                    self._deal_buffer.setdefault(ordno, []).append(
                        {"price": price, "qty": qty}
                    )
                    total_qty = sum(d["qty"] for d in self._deal_buffer[ordno])
                    total_amt = sum(d["price"] * d["qty"] for d in self._deal_buffer[ordno])
                    avg_price = total_amt / total_qty if total_qty > 0 else price

                    pending = self._pending_orders.get(code)
                    is_bot_order = pending is not None
                    fully_filled = pending and total_qty >= pending["qty"]

                    if fully_filled:
                        print(f"[即時成交] {code} 全部成交（{total_qty}/{pending['qty']}）均價 {avg_price:.2f}")
                        if pending["action"] == "Buy" and code in self.positions:
                            pos = self.positions[code]
                            pos.entry_price = avg_price
                            pos.qty = total_qty
                            pos.stop_price  = avg_price * (1 - STOP_LOSS_PCT)
                            pos.trail_price = avg_price * (1 + TRAILING_START)
                            if pos.atr > 0:
                                pos.stop_price = max(avg_price - 1.5 * pos.atr, pos.stop_price)
                            send_notify(
                                f"[✅ 買進成交] {stock_label(code)}\n"
                                f"成交：{total_qty}股 @ {avg_price:.2f}（總額 {total_qty * avg_price:,.0f}元）\n"
                                f"止損：{pos.stop_price:.2f}  止盈啟動：{pos.trail_price:.2f}"
                            )
                        elif pending["action"] == "Sell":
                            send_notify(
                                f"[✅ 賣出成交] {stock_label(code)}\n"
                                f"成交：{total_qty}股 @ {avg_price:.2f}（總額 {total_qty * avg_price:,.0f}元）"
                            )
                        self._pending_orders.pop(code, None)
                        self._pending_sell_positions.pop(code, None)
                        self._deal_buffer.pop(ordno, None)

                    # 處理外部來源（手機 APP / 網頁 / 其他途徑）的成交
                    if not is_bot_order:
                        if "Buy" in action and code not in self.positions:
                            # 手機手動買入 → 即時補入部位
                            pos = Position(code=code, entry_price=price, qty=qty)
                            pos.entry_time = now_tw()  # 視為今日新建，T+1 保護生效
                            pos.stop_price  = price * (1 - STOP_LOSS_PCT)
                            pos.trail_price = price * (1 + TRAILING_START)
                            self.positions[code] = pos
                            print(f"[即時成交] 外部買入 {code} {qty}股 @ {price}，已加入監控")
                            send_notify(f"[外部買入偵測] {code} {qty}股 @ {price}，bot 已加入監控")
                        elif "Sell" in action and code in self.positions:
                            # 手機手動賣出 → 即時清除部位（避免 bot 繼續監控不存在的股票）
                            pos = self.positions.pop(code)
                            print(f"[即時成交] 外部賣出 {code} {qty}股 @ {price}，已從監控移除")
                            send_notify(
                                f"[外部賣出偵測] {code} {qty}股 @ {price}\n"
                                f"成本 {pos.entry_price:.2f}  bot 已停止監控"
                            )

                # OrderState.StockOrder → 狀態變化（New / Cancel / UpdatePrice / UpdateQty）
                elif "Order" in stat_str:
                    op       = msg.get("operation") if isinstance(msg.get("operation"), dict) else {}
                    op_type  = str(op.get("op_type", ""))
                    op_msg   = op.get("op_msg", "")
                    status   = msg.get("status") if isinstance(msg.get("status"), dict) else {}
                    status_s = str(status.get("status", ""))
                    contract = msg.get("contract") if isinstance(msg.get("contract"), dict) else {}
                    code     = contract.get("code", "")
                    order    = msg.get("order") if isinstance(msg.get("order"), dict) else {}
                    new_price = float(order.get("price", 0) or 0)
                    new_qty   = int(order.get("quantity", 0) or 0)

                    if "Cancel" in op_type or "Cancelled" in status_s:
                        if not code:
                            return
                        print(f"[即時委託] {code} 已取消  {op_msg}")
                        info = self._pending_orders.pop(code, None)
                        # 統一推播取消通知（含金額、原因）
                        if info:
                            act_zh = "買單" if info["action"] == "Buy" else "賣單"
                            reason = f"  原因：{op_msg}" if op_msg else ""
                            notify_lines = [
                                f"[委託取消] ⚠️ {code} {act_zh}已取消",
                                f"委託：{info['qty']} 股 @ {info['price']}  金額 {info['amount']:,.0f} 元",
                            ]
                            if info["action"] == "Sell":
                                backup = self._pending_sell_positions.pop(code, None)
                                if backup:
                                    self.positions[code] = backup
                                    notify_lines.append("→ 部位已恢復監控")
                            elif info["action"] == "Buy" and code in self.positions:
                                del self.positions[code]
                                notify_lines.append("→ 部位記錄已移除")
                            if reason:
                                notify_lines.append(reason.strip())
                            send_notify("\n".join(notify_lines))
                        else:
                            # 非 bot 下的委託（手機 APP 直接下單後取消）
                            send_notify(f"[委託取消] ⚠️ {code} 委託已取消（外部來源）{op_msg}")

                    elif "UpdatePrice" in op_type or "ChangePrice" in op_type:
                        info = self._pending_orders.get(code)
                        if info and new_price > 0:
                            old_price = info["price"]
                            info["price"]  = new_price
                            info["amount"] = new_price * info["qty"]
                            print(
                                f"[即時委託] {code} 改價 {old_price:.2f} → {new_price:.2f}"
                                f"  新凍結 {info['amount']:,.0f} 元"
                            )
                            send_notify(
                                f"[委託改價] {code}\n"
                                f"{old_price:.2f} → {new_price:.2f}  ({info['action']} {info['qty']}股)"
                            )

                    elif "UpdateQty" in op_type or "ChangeQty" in op_type:
                        info = self._pending_orders.get(code)
                        if info and new_qty > 0:
                            old_qty = info["qty"]
                            info["qty"]    = new_qty
                            info["amount"] = info["price"] * new_qty
                            print(
                                f"[即時委託] {code} 改量 {old_qty} → {new_qty} 股"
                                f"  新凍結 {info['amount']:,.0f} 元"
                            )
                            send_notify(
                                f"[委託改量] {code}\n"
                                f"{old_qty} → {new_qty} 股  ({info['action']} @ {info['price']})"
                            )
            except Exception as e:
                print(f"[訂單回呼] 處理失敗: {e}")

        try:
            self.api.set_order_callback(_on_order_event)
            print("[初始化] 訂單即時回呼已註冊")
        except Exception as e:
            print(f"[初始化] 訂單回呼註冊失敗: {e}")

    def _safe_get_contract(self, code: str):
        """安全取得合約（多種 Shioaji 版本介面相容）。
        從 upstream sync (yinyaoqing/AI_trade)。
        """
        try:
            return self.api.Contracts.Stocks[code]
        except Exception:
            pass
        try:
            return self.api.Contracts.Stocks.get(code)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 零股報價訂閱：訂閱 PINNED_STOCKS BidAsk，快取最新買賣報價
    # Shioaji 無獨立零股 snapshot API；即時報價需透過 quote.subscribe()
    # ------------------------------------------------------------------
    def _subscribe_odd_quotes(self) -> None:
        """訂閱所有 PINNED_STOCKS 的 BidAsk 即時報價（盤中零股滑點保護用）"""

        @self.api.on_bidask_stk_v1()
        def _on_bidask(_exchange, bidask) -> None:
            """接收 BidAsk 推播並更新快取（bid_price[0] / ask_price[0] 為最優報價）"""
            code = getattr(bidask, "code", None)
            if code is None:
                return
            try:
                bid = float(bidask.bid_price[0]) if bidask.bid_price else 0.0
                ask = float(bidask.ask_price[0]) if bidask.ask_price else 0.0
                if bid > 0 and ask > 0:
                    self._odd_quotes[code] = (bid, ask)
            except Exception:
                pass

        subscribed, failed = 0, 0
        for code in PINNED_STOCKS:
            try:
                contract = self.api.Contracts.Stocks[code]
                self.api.quote.subscribe(
                    contract,
                    quote_type=sj.constant.QuoteType.BidAsk,
                    version=sj.constant.QuoteVersion.v1,
                )
                subscribed += 1
            except Exception as e:
                print(f"[報價訂閱] {code} 失敗: {e}")
                failed += 1
        print(f"[報價訂閱] BidAsk 訂閱完成：成功 {subscribed} 檔，失敗 {failed} 檔")

    def _init_budget(self, notify: bool = False) -> None:
        """
        以「安全可動用現金」作為 TOTAL_BUDGET：
          安全可動用現金 = 交割款餘額 + 未交割淨額（應收 - 應付）
        模擬帳戶不支援 account_balance，回傳 0 時沿用頂部設定值。
        notify=True 時將結果推播至 Telegram（定期更新時使用）。
        """
        global TOTAL_BUDGET, POSITION_SIZE, RISK_PER_TRADE
        try:
            bal = self.api.account_balance()
            acc_balance = float(bal.acc_balance)
            if acc_balance <= 0:
                # 模擬帳戶不支援 account_balance，回傳 0，沿用設定值
                print(f"[預算] 帳戶餘額回傳 0（模擬帳戶限制），沿用設定值 {TOTAL_BUDGET:,} 元")
                return

            # 嘗試取得券商提供的交易上限（Shioaji 1.3+ 新 API）
            # 若可用即作為硬上限參考，券商的計算包含 T+1/T+2 應收應付 + 信用 + 手續費等
            # 從 upstream sync (yinyaoqing/AI_trade)：三層預算保護的第三層
            broker_buy_limit = None
            try:
                limits = self.api.trading_limits()
                broker_buy_limit = float(getattr(limits, "limit_buy", 0) or 0)
                if broker_buy_limit > 0:
                    print(f"[預算] 券商交易上限 trading_limits().limit_buy = {broker_buy_limit:,.0f} 元")
            except Exception as e:
                # 1.3.3 或更舊版本可能無此 API，靜默忽略
                if "object has no attribute" not in str(e):
                    print(f"[預算] trading_limits 查詢失敗（忽略）: {type(e).__name__}: {e}")

            # 查詢未交割淨額
            # 規則：s_date <= today（T+0）的應收/應付皆已反映在 acc_balance，不重複計算
            #        s_date >  today 的才計入
            net_settlement = 0.0
            payable = receivable = 0.0
            settlement_lines: list[str] = []
            try:
                settlements = self.api.settlements(self.api.stock_account)
                if settlements:
                    today_dt = now_tw().date()
                    for s in settlements:
                        s_date = s.date if hasattr(s.date, "year") else s.date
                        if s_date <= today_dt:
                            # 今日（含）以前：已計入交割款餘額
                            label = "應付" if s.amount < 0 else "應收"
                            settlement_lines.append(
                                f"  {s.date} T+{s.T} {label} {s.amount:+,.0f} 元（已計入餘額）"
                            )
                        elif s.amount < 0:
                            payable += s.amount
                            settlement_lines.append(
                                f"  {s.date} T+{s.T} 應付 {s.amount:+,.0f} 元"
                            )
                        else:
                            receivable += s.amount
                            settlement_lines.append(
                                f"  {s.date} T+{s.T} 應收 {s.amount:+,.0f} 元"
                            )
                    net_settlement = payable + receivable
                    print(
                        f"[預算] 未交割：應付（未扣）{payable:,.0f} 元  "
                        f"應收（未入帳）{receivable:+,.0f} 元  淨額 {net_settlement:+,.0f} 元"
                    )
            except Exception as e:
                print(f"[預算] 未交割查詢失敗，以 0 計算：{e}")

            # 扣除委託中但未成交的買單凍結金額
            frozen = self.pending_buy_amount()
            available = acc_balance + net_settlement - frozen

            # 暴露給 scan_candidates 作嚴格現金檢查使用
            self._acc_balance_cache    = acc_balance
            self._future_payable_cache = payable      # 未來應付（負值）
            self._future_recv_cache    = receivable   # 未來應收（正值）
            self._broker_buy_limit     = broker_buy_limit  # 券商交易上限（可能為 None）

            TOTAL_BUDGET   = available
            POSITION_SIZE  = _calc_position_size(TOTAL_BUDGET)
            RISK_PER_TRADE = TOTAL_BUDGET * STOP_LOSS_PCT
            print(
                f"[預算] 交割款餘額：{acc_balance:,.0f} 元  淨額：{net_settlement:+,.0f} 元"
                + (f"  凍結：{frozen:,.0f} 元" if frozen else "") +
                f"\n  → 安全可動用現金 TOTAL_BUDGET={TOTAL_BUDGET:,.0f}  "
                f"POSITION_SIZE={POSITION_SIZE:,}  "
                f"RISK_PER_TRADE={RISK_PER_TRADE:,.0f}"
            )

            # 安全警告：單筆預算低於最小下單金額時無法進場
            warn_msg = ""
            critical_alert = ""
            if TOTAL_BUDGET < 0:
                # 預算為負 → 違約交割風險，30 分鐘冷卻內不重複推播
                CRITICAL_COOLDOWN = 1800
                shortfall = -TOTAL_BUDGET
                critical_alert = (
                    f"🚨【違約交割風險】🚨\n"
                    f"安全可動用現金為負值：{TOTAL_BUDGET:,.0f} 元\n"
                    f"缺口：{shortfall:,.0f} 元\n"
                    f"帳戶餘額 {acc_balance:,.0f}，未來應付 {net_settlement:+,.0f}\n"
                    f"⚠️ 請立即執行以下任一動作：\n"
                    f"  1. 匯款補足至少 {shortfall + 2000:,.0f} 元（含 2,000 緩衝）\n"
                    f"  2. 手動賣出獲利部位變現\n"
                    f"  3. 確認是否有應收款項即將入帳"
                )
                print(f"[預算] {critical_alert}")
                if time.time() - self._last_critical_alert >= CRITICAL_COOLDOWN:
                    send_notify(critical_alert)
                    self._last_critical_alert = time.time()
                else:
                    remain = CRITICAL_COOLDOWN - (time.time() - self._last_critical_alert)
                    print(f"[預算] 違約警告冷卻中（剩 {remain:.0f}s），跳過推播")
            elif POSITION_SIZE < MIN_ORDER_VALUE:
                warn_msg = (
                    f"\n⚠️ 單筆預算 {POSITION_SIZE:,} 元 < 最低下單 {MIN_ORDER_VALUE:,} 元，無法進場。"
                    f"\n建議帳戶至少 {MIN_ORDER_VALUE * MAX_POSITIONS:,} 元。"
                )
                print(f"[預算] {warn_msg}")

            if notify:
                frozen_line = f"委託凍結：{frozen:,.0f} 元\n" if frozen else ""
                msg = (
                    f"[預算更新] {now_tw().strftime('%H:%M:%S')}\n"
                    f"交割款餘額：{acc_balance:,.0f} 元\n"
                    f"未交割明細：\n" + ("\n".join(settlement_lines) if settlement_lines else "  （無待交割）") + "\n"
                    f"應付：{payable:,.0f} 元  應收：{receivable:+,.0f} 元  淨額：{net_settlement:+,.0f} 元\n"
                    + frozen_line +
                    f"─────────────────────\n"
                    f"安全可動用現金：{TOTAL_BUDGET:,.0f} 元\n"
                    f"單筆上限：{POSITION_SIZE:,} 元"
                    + warn_msg
                )
                send_notify(msg)

        except Exception as e:
            print(f"[預算] 查詢餘額失敗，沿用設定值 {TOTAL_BUDGET:,} 元：{e}")

    # ------------------------------------------------------------------
    # 持倉同步：將 API 實際持倉載入 self.positions
    # ------------------------------------------------------------------
    def _sync_positions_from_api(self) -> None:
        """查詢券商實際持倉，載入 self.positions，避免重啟後遺漏持股"""
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
            if not held:
                print("[持倉] 目前無持股")
                return

            # 查詢今日買入紀錄，用於精確判斷 T+1
            today_buys: set[str] = set()
            try:
                try:
                    trades = self.api.list_trades(self.api.stock_account)
                except TypeError:
                    trades = self.api.list_trades()
                today_str = now_tw().strftime("%Y-%m-%d")
                for t in (trades or []):
                    action = str(getattr(getattr(t, "order", None), "action", ""))
                    code   = getattr(getattr(t, "contract", None), "code", "")
                    ts     = str(getattr(getattr(t, "status", None), "order_datetime", ""))
                    if "Buy" in action and ts.startswith(today_str):
                        today_buys.add(code)
            except Exception as e:
                print(f"[持倉] list_trades 查詢失敗（T+1 判斷可能不準）: {e}")

            print(f"[持倉] 查詢到 {len(held)} 筆持股，同步中...")
            if today_buys:
                print(f"[持倉] 今日買入：{today_buys}（T+1 不可當日賣出）")

            for p in held:
                code = p.code
                if code in self.positions:
                    continue  # 已有紀錄，不覆蓋
                avg_price = getattr(p, "price", None) or getattr(p, "average_price", 0)
                qty       = getattr(p, "quantity", 0)
                pos = Position(
                    code=code,
                    entry_price=float(avg_price),
                    qty=int(qty),
                )

                # 查詢 position_detail 取得實際買入日期
                entry_date = None
                try:
                    pid = getattr(p, "id", None)
                    if pid is not None:
                        details = self.api.list_position_detail(self.api.stock_account, pid)
                        dates = [getattr(d, "date", None) for d in (details or [])]
                        dates = [d for d in dates if d]
                        if dates:
                            earliest = min(dates)  # 取最早一筆作為實際進場日
                            if isinstance(earliest, str):
                                entry_date = datetime.strptime(earliest[:10], "%Y-%m-%d").replace(tzinfo=TZ_TW)
                            elif hasattr(earliest, "year"):
                                entry_date = datetime(earliest.year, earliest.month, earliest.day, tzinfo=TZ_TW)
                except Exception as e:
                    print(f"[持倉] {code} list_position_detail 失敗: {e}")

                if entry_date is not None:
                    pos.entry_time = entry_date
                    t1_label = f"（進場日 {entry_date.strftime('%Y-%m-%d')}）"
                elif code in today_buys:
                    t1_label = "（今日買入，T+1）"
                else:
                    pos.entry_time = now_tw() - timedelta(days=1)
                    t1_label = "（日期未知）"

                self.positions[code] = pos
                last  = float(getattr(p, "last_price", avg_price) or avg_price)
                pnl   = (last - float(avg_price)) * int(qty)
                print(
                    f"  {code}  均價={avg_price}  持股={qty}股  "
                    f"現值≈{last}  損益={pnl:+.0f}元 {t1_label}"
                )
        except Exception as e:
            print(f"[持倉] 查詢失敗: {e}")

    def format_watch_list(self, codes: list[str] | None = None) -> str:
        """
        把監控代號 list 轉成「代號 名稱、代號 名稱、...」單行格式，供 print / Telegram 使用。
        從 self.api.Contracts.Stocks 查名稱（CA 啟用後合約已 loaded 進記憶體）。
        查不到名稱顯示「?」，不會 raise。
        """
        codes = codes if codes is not None else self.watch_list
        items = []
        for code in codes:
            try:
                name = self.api.Contracts.Stocks[code].name
            except (KeyError, AttributeError, TypeError):
                name = "?"
            items.append(f"{code} {name}")
        return "、".join(items)

    def get_positions_summary(self) -> str:
        """回傳持倉摘要字串（供啟動通知與定時推播使用）"""
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
        except Exception as e:
            return f"（持倉查詢失敗: {e}）"

        if not held:
            return "目前無持股"

        lines = []
        total_pnl = 0.0
        for p in held:
            code      = p.code
            qty       = int(getattr(p, "quantity", 0))
            avg_price = float(getattr(p, "price", None) or getattr(p, "average_price", 0))
            last      = float(getattr(p, "last_price", avg_price) or avg_price)
            # 自行計算損益，避免 API pnl 欄位單位不一致問題
            pnl = (last - avg_price) * qty
            total_pnl += pnl
            pct = (last - avg_price) / avg_price * 100 if avg_price else 0
            lines.append(
                f"  {code}  {qty}股  均價={avg_price}  現價={last}  "
                f"損益={pnl:+.0f}元 ({pct:+.2f}%)"
            )
        lines.append(f"  合計未實現損益：{total_pnl:+.0f} 元")
        return "\n".join(lines)

    def get_positions_with_exits(self) -> str:
        """
        回傳持倉摘要 + 出場條件（stop / 成本保衛 / 移動止盈門檻）。
        供定期狀態報告使用。
        """
        if not self.positions:
            return "[持倉] 無"

        # 取得現價（snapshot）
        snaps: dict[str, float] = {}
        try:
            contracts = [self.api.Contracts.Stocks[c] for c in self.positions]
            for s in self.api.snapshots(contracts):
                snaps[s.code] = float(s.close)
        except Exception as e:
            print(f"[狀態報告] snapshot 失敗: {e}")

        lines = [f"[持倉] {len(self.positions)} 筆"]
        total_pnl = 0.0
        for code, pos in self.positions.items():
            current = snaps.get(code, pos.entry_price)
            pos.update_max(current)
            pnl     = (current - pos.entry_price) * pos.qty
            profit  = (current - pos.entry_price) / pos.entry_price if pos.entry_price else 0
            total_pnl += pnl

            # 出場條件計算
            breakeven_price = pos.entry_price * (1 + BREAKEVEN_TRIGGER)
            trail_start     = pos.trail_price
            # 動態回吐門檻（同 monitor_exit 邏輯）
            atr_pullback    = TRAILING_ATR_MULT * pos.atr if pos.atr > 0 else 0
            pullback_pct    = max(atr_pullback / pos.max_price if pos.max_price else 0,
                                  TRAILING_PULLBACK)
            trail_exit_price = pos.max_price * (1 - pullback_pct)

            # 當前哪個條件最接近？
            if current <= pos.stop_price:
                state = "🔴 已觸發止損"
            elif pos.stop_price >= pos.entry_price:
                state = "🟢 成本保衛啟動"
            elif profit >= BREAKEVEN_TRIGGER:
                state = "🟡 即將啟動成本保衛"
            elif current >= trail_start:
                state = "🟢 移動止盈監控中"
            else:
                state = "⚪ 等待進入止盈區"

            # 持有時間（以日為單位，不足一天顯示小時）
            held_delta = now_tw() - pos.entry_time
            held_days  = held_delta.days
            held_hours = held_delta.seconds // 3600
            if held_days >= 1:
                held_str = f"{held_days}天"
            else:
                held_mins = (held_delta.seconds % 3600) // 60
                held_str  = f"{held_hours}時{held_mins}分"
            entry_date_str = pos.entry_time.strftime("%Y-%m-%d")

            lines.append(
                f"  {code}  {pos.qty}股  均價={pos.entry_price:.2f}  現價={current:.2f}"
                f"  損益={pnl:+.0f}元 ({profit:+.2%})"
            )
            lines.append(
                f"    狀態：{state}  歷史高={pos.max_price:.2f}"
                f"  進場日={entry_date_str}  持有={held_str}"
            )
            lines.append(
                f"    A.止損={pos.stop_price:.2f}（-{(1-pos.stop_price/pos.entry_price)*100:.2f}%）"
                f"  B.成本保衛觸發={breakeven_price:.2f}"
            )
            lines.append(
                f"    C.止盈啟動={trail_start:.2f}"
                f"  回吐出場={trail_exit_price:.2f}（門檻 {pullback_pct:.2%}）"
            )
            # D. 時間停損
            held_bdays  = _business_days_between(pos.entry_time.date(), now_tw().date())
            remain      = max(TIME_STOP_BDAYS - held_bdays, 0)
            peak_growth = (pos.max_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
            if peak_growth >= TRAILING_START:
                d_note = f"歷史高獲利{peak_growth:+.2%} ≥ {TRAILING_START:.1%}，已避開"
            elif held_bdays >= TIME_STOP_BDAYS:
                d_note = f"🔴 條件達成（將於下輪出場）"
            else:
                d_note = f"歷史高獲利{peak_growth:+.2%}，剩 {remain} 工作天"
            lines.append(
                f"    D.時間停損={TIME_STOP_BDAYS}工作天  已持有 {held_bdays} 工作天  {d_note}"
            )
        lines.append(f"  合計未實現損益：{total_pnl:+.0f} 元")
        return "\n".join(lines)

    def calc_total_pnl(self) -> str:
        """
        計算累計損益：
          總資產 = 帳戶餘額 + 所有未交割金額 + 持倉市值
          累計損益 = 總資產 - INITIAL_CAPITAL

        模擬模式下 account_balance() 與 settlements() 都不支援（Shioaji 限制），
        會回 0 導致「總資產 0 - INITIAL_CAPITAL 68000 = -100%」誤導訊息，
        因此模擬模式直接 early return 顯示「不適用」。
        """
        if self._simulation:
            return (
                "[累計損益]\n"
                "  （模擬模式：account_balance/settlements API 不支援，無法計算）\n"
                "  （切換至正式交易後此欄會顯示真實累計損益）"
            )

        try:
            bal = self.api.account_balance()
            acc_balance = float(bal.acc_balance)
        except Exception:
            return "（餘額查詢失敗，無法計算）"

        # 未交割淨額：s_date <= today 已計入 acc_balance，只算未來的
        net_settlement = 0.0
        try:
            settlements = self.api.settlements(self.api.stock_account)
            if settlements:
                today_dt = now_tw().date()
                net_settlement = sum(
                    s.amount for s in settlements
                    if (s.date if hasattr(s.date, "year") else s.date) > today_dt
                )
        except Exception:
            pass

        # 持倉市值
        position_value = 0.0
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
            for p in (held or []):
                qty  = int(getattr(p, "quantity", 0))
                last = float(getattr(p, "last_price", 0) or 0)
                position_value += qty * last
        except Exception:
            pass

        total_asset = acc_balance + net_settlement + position_value
        pnl = total_asset - INITIAL_CAPITAL
        pnl_pct = pnl / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL else 0

        lines = [
            f"[累計損益]",
            f"  原始資金：{INITIAL_CAPITAL:,.0f} 元",
            f"  帳戶餘額：{acc_balance:,.0f} 元",
            f"  未交割淨額：{net_settlement:+,.0f} 元",
            f"  持倉市值：{position_value:,.0f} 元",
            f"  ────────────────────",
            f"  總資產：{total_asset:,.0f} 元",
            f"  累計損益：{pnl:+,.0f} 元（{pnl_pct:+.2f}%）",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 啟動時檢查昨日遺留委託，回報狀態並處理未成交買單
    # ------------------------------------------------------------------
    def startup_order_check(self) -> str:
        """
        啟動時呼叫：
        1. 查詢 list_trades() 取得所有委託
        2. 篩出未完全成交的委託（Submitted / PartFilled）
        3. 對未成交買單：重新評估是否仍符合條件
        4. 回傳摘要字串供 Telegram 推播
        """
        lines: list[str] = ["[委託狀態檢查]"]
        try:
            try:
                trades = self.api.list_trades(self.api.stock_account)
            except TypeError:
                trades = self.api.list_trades()
        except Exception as e:
            return f"[委託狀態檢查] 查詢失敗: {e}"

        if not trades:
            lines.append("  無任何委託紀錄")
            return "\n".join(lines)

        pending_buys: list = []
        pending_sells: list = []
        filled_count = 0

        for t in (trades or []):
            status_obj = getattr(t, "status", None)
            order_obj  = getattr(t, "order", None)
            status_str = str(getattr(status_obj, "status", ""))
            action_str = str(getattr(order_obj, "action", ""))
            code       = getattr(getattr(t, "contract", None), "code", "?")
            order_qty  = int(getattr(order_obj, "quantity", 0))
            deal_qty   = int(getattr(status_obj, "deal_quantity", 0))

            if "Filled" in status_str:
                filled_count += 1
                continue
            if "Cancelled" in status_str:
                continue

            # 仍在委託中（Submitted / PartFilled 等）
            price = float(getattr(order_obj, "price", 0))
            info = {
                "trade": t, "code": code, "action": action_str,
                "price": price, "order_qty": order_qty,
                "deal_qty": deal_qty, "status": status_str,
            }
            if "Buy" in action_str:
                pending_buys.append(info)
                lines.append(
                    f"  ⏳ 買單 {code} {deal_qty}/{order_qty}股 @ {price}  {status_str}"
                )
            elif "Sell" in action_str:
                pending_sells.append(info)
                lines.append(
                    f"  ⏳ 賣單 {code} {deal_qty}/{order_qty}股 @ {price}  {status_str}"
                )

        if not pending_buys and not pending_sells:
            lines.append(f"  所有委託已成交或取消（已成交 {filled_count} 筆）")
            return "\n".join(lines)

        # ── 未成交買單：重新評估 ──
        if pending_buys:
            lines.append("")
            lines.append("[未成交買單重新評估]")
            for info in pending_buys:
                code = info["code"]
                result = self._reevaluate_pending_buy(info)
                lines.append(f"  {code}: {result}")

        return "\n".join(lines)

    def _reevaluate_pending_buy(self, info: dict) -> str:
        """
        重新評估未成交買單：
        - 仍符合條件 → 保留
        - 不符合條件但有更好標的 → 取消並買入新標的
        - 不符合條件且無更好標的 → 取消，釋放資金
        """
        code      = info["code"]
        trade_obj = info["trade"]

        # 1) 重新評估原標的
        still_valid = False
        try:
            c = self._eval_momentum(code, 1.0)
            if c:
                still_valid = True
        except Exception:
            pass

        if still_valid:
            return "仍符合買入條件，保留委託"

        # 2) 原標的不符合 → 取消委託
        try:
            self.api.cancel_order(trade_obj)
            print(f"[委託重評] {code} 已取消")
        except Exception as e:
            return f"取消失敗: {e}，保留委託"

        # 移除 positions 中的記錄（若有）
        if code in self.positions:
            del self.positions[code]
        self._pending_orders.pop(code, None)

        # 3) 掃描是否有更好標的
        best_candidate = None
        for watch_code in self.watch_list:
            if watch_code in self.positions:
                continue
            try:
                c = self._eval_momentum(watch_code, 1.0)
                if c and (best_candidate is None or c.score > best_candidate.score):
                    best_candidate = c
            except Exception:
                continue

        if best_candidate:
            self._execute_buy(best_candidate, 1.0, "（啟動重評替換）")
            return (
                f"已取消，改買 {best_candidate.code} "
                f"（評分 {best_candidate.score:.3f}，價 {best_candidate.price}）"
            )
        else:
            return "已取消，目前無更好標的"

    # ------------------------------------------------------------------
    # 1.1 情緒平滑：加入新分數並回傳移動平均
    # ------------------------------------------------------------------
    def smooth_sentiment(self, raw: float) -> float:
        self._sentiment_scores.append(raw)
        smoothed = sum(self._sentiment_scores) / len(self._sentiment_scores)
        if len(self._sentiment_scores) > 1:
            print(f"[情緒平滑] 原始={raw:+.2f}  近{len(self._sentiment_scores)}次均值={smoothed:+.2f}")
        return smoothed

    # ------------------------------------------------------------------
    # 盤中 ticks 取得：Shioaji 優先（正式戶有 intraday），模擬戶 fallback 日 K
    # 模擬戶下 api.ticks() 回空 list → ta.vwap() 回 None → .iloc[-1] raise。
    # 用日 K 近似（VWAP/RSI 從日線算，雖然失真但能進場）。
    # ------------------------------------------------------------------
    def _get_intraday_or_daily(self, contract) -> pd.DataFrame:
        """回傳 DataFrame for momentum/mean-reversion signal calculations.
        index = datetime, columns = High/Low/Close/Volume/Open。
        空 DataFrame 表示無法取得（呼叫端應 return None）。
        """
        # Tier 1: Shioaji ticks（正式戶有實時 intraday）
        try:
            ticks = self.api.ticks(contract, date=now_tw().strftime("%Y-%m-%d"))
            df = ticks_to_df(ticks)
            if not df.empty and len(df) >= 5:
                return df
        except Exception as e:
            print(f"[ticks/{contract.code}] 失敗: {type(e).__name__}: {e}")

        # Tier 2: 用日 K 近似（模擬戶 ticks 受限的妥協）
        daily_df = self._get_kbars_safe(contract.code, days=30)
        if daily_df.empty or len(daily_df) < 5:
            return pd.DataFrame()
        daily_df = daily_df.sort_values("ts").reset_index(drop=True).copy()
        daily_df["datetime"] = pd.to_datetime(daily_df["ts"])
        daily_df = daily_df.set_index("datetime")
        return daily_df

    # ------------------------------------------------------------------
    # 日 K 取得：Shioaji 優先（正式戶），失敗 fallback yfinance（模擬戶）
    # 24 小時記憶體快取，避免雲端 cron 每分鐘 68 次 yfinance 觸發 rate limit
    # ------------------------------------------------------------------
    def _get_kbars_safe(self, code: str, days: int = 90) -> pd.DataFrame:
        """取得 code 的日 K DataFrame。欄位：ts / Open / High / Low / Close / Volume / Amount。
        順序：記憶體快取 → Shioaji api.kbars() → yfinance fallback。
        24 小時快取（雲端 cron 一天跑一次足夠）。
        """
        # 快取命中
        cache_entry = self._kbar_cache.get(code)
        if cache_entry is not None:
            ts, df = cache_entry
            if time.time() - ts < 86400 and not df.empty:
                return df

        df = pd.DataFrame()
        # Tier 1: Shioaji
        try:
            contract = self.api.Contracts.Stocks[code]
            end_date   = now_tw().strftime("%Y-%m-%d")
            start_date = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
            kbars = self.api.kbars(contract, start=start_date, end=end_date)
            df = pd.DataFrame(_shioaji_obj_to_dict(kbars))
            if df.empty or "Close" not in df.columns or len(df) < 5:
                df = pd.DataFrame()  # 視為失敗，走 fallback
        except Exception as e:
            print(f"[_get_kbars_safe/{code}] Shioaji 失敗: {type(e).__name__}: {e}")

        # Tier 2: TWSE 官方 STOCK_DAY API（對 GitHub Actions Azure IP 比 yfinance 穩定）
        if df.empty:
            df = _fetch_twse_kbars(code, days=days)
            if not df.empty:
                print(f"[_get_kbars_safe/{code}] TWSE 拿到 {len(df)} 筆")
                self._notify_anomaly(
                    "kbars_external_fallback",
                    f"Shioaji 模擬戶 kbars() 受限，bot 改用 TWSE 公開 API 取日 K（每天抓 1 次快取）。"
                    f"切換正式戶後會優先用 Shioaji，這個訊息會消失。",
                    cooldown_sec=86400,
                )

        # Tier 3: yfinance fallback（本機備援；GH Actions runner 常被擋）
        if df.empty:
            df = _fetch_yfinance_kbars(code, days=days)
            if not df.empty:
                print(f"[_get_kbars_safe/{code}] yfinance 拿到 {len(df)} 筆")
                self._notify_anomaly(
                    "kbars_external_fallback",
                    f"Shioaji 模擬戶 kbars() 受限，bot 改用 yfinance 取日 K（每天抓 1 次快取）。",
                    cooldown_sec=86400,
                )

        # 寫入快取（即使空也快取避免短時間反覆嘗試）
        self._kbar_cache[code] = (time.time(), df)
        return df

    # ------------------------------------------------------------------
    # 異常通知節流器：避免每分鐘失敗都推 Telegram 轟炸用戶
    # ------------------------------------------------------------------
    def _notify_anomaly(self, key: str, msg: str, cooldown_sec: int = 1800) -> None:
        """節流式異常通知。同 key 訊息在 cooldown_sec 秒內只推一次。
        用途：API 失敗、健康監控異常等「非致命但需要關注」的情境。
        預設 30 分鐘冷卻 — 避免 5 處 except 在交易時段每分鐘都推。
        """
        now_ts = time.time()
        last = self._anomaly_alerts.get(key, 0.0)
        if now_ts - last < cooldown_sec:
            return
        print(f"[警示] {msg}")
        try:
            send_notify(f"⚠ [bot 警示]\n{msg}")
        except Exception:
            pass
        self._anomaly_alerts[key] = now_ts

    # ------------------------------------------------------------------
    # 1.2 ATR 動態部位：依個股波動率計算合理股數
    # ------------------------------------------------------------------
    def get_atr_qty(self, contract, current_price: float) -> int:
        """回傳 ATR-based 股數（風險均等化），上限為固定預算所能買到的最大股數"""
        fallback = max(int(POSITION_SIZE / current_price), 1)
        try:
            df = self._get_kbars_safe(contract.code, days=60).sort_values("ts")
            if len(df) < 15:
                return fallback
            atr = ta.atr(df["High"], df["Low"], df["Close"], length=14).iloc[-1]
            if not atr or pd.isna(atr) or atr <= 0:
                return fallback
            qty_by_risk   = int(RISK_PER_TRADE / atr)          # 風險控制上限
            qty_by_budget = int(POSITION_SIZE / current_price)  # 預算上限
            qty = max(min(qty_by_risk, qty_by_budget), 1)
            print(f"[ATR] {contract.code}  ATR={atr:.2f}  風險部位={qty_by_risk}股  預算上限={qty_by_budget}股  → {qty}股")
            return qty
        except Exception as e:
            print(f"[ATR] {contract.code} 計算失敗: {e}，改用預算法")
            # 節流通知：60 分鐘 cooldown（68 檔每分鐘掃描，避免轟炸）
            self._notify_anomaly(
                "atr_helper_fail",
                f"[ATR] 計算失敗（會改用預算法 fallback）: {type(e).__name__}: {e}",
                cooldown_sec=3600,
            )
            return fallback

    # ------------------------------------------------------------------
    # 大盤趨勢過濾
    # ------------------------------------------------------------------
    def check_market_trend(self) -> bool:
        """
        0050 收盤價是否在 20 日均線之上（含遲滯帶防抖動）。
        突破 MA20×1.001 → 轉多；跌破 MA20×0.999 → 轉空。
        介於之間維持上次判斷，避免每分鐘翻轉。
        """
        # SKIP_MARKET_FILTER=true 時直接視為「趨勢向上」
        # 用於模擬戶 — TWSE 日 K 收盤後才更新，盤中拿到的是昨日收盤
        # 0050 vs MA20 差 < 0.5% 時遲滯帶內隨機決定，整天卡在「趨勢偏弱」
        if SKIP_MARKET_FILTER:
            self._market_trend_up = True
            self._last_market_check_success = time.time()
            return True
        try:
            df = self._get_kbars_safe(MARKET_INDEX, days=90)
            if df.empty or len(df) < 20:
                raise RuntimeError(f"kbars for {MARKET_INDEX} 不足 20 筆（Shioaji + yfinance 都拿不到）")
            df = df.set_index("ts").sort_index()
            ma20 = df["Close"].rolling(20).mean().iloc[-1]
            current = df["Close"].iloc[-1]

            HYST = 0.001  # 遲滯帶 0.1%
            if current > ma20 * (1 + HYST):
                self._market_trend_up = True
            elif current < ma20 * (1 - HYST):
                self._market_trend_up = False
            # else: 維持 self._market_trend_up 上次值

            label = "趨勢向上" if self._market_trend_up else "趨勢偏弱"
            print(f"[大盤] 0050={current:.2f}  MA20={ma20:.2f}  {label}")
            self._last_market_check_success = time.time()    # 健康監控用
            return self._market_trend_up
        except Exception as e:
            err = f"[大盤] 取得失敗（連續失敗會跳過所有進場掃描）: {type(e).__name__}: {e}"
            print(err)
            # 推 Telegram 通知（30 分鐘 cooldown）— 之前 5 天 0 進場就是這個靜默吞掉
            self._notify_anomaly("market_check_fail", err, cooldown_sec=1800)
            return False

    # ------------------------------------------------------------------
    # 滑點保護
    # ------------------------------------------------------------------
    def check_slippage_safe(self, contract) -> bool:
        """買賣價差是否在允許範圍內。
        優先使用 BidAsk 即時訂閱快取（零股實際報價），
        若快取尚未到位則退回 snapshots()（整股報價，略有偏差）。
        """
        code = contract.code
        source = "即時BidAsk"
        try:
            cached = self._odd_quotes.get(code)
            if cached:
                bid, ask = cached
            else:
                # 快取尚未建立（剛啟動或訂閱失敗），退回 snapshot
                source = "snapshot(整股)"
                snap = self.api.snapshots([contract])[0]
                bid  = snap.buy_price
                ask  = snap.sell_price

            if bid == 0 or ask == 0:
                print(f"[滑點] {code} 無報價（{source}），跳過。")
                return False
            spread = (ask - bid) / bid
            if spread > SLIPPAGE_LIMIT:
                print(f"[滑點] {code} 價差 {spread:.2%} > {SLIPPAGE_LIMIT:.2%}（{source}），暫緩。")
                return False
            print(f"[滑點] {code} 價差 {spread:.2%} 合格（{source}）。")
            return True
        except Exception as e:
            print(f"[滑點] {code} 檢查失敗: {e}")
            return False

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 進場評估：掃描單一標的，回傳候選或 None（不下單）
    # ------------------------------------------------------------------
    def _eval_momentum(self, stock_code: str, sentiment_score: float) -> "BuyCandidate | None":
        """評估動能策略進場條件（VWAP 突破 + RSI + 法人），不執行下單"""
        if stock_code in self.positions:
            print(f"[{stock_code}] 已持有，跳過。")
            return None
        try:
            contract = self.api.Contracts.Stocks[stock_code]
            if not self.check_slippage_safe(contract):
                return None

            df = self._get_intraday_or_daily(contract)
            if df.empty:
                return None
            vwap_s = ta.vwap(df["High"], df["Low"], df["Close"], df["Volume"])
            if vwap_s is None or vwap_s.empty:
                # 日 K 太短時 vwap 可能算不出來；用 (H+L+C)/3 近似
                vwap = float((df["High"].iloc[-1] + df["Low"].iloc[-1] + df["Close"].iloc[-1]) / 3)
            else:
                vwap = float(vwap_s.iloc[-1])
            current_price = float(df["Close"].iloc[-1])

            # ── RSI 計算 ──────────────────────────────────────────────
            rsi_series = ta.rsi(df["Close"], length=14)
            rsi_val = float(rsi_series.iloc[-1]) if (rsi_series is not None and not rsi_series.empty) else 50.0

            # ── 動態 RSI 門檻（Gemini 建議 2）─────────────────────────
            # RSI 持續上升（近 3 根斜率為正）代表趨勢強勁，放寬至 RSI_OVERBOUGHT_LAX
            rsi_threshold = RSI_OVERBOUGHT
            if RSI_DYNAMIC and rsi_series is not None and len(rsi_series) >= 4:
                rsi_slope = rsi_series.iloc[-1] - rsi_series.iloc[-4]   # 近 3 步的變化
                if rsi_slope > 0:
                    rsi_threshold = RSI_OVERBOUGHT_LAX

            # ── VWAP 乖離率（Gemini 建議 4）──────────────────────────
            vwap_gap = (current_price - vwap) / vwap   # 正值 = 高於 VWAP 多少 %

            print(f"[動能/{stock_code}] 現價={current_price}  VWAP={vwap:.2f}  "
                  f"乖離={vwap_gap:+.2%}  RSI={rsi_val:.1f}(門檻={rsi_threshold})")

            if rsi_val >= rsi_threshold:
                print(f"[動能/{stock_code}] RSI={rsi_val:.1f} ≥ {rsi_threshold}，超買，跳過。")
                return None
            if vwap_gap <= 0:
                print(f"[動能/{stock_code}] 現價未突破 VWAP，跳過。")
                return None
            if vwap_gap > VWAP_MAX_GAP:
                print(f"[動能/{stock_code}] VWAP 乖離 {vwap_gap:.2%} > {VWAP_MAX_GAP:.0%}，過熱追高，跳過。")
                return None

            # ── 法人籌碼 ─────────────────────────────────────────────
            chip_score = chips_sentiment(stock_code)
            print(f"  {chips_summary(stock_code)}  法人分: {chip_score:+.2f}")
            if chip_score < -0.3:
                print(f"[動能/{stock_code}] 法人持續賣超，跳過。")
                return None

            # ── 相對成交量 RVOL（Gemini 建議 1）─────────────────────
            rvol = 1.0
            try:
                # 用日 K 近似 5 分鐘 RVOL（模擬戶 5min kbars 也受限）
                # 既然取得的是日 K，今日 / 平均成交量比較有意義
                # bug 修補：原本 `today_vol = df["Volume"].sum()` 在 df 是 30 天日 K
                #          fallback 時會變成「30 天總和」→ RVOL 數值膨脹（log 看到 RVOL=46~108）
                #          改用 kdf5 最後一筆（最近交易日）當分子，過去 5 日均量當分母
                kdf5 = self._get_kbars_safe(contract.code, days=14).sort_values("ts")
                if len(kdf5) >= 2:
                    # 分母：過去 5 日均量（不含最後一筆，避免分母包含分子）
                    if len(kdf5) >= 6:
                        avg_vol = float(kdf5["Volume"].iloc[-6:-1].mean())
                    else:
                        avg_vol = float(kdf5["Volume"].iloc[:-1].mean())
                    # 分子：最近交易日成交量（盤中通常是昨日收盤，因 TWSE 日 K 收盤後才更新）
                    today_vol = float(kdf5["Volume"].iloc[-1])
                    rvol = today_vol / avg_vol if avg_vol > 0 else 1.0
            except Exception as e:
                # 節流通知：60 分鐘 cooldown。失敗時 rvol=1.0 < RVOL_MIN=1.5 → 該股被擋下，
                # 但 bot 仍會掃其他股票，不像 check_market_trend 失敗那樣致命
                self._notify_anomaly(
                    "rvol_5min_fail",
                    f"[RVOL] kbars 取得失敗（會 fallback 1.0，導致 RVOL 過濾擋下所有股票）: {type(e).__name__}: {e}",
                    cooldown_sec=3600,
                )
            print(f"  RVOL={rvol:.2f}（門檻={RVOL_MIN}）")
            if rvol < RVOL_MIN:
                print(f"[動能/{stock_code}] 量能不足（RVOL={rvol:.2f} < {RVOL_MIN}），跳過。")
                return None

            # ── ATR 動態部位與止損 ────────────────────────────────────
            qty = self.get_atr_qty(contract, current_price)
            if qty < 1:
                return None
            # 最小下單金額：防止手續費侵蝕（零股最低手續費陷阱）
            if qty * current_price < MIN_ORDER_VALUE:
                print(f"[動能/{stock_code}] 下單金額 {qty * current_price:,.0f} 元 < 最低 {MIN_ORDER_VALUE:,} 元，跳過。")
                return None

            atr_val = 0.0
            kdf = pd.DataFrame()
            try:
                kdf = self._get_kbars_safe(contract.code, days=60).sort_values("ts")
                if not kdf.empty and len(kdf) >= 15:
                    atr_s = ta.atr(kdf["High"], kdf["Low"], kdf["Close"], length=14)
                    atr_val = float(atr_s.iloc[-1]) if atr_s is not None and not atr_s.empty else 0.0
            except Exception as e:
                # 節流通知：60 分鐘 cooldown。失敗時 atr_val=0 → ATR_MAX_PCT 過濾不生效，
                # 且 MA50 趨勢過濾因 kdf 為空也失效 → 該股可能繞過部分濾網
                self._notify_anomaly(
                    "daily_kbar_fail",
                    f"[日K] kbars 取得失敗（會繞過 ATR 過熱保護 + MA50 趨勢過濾）: {type(e).__name__}: {e}",
                    cooldown_sec=3600,
                )

            # ── ATR 過熱保護（跳空缺口風險）────────────────────────────
            if atr_val > 0 and (atr_val / current_price) > ATR_MAX_PCT:
                print(f"[動能/{stock_code}] ATR過熱 {atr_val/current_price:.2%} > {ATR_MAX_PCT:.0%}，跳過。")
                return None

            # ── MA50 趨勢過濾（回測驗證：加入後最大回撤從 -43% 降至 -19%）────
            if len(kdf) >= MA_TREND_PERIOD:
                ma50 = kdf["Close"].rolling(MA_TREND_PERIOD).mean().iloc[-1]
                if not pd.isna(ma50) and current_price < ma50:
                    print(f"[動能/{stock_code}] 現價 {current_price} < MA50 {ma50:.1f}，下降趨勢，跳過。")
                    return None

            # 止損：ATR 止損與固定止損取較嚴格者（止損價較高 = 損失較小），防跳空打滑
            # 再以 2.5% 為下限，避免 ATR 過小時止損空間不足 0.15% 造成假止損
            atr_stop_p = current_price - 1.5 * atr_val
            pct_stop_p = current_price * (1 - STOP_LOSS_PCT)
            stop_p  = min(max(atr_stop_p, pct_stop_p), current_price * 0.975)
            trail_p = current_price + max(1.0 * atr_val, current_price * TRAILING_START)

            # ── 綜合排序分：VWAP 突破幅度 40% + 法人情緒 40% + 量能 20%
            chip_norm  = (chip_score + 1) / 2          # -1~1 → 0~1
            rvol_norm  = min(rvol / 3.0, 1.0)          # 0~3x → 0~1（超過 3 倍不繼續加分）
            rank_score = vwap_gap * 0.4 + chip_norm * 0.4 + rvol_norm * 0.2

            return BuyCandidate(
                code=stock_code, strategy="momentum",
                price=current_price, qty=qty,
                vwap=float(vwap), rsi=rsi_val, chip_score=chip_score,
                atr_val=atr_val, stop_price=stop_p, trail_price=trail_p,
                score=rank_score, rvol=float(rvol),
            )
        except Exception as e:
            print(f"[動能/{stock_code}] 評估失敗: {e}")
            return None

    def _eval_mean_reversion(self, stock_code: str, budget: float) -> "BuyCandidate | None":
        """評估均值回歸進場條件（RSI<30 + 現價<VWAP），不執行下單"""
        if stock_code in self.positions:
            return None
        try:
            contract = self.api.Contracts.Stocks[stock_code]
            if not self.check_slippage_safe(contract):
                return None

            df = self._get_intraday_or_daily(contract)
            if df.empty:
                return None
            sig = mean_reversion_signal(df, stock_code)

            print(f"[均值回歸/{stock_code}]  {sig.reason}")
            if sig.action != "BUY":
                return None

            chip_score = chips_sentiment(stock_code)
            if chip_score < -0.5:
                print(f"[均值回歸/{stock_code}] 法人大幅賣超，跳過。")
                return None

            qty = max(int(budget / sig.current_price), 1)
            # 最小下單金額：防止手續費侵蝕（零股最低手續費陷阱）
            # 強烈超賣（RSI<25）且差額不大時，補足股數至門檻以進場
            if qty * sig.current_price < MIN_ORDER_VALUE:
                needed = math.ceil((MIN_ORDER_VALUE - qty * sig.current_price) / sig.current_price)
                extra_cost = needed * sig.current_price
                if sig.rsi < 25 and extra_cost <= sig.current_price * 5:
                    # 最多補 5 股，避免資金失控
                    qty += needed
                    print(f"[均值回歸/{stock_code}] RSI={sig.rsi:.1f} 強烈超賣，補 {needed} 股至 {qty*sig.current_price:,.0f} 元")
                else:
                    print(f"[均值回歸/{stock_code}] 下單金額 {qty * sig.current_price:,.0f} 元 < 最低 {MIN_ORDER_VALUE:,} 元，跳過。")
                    return None
            # 排序分：RSI 低於 30 的距離（越低越強）+ 法人情緒
            rsi_gap   = max(30 - sig.rsi, 0) / 30               # 0~1
            chip_norm = (chip_score + 1) / 2
            rank_score = rsi_gap * 0.5 + chip_norm * 0.5

            return BuyCandidate(
                code=stock_code, strategy="mean_reversion",
                price=sig.current_price, qty=qty,
                vwap=sig.vwap, rsi=sig.rsi, chip_score=chip_score,
                atr_val=0.0,
                stop_price=sig.current_price * (1 - STOP_LOSS_PCT),
                trail_price=sig.current_price * (1 + TRAILING_START),
                score=rank_score,
            )
        except Exception as e:
            print(f"[均值回歸/{stock_code}] 評估失敗: {e}")
            return None

    def _execute_buy(self, c: "BuyCandidate", sentiment_score: float, analysis: str) -> None:
        """對已通過評估的候選標的執行買進下單，並確認成交後更新部位"""
        contract = self.api.Contracts.Stocks[c.code]

        # 疑慮 2：下單前重抓現價，避免評估與下單之間的價格漂移
        # 若漂移超過 0.5%，依方向決定動作：
        #   上漲 → 改用新價追單（避免委託在低價無法成交）
        #   下跌 → 跳過（避免追跌被套）
        try:
            snap_now = self.api.snapshots([contract])[0]
            cur_price = float(getattr(snap_now, "close", c.price) or c.price)
            if cur_price > 0 and c.price > 0:
                drift = (cur_price - c.price) / c.price
                if abs(drift) > 0.005:
                    if drift > 0 and drift <= 0.01:
                        # 小幅上漲（≤1%）：用新價追單
                        print(f"[價格漂移] {c.code} 評估價={c.price} → 現價={cur_price}（{drift:+.2%}），改用現價下單")
                        c.price = cur_price
                        c.qty   = max(int((c.price * c.qty) / cur_price), 1)  # 維持金額一致
                    elif drift > 0.01:
                        # 上漲過多：評估時的訊號可能已失效，跳過
                        print(f"[價格漂移] {c.code} 漲幅 {drift:+.2%} > 1%，訊號失效，跳過")
                        return
                    else:
                        # 下跌：可能在賣壓中，跳過避免接刀
                        print(f"[價格漂移] {c.code} 跌幅 {drift:+.2%}，避免追跌，跳過")
                        return
        except Exception as e:
            print(f"[價格漂移] {c.code} 重抓 snapshot 失敗（沿用原價）: {e}")

        ok = self._place_odd_order(contract, c.price, c.qty, sj.constant.Action.Buy)
        if not ok:
            print(f"[買進] {c.code} 下單被拒，跳過。")
            return

        # 確認成交：以實際成交價格/數量建立部位
        fill = self._confirm_fill(c.code, "Buy")

        # 交易所拒單（status=Failed）→ 撤銷整個下單流程
        # 不寫 trade log、不建立部位、不推誤導的「⏳ 尚未成交」訊息，並清 pending 釋放 slot
        if fill and "Failed" in fill.get("status", ""):
            print(f"[買進] {c.code} 交易所拒單（{fill['status']}），不建立部位")
            self._pending_orders.pop(c.code, None)
            send_notify(
                f"[❌ 買進拒單] {stock_label(c.code)}\n"
                f"委託：{c.qty}股 @ {c.price}\n"
                f"狀態：{fill['status']}（可能因該股無報價/漲跌停/盤後等原因被交易所拒絕）"
            )
            return

        actual_price = fill["deal_price"] if fill and fill["deal_qty"] > 0 else c.price
        actual_qty   = fill["deal_qty"]   if fill and fill["deal_qty"] > 0 else c.qty

        # 以實際成交價重算止損/止盈
        stop_p  = actual_price * (1 - STOP_LOSS_PCT)
        trail_p = actual_price * (1 + TRAILING_START)
        if c.atr_val > 0:
            stop_p = max(actual_price - 1.5 * c.atr_val, stop_p)

        pos = Position(
            code=c.code,
            entry_price=actual_price,
            qty=actual_qty,
            atr=c.atr_val,
            stop_price=stop_p,
            trail_price=trail_p,
            entry_score=sentiment_score,
            entry_rsi=c.rsi,
            entry_vwap=c.vwap,
            entry_chips=c.chip_score,
        )
        self.positions[c.code] = pos
        self._trade_log("BUY", pos, actual_price)
        tag = "買進" if c.strategy == "momentum" else "均值回歸買進"
        fill_note = ""
        if fill and fill["deal_qty"] > 0 and abs(actual_price - c.price) > 0.01:
            fill_note = f"\n實際成交：{actual_qty}股 @ {actual_price:.2f}"
        elif fill and fill["deal_qty"] == 0:
            fill_note = "\n⏳ 尚未成交，待後續確認"
        send_notify(
            f"[{tag}] {stock_label(c.code)}\n"
            f"委託: {c.price} x {c.qty}股\n"
            f"VWAP: {c.vwap:.2f}  RSI: {c.rsi:.1f}  法人: {c.chip_score:+.2f}\n"
            f"止損價: {stop_p:.2f}  止盈啟動: {trail_p:.2f}\n"
            f"ATR: {c.atr_val:.2f}  情緒: {sentiment_score:+.2f}  {analysis}"
            + fill_note
        )

    def scan_candidates(
        self,
        watch_list: list,
        sentiment_score: float,
        analysis: str,
        alloc: "AllocationResult",
    ) -> None:
        """
        全局候選掃描：
        1. 對 watch_list 所有標的評估，收集通過條件的候選清單
        2. 依綜合評分排序（高分優先）
        3. 依序下單，直到部位滿為止
        """
        slots = MAX_POSITIONS - len(self.positions)
        if slots <= 0:
            return

        from src.ai_trade.strategy import MarketRegime
        candidates: list[BuyCandidate] = []

        # 清除已過期的賣出冷卻記錄
        now_ts = time.time()
        expired = [k for k, v in self._sell_cooldown.items() if v <= now_ts]
        for k in expired:
            del self._sell_cooldown[k]

        # ── 評估階段（全部掃完）──────────────────────────────────
        for code in watch_list:
            if code in self.positions:
                print(f"[{code}] 已持有，跳過。")
                continue
            if code in self._sell_cooldown:
                remain = self._sell_cooldown[code] - now_ts
                hrs, mins = divmod(int(remain) // 60, 60)
                print(f"[{code}] 賣出冷卻中（剩 {hrs}時{mins}分），跳過。")
                continue
            # 單檔 budget 一律給 POSITION_SIZE（與動能策略 get_atr_qty 一致）
            # 策略佔比由排序階段的 prior 權重 ×0.90/×1.10 體現，不從金額壓制 —
            # 否則均值回歸只拿 POSITION_SIZE × 20% = 2,300 元，永遠過不了 MIN_ORDER_VALUE 門檻。
            if alloc.regime == MarketRegime.RANGING:
                c = self._eval_mean_reversion(code, POSITION_SIZE)
                if c:
                    candidates.append(c)
                # 盤整市仍允許動能策略作補充
                c2 = self._eval_momentum(code, sentiment_score)
                if c2:
                    candidates.append(c2)
            else:
                c = self._eval_momentum(code, sentiment_score)
                if c:
                    candidates.append(c)
                # 趨勢市也收集均值回歸作補充
                c2 = self._eval_mean_reversion(code, POSITION_SIZE)
                if c2:
                    candidates.append(c2)

        if not candidates:
            print("[掃描] 本輪無符合條件的候選標的。")
            return

        # ── 疑慮 1（方案 B）：依大盤狀態給策略 prior 權重 ─────────
        # RANGING：均值回歸 ×1.10，動能 ×0.90（盤整偏向買弱反彈）
        # TRENDING：動能 ×1.10，均值回歸 ×0.90（趨勢偏向追強勢）
        if alloc.regime == MarketRegime.RANGING:
            momentum_prior, mr_prior = 0.90, 1.10
        else:
            momentum_prior, mr_prior = 1.10, 0.90
        for c in candidates:
            prior = mr_prior if c.strategy == "mean_reversion" else momentum_prior
            c.score = c.score * prior
        print(f"[掃描] 策略加權 動能×{momentum_prior:.2f}  均值回歸×{mr_prior:.2f}")

        # ── 排序階段（綜合評分高分優先）──────────────────────────
        # 同一股票若兩種策略都入選，只保留分數較高者
        best: dict[str, BuyCandidate] = {}
        for c in candidates:
            if c.code not in best or c.score > best[c.code].score:
                best[c.code] = c

        # 疑慮 3：分數接近時的排序穩定性 → 加入次要排序鍵
        # 主鍵：分數（取兩位小數，0.01 內視為平手）
        # 次鍵：法人情緒 → RVOL → RSI 較低者（避免追漲）
        def _composite_key(c: "BuyCandidate"):
            return (
                round(c.score, 2),
                c.chip_score,
                c.rvol,
                -c.rsi,  # RSI 較低者優先
            )
        ranked = sorted(best.values(), key=_composite_key, reverse=True)
        print(f"[掃描] 候選 {len(ranked)} 檔（排序後）：")
        for c in ranked:
            print(f"  {c.describe()}")

        # ── 執行階段（高分優先，受預算與部位上限雙重限制）──────────
        # 下單前強制刷新預算：避免 TOTAL_BUDGET 沿用啟動時的舊值
        # （settlements 反映買入需 T+1/T+2，10 分鐘 refresh 太慢可能跨輪超買）
        self._init_budget()

        # ── 雙層預算保護 ─────────────────────────────────────────
        # 1) budget_cap：TOTAL_BUDGET - pending 凍結（含未來淨額預估，最樂觀）
        # 2) strict_cap：acc + 未來應收 + 未來應付 - pending - 今日已成交買單
        #                以實際現金流為基礎，避免今日買單尚未進入 settlements 造成超買
        spent = 0.0
        pending_amt = self.pending_buy_amount()
        budget_cap = TOTAL_BUDGET - pending_amt

        today_buy_cost = sum(
            pos.entry_price * pos.qty
            for pos in self.positions.values()
            if pos.entry_time.date() == now_tw().date()
        )
        acc_bal       = getattr(self, "_acc_balance_cache", 0.0)
        future_recv   = getattr(self, "_future_recv_cache", 0.0)
        future_pay    = getattr(self, "_future_payable_cache", 0.0)
        strict_cap = acc_bal + future_recv + future_pay - pending_amt - today_buy_cost

        # 模擬戶限制：account_balance() / settlements() 都回 0
        # → strict_cap = 0 → min(budget_cap, 0) = 0 → 永遠不下單
        # 修補：模擬模式下跳過嚴格上限（沒有真實現金流可比對，靠 TOTAL_BUDGET 控制即可）
        # 切正式戶後 acc_bal 會是真實值，strict_cap 自動恢復防違約交割功能。
        if self._simulation:
            broker_cap = getattr(self, "_broker_buy_limit", None)
            if broker_cap and broker_cap > 0:
                broker_remaining = broker_cap - pending_amt - today_buy_cost
                effective_cap = min(budget_cap, broker_remaining)
                print(f"[掃描] 券商 trading_limits.limit_buy = {broker_cap:,.0f}，"
                      f"扣除 pending/今日已買後 = {broker_remaining:,.0f}")
            else:
                effective_cap = budget_cap
            print(
                f"[掃描] 預算上限 budget={budget_cap:,.0f}（模擬模式跳過嚴格上限 — "
                f"acc/settlements 不支援會回 0，正式戶下自動恢復防違約交割保護）"
                f"  → 採用 {effective_cap:,.0f}"
            )
        else:
            # 正式戶：三層保護完整生效
            broker_cap = getattr(self, "_broker_buy_limit", None)
            if broker_cap and broker_cap > 0:
                broker_remaining = broker_cap - pending_amt - today_buy_cost
                effective_cap = min(budget_cap, strict_cap, broker_remaining)
                print(f"[掃描] 券商 trading_limits.limit_buy = {broker_cap:,.0f}，"
                      f"扣除 pending/今日已買後 = {broker_remaining:,.0f}")
            else:
                effective_cap = min(budget_cap, strict_cap)
            print(
                f"[掃描] 預算上限 budget={budget_cap:,.0f}  "
                f"嚴格={strict_cap:,.0f}（acc {acc_bal:,.0f} + 應收 {future_recv:+,.0f} "
                f"+ 應付 {future_pay:+,.0f} - 凍結 {pending_amt:,.0f} "
                f"- 今日已買 {today_buy_cost:,.0f}）  → 採用 {effective_cap:,.0f}"
            )

        if effective_cap <= 0:
            print(f"[掃描] 可用預算 {effective_cap:,.0f} 已歸零，停止下單")
            return

        for c in ranked:
            active_slots, occupied = self.effective_slots()
            if active_slots >= MAX_POSITIONS:
                print(f"[掃描] 已達部位上限 {MAX_POSITIONS}（含委託中），停止下單")
                break
            if c.code in occupied:
                continue
            cost = c.price * c.qty
            if spent + cost > effective_cap:
                print(
                    f"[掃描] {c.code} 下單金額 {cost:,.0f}，累計將達 {spent + cost:,.0f}"
                    f" > 可用預算 {effective_cap:,.0f}，停止下單"
                )
                break
            self._execute_buy(c, sentiment_score, analysis)
            spent += cost

    # ------------------------------------------------------------------
    # 出場監控：移動止盈 + 強制止損
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    def monitor_exit(self) -> None:
        """每輪皆執行，不受情緒/大盤過濾影響"""
        if not self.positions:
            return

        contracts = [self.api.Contracts.Stocks[code] for code in self.positions]
        snapshots = self.api.snapshots(contracts)

        for snap in snapshots:
            code = snap.code
            pos = self.positions.get(code)
            if pos is None:
                continue

            # 長期持有清單：跳過所有自動出場監控，由人工決定
            if code in LONG_TERM_HOLD:
                print(f"[監控] {code} 為長期持有，跳過自動出場監控。")
                continue

            # 盤中零股規則：當日進場的部位 T+1 才能賣，跳過出場監控避免當沖
            if pos.entry_time.date() == now_tw().date():
                print(f"[監控] {code} 今日新建部位（零股 T+1），跳過當日出場。")
                continue

            current = snap.close
            pos.update_max(current)

            profit  = pos.profit_pct(current)
            pullback = pos.pullback_pct(current)

            print(
                f"[監控] {code}  現價={current}  成本={pos.entry_price}"
                f"  獲利={profit:+.2%}  歷史高={pos.max_price}"
                f"  回吐={pullback:.2%}"
            )

            reason = None

            # ── A. ATR 自適應止損 ────────────────────────────────────
            # 早盤跳空保護：09:05~09:20 跌破止損價但虧損 ≤ 5% 時，
            # 視為開盤雜訊，延後止損等待價格穩定。
            # 虧損 > 5% 仍視為嚴重利空，立即執行止損保護。
            now_ts = now_tw()
            in_early_market = (now_ts.hour == 9 and now_ts.minute < 20)
            if current <= pos.stop_price:
                if in_early_market and profit > -0.05:
                    print(
                        f"[早盤保護] {code} 跌破止損但虧損 {profit:.2%} ≤ 5%，"
                        f"延後至 09:20 後再判斷"
                    )
                else:
                    reason = (f"止損（現價{current} ≤ 止損價{pos.stop_price:.2f}，"
                              f"虧損{profit:.2%}，ATR={pos.atr:.2f}）")

            # ── B. 成本保衛（Break-even Stop）────────────────────────
            # 獲利達 BREAKEVEN_TRIGGER 後，止損價自動上移至進場成本（不再允許虧損）
            elif profit >= BREAKEVEN_TRIGGER and pos.stop_price < pos.entry_price:
                pos.stop_price = pos.entry_price
                print(f"[成本保衛] {code}  獲利已達{profit:.2%}，止損上移至成本 {pos.entry_price}")

            # ── C. 動態移動止盈 ───────────────────────────────────────
            # 啟動後：從最高點回落 0.6×ATR（ATR 夠大時）或固定 TRAILING_PULLBACK
            elif current >= pos.trail_price:
                atr_pullback = TRAILING_ATR_MULT * pos.atr if pos.atr > 0 else 0
                pullback_threshold = max(atr_pullback / pos.max_price, TRAILING_PULLBACK)
                if pullback >= pullback_threshold:
                    contract = self.api.Contracts.Stocks[code]
                    if self.check_slippage_safe(contract):
                        reason = (f"移動止盈（高點{pos.max_price}，"
                                  f"回吐{pullback:.2%}≥門檻{pullback_threshold:.2%}，"
                                  f"獲利{profit:.2%}）")

            # ── D. 時間停損 ───────────────────────────────────────────
            # 持有滿 TIME_STOP_BDAYS 工作天內未觸發 A/B/C，且獲利無明顯成長 → 強制出場
            # 「無明顯成長」定義：歷史高點未曾突破移動止盈啟動價（entry × (1+TRAILING_START)）
            #                    代表從未進入止盈區，動能消失，佔用資金
            if not reason:
                held_bdays = _business_days_between(pos.entry_time.date(), now_tw().date())
                peak_growth = (pos.max_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
                no_growth = peak_growth < TRAILING_START
                if held_bdays >= TIME_STOP_BDAYS and no_growth:
                    contract = self.api.Contracts.Stocks[code]
                    if self.check_slippage_safe(contract):
                        reason = (f"時間停損（持有 {held_bdays} 工作天，"
                                  f"歷史高獲利{peak_growth:+.2%} < {TRAILING_START:.1%}，"
                                  f"動能不足，釋放資金）")

            if reason:
                self._execute_exit(code, current, reason)

    def _execute_exit(self, code: str, price: float, reason: str) -> None:
        pos = self.positions.get(code)
        if pos is None:
            return
        contract = self.api.Contracts.Stocks[code]
        # 查詢實際持倉數量
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
            hold = next((p for p in held if p.code == code), None)
            qty = hold.quantity if hold else pos.qty
        except Exception:
            qty = pos.qty

        ok = self._place_odd_order(contract, price, qty, sj.constant.Action.Sell)
        if not ok:
            print(f"[警告] {code} 賣單被拒，部位保留，下輪繼續監控。")
            return

        # 確認成交：以實際成交價計算損益
        fill = self._confirm_fill(code, "Sell")
        actual_price = fill["deal_price"] if fill and fill["deal_qty"] > 0 else price
        actual_qty   = fill["deal_qty"]   if fill and fill["deal_qty"] > 0 else qty

        profit_pct = (actual_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
        net_pnl    = (actual_price - pos.entry_price) * actual_qty * (1 - TRADE_COST_PCT)
        self._trade_log("SELL", pos, actual_price, reason=reason)   # 2.3
        # 移除部位，但 _pending_orders 保留 Position 副本
        # 若賣單被取消，_sync_pending_orders 會將部位恢復
        self._pending_sell_positions[code] = pos
        del self.positions[code]

        # 賣出冷卻：避免賣完立刻買回造成鋸齒交易
        # 跳空止損（開盤 09:05~09:30 觸發 + 虧損 >2.5%）→ 冷卻至次日 09:30
        # 一般出場 → 冷卻 30 分鐘
        now_dt = now_tw()
        is_gap_stop = (
            "止損" in reason
            and profit_pct < -0.025
            and (now_dt.hour == 9 and now_dt.minute <= 30)
        )
        if is_gap_stop:
            next_day_open = (now_dt + timedelta(days=1)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            self._sell_cooldown[code] = next_day_open.timestamp()
            cooldown_desc = f"跳空止損 → 冷卻至明日 09:30"
        else:
            self._sell_cooldown[code] = time.time() + 1800   # 30 分鐘
            cooldown_desc = "一般出場 → 冷卻 30 分鐘"
        print(f"[賣出冷卻] {code}  {cooldown_desc}")
        fill_note = ""
        if fill and fill["deal_qty"] > 0 and abs(actual_price - price) > 0.01:
            fill_note = f"\n實際成交：{actual_qty}股 @ {actual_price:.2f}"
        elif fill and fill["deal_qty"] == 0:
            fill_note = "\n⏳ 尚未成交，待後續確認"
        send_notify(
            f"[賣出] {stock_label(code)}  {reason}\n"
            f"委託: {price} x {qty}股\n"
            f"成本: {pos.entry_price}  獲利: {profit_pct:+.2%}\n"
            f"淨損益: {net_pnl:+.0f} 元"
            + fill_note
        )

    # ------------------------------------------------------------------
    # 2.3 績效日誌：每筆進出場寫入 logs/trades_YYYYMMDD.csv
    # ------------------------------------------------------------------
    def _trade_log(self, action: str, pos: "Position", price: float, reason: str = "") -> None:
        import csv, pathlib
        log_dir = pathlib.Path("logs")
        log_dir.mkdir(exist_ok=True)
        today   = now_tw().strftime("%Y%m%d")
        fpath   = log_dir / f"trades_{today}.csv"
        is_new  = not fpath.exists()
        net_pnl = (price - pos.entry_price) * pos.qty * (1 - TRADE_COST_PCT) if action == "SELL" else 0.0
        row = {
            "timestamp":    now_tw().strftime("%Y-%m-%d %H:%M:%S"),
            "action":       action,
            "code":         pos.code,
            "price":        price,
            "qty":          pos.qty,
            "entry_price":  pos.entry_price,
            "stop_price":   round(pos.stop_price, 2),
            "trail_price":  round(pos.trail_price, 2),
            "atr":          round(pos.atr, 2),
            "entry_score":  round(pos.entry_score, 2),
            "entry_rsi":    round(pos.entry_rsi, 1),
            "entry_vwap":   round(pos.entry_vwap, 2),
            "entry_chips":  pos.entry_chips,
            "net_pnl":      round(net_pnl, 0),
            "reason":       reason,
        }
        with open(fpath, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        print(f"[日誌] {action} {pos.code} @ {price}  寫入 {fpath}")

    # ------------------------------------------------------------------
    # 零股下單
    # ------------------------------------------------------------------
    def _place_odd_order(self, contract, price: float, qty: int, action) -> bool:
        """回傳 True 表示下單成功（op_code == '00'），False 表示交易所拒單"""
        order = self.api.Order(
            price=price,
            quantity=qty,
            action=action,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_lot=sj.constant.StockOrderLot.IntradayOdd,
            account=self.api.stock_account,
        )
        trade = self.api.place_order(contract, order)
        op_code = getattr(getattr(trade, "operation", None), "op_code", "00")
        ok = (op_code == "00")
        status_str = trade.status.status if ok else f"拒單(op_code={op_code})"
        print(f"[下單] {action} {contract.code} x{qty} @ {price}  狀態: {status_str}")
        if ok:
            code = contract.code
            act_str = str(action).split(".")[-1]  # "Buy" or "Sell"
            self._pending_orders[code] = {
                "action": act_str,
                "price": price,
                "qty": qty,
                "amount": price * qty,
                "trade": trade,
                "created_at": time.time(),
            }
            print(f"[委託追蹤] {act_str} {code} x{qty} @ {price} 加入追蹤")
        return ok

    # ------------------------------------------------------------------
    # 下單後即時確認成交：等待數秒後查 update_status 確認成交結果
    # ------------------------------------------------------------------
    def _confirm_fill(self, code: str, expected_action: str) -> dict | None:
        """
        下單後短暫等待，查詢成交結果。
        回傳 {"deal_qty": int, "deal_price": float, "status": str} 或 None（查詢失敗）。
        """
        pending = self._pending_orders.get(code)
        if not pending:
            return None
        trade_obj = pending["trade"]

        # 等待交易所回報（零股通常 1~3 秒內回報）
        time.sleep(3)

        try:
            self.api.update_status(self.api.stock_account)
        except Exception:
            pass

        try:
            status_obj = getattr(trade_obj, "status", None)
            status_str = str(getattr(status_obj, "status", ""))
            deal_qty   = int(getattr(status_obj, "deal_quantity", 0))
            # 計算成交均價（從 deals 列表）
            deals = getattr(trade_obj, "deals", []) or []
            if deals:
                total_amt = sum(float(getattr(d, "price", 0)) * int(getattr(d, "quantity", 0)) for d in deals)
                total_qty = sum(int(getattr(d, "quantity", 0)) for d in deals)
                deal_price = total_amt / total_qty if total_qty > 0 else pending["price"]
            else:
                deal_price = pending["price"]

            result = {"deal_qty": deal_qty, "deal_price": deal_price, "status": status_str}
            print(
                f"[成交確認] {expected_action} {code}  "
                f"狀態={status_str}  成交={deal_qty}/{pending['qty']}股"
                + (f"  均價={deal_price:.2f}" if deal_qty > 0 else "")
            )
            # 全部成交 → 立即移除 pending，避免凍結金額殘留
            if deal_qty >= pending["qty"] or "Filled" in status_str:
                self._pending_orders.pop(code, None)
                self._pending_sell_positions.pop(code, None)
                print(f"[委託追蹤] {code} 已確認成交，解除凍結")
            return result
        except Exception as e:
            print(f"[成交確認] {code} 查詢失敗: {e}")
            return None

    # ------------------------------------------------------------------
    # 委託成交確認：同步 list_trades() 更新委託狀態
    # ------------------------------------------------------------------
    def _sync_pending_orders(self) -> None:
        """
        檢查 _pending_orders 內的委託是否已成交。
        - 買單全部成交 → 移除追蹤（部位已在 self.positions）
        - 買單未成交   → 保留追蹤，凍結資金
        - 賣單全部成交 → 移除追蹤，確認部位已清除
        - 賣單未成交   → 保留追蹤，部位恢復至 self.positions（防止空出 slot 又買入）
        """
        if not self._pending_orders:
            return
        try:
            try:
                trades = self.api.list_trades(self.api.stock_account)
            except TypeError:
                trades = self.api.list_trades()
        except Exception as e:
            print(f"[委託追蹤] list_trades 失敗: {e}")
            return

        # 建立 ordno → trade 索引（用於比對成交狀態）
        trade_map: dict[str, object] = {}
        for t in (trades or []):
            ordno = getattr(getattr(t, "status", None), "ordno", None) or \
                    getattr(getattr(t, "order", None), "ordno", None)
            if ordno:
                trade_map[ordno] = t

        resolved: list[str] = []
        for code, info in self._pending_orders.items():
            pending_trade = info.get("trade")
            ordno = getattr(getattr(pending_trade, "order", None), "ordno", None) or \
                    getattr(getattr(pending_trade, "status", None), "ordno", None)
            if not ordno:
                resolved.append(code)
                continue

            live = trade_map.get(ordno)
            if live is None:
                continue

            status_obj = getattr(live, "status", None)
            status_str = str(getattr(status_obj, "status", ""))
            order_qty = info["qty"]
            deal_qty  = int(getattr(status_obj, "deal_quantity", 0))

            if "Filled" in status_str or deal_qty >= order_qty:
                # 全部成交
                print(f"[委託追蹤] {info['action']} {code} 已全部成交（{deal_qty}/{order_qty}）")
                self._pending_sell_positions.pop(code, None)  # 清除賣單備份
                resolved.append(code)
            elif "Cancelled" in status_str:
                print(f"[委託追蹤] {info['action']} {code} 已取消（成交 {deal_qty}/{order_qty}）")
                if info["action"] == "Buy" and code in self.positions:
                    if deal_qty == 0:
                        del self.positions[code]
                        print(f"[委託追蹤] {code} 買單取消且未成交，移除部位")
                elif info["action"] == "Sell" and code not in self.positions:
                    # 賣單取消 → 從備份恢復部位
                    backup = self._pending_sell_positions.pop(code, None)
                    if backup:
                        self.positions[code] = backup
                        print(f"[委託追蹤] {code} 賣單取消，部位已恢復，下輪繼續監控")
                        send_notify(f"[委託追蹤] ⚠️ {code} 賣單取消（{deal_qty}/{order_qty}），部位已恢復監控")
                resolved.append(code)
            else:
                # 仍在委託中
                if info["action"] == "Sell" and code not in self.positions:
                    # 賣單未成交但部位已被移除 → 發出警告
                    print(f"[委託追蹤] ⚠️ {code} 賣單未成交（{deal_qty}/{order_qty}），部位已移除")

        # 超時自動取消：委託超過 PENDING_ORDER_TIMEOUT 秒仍未成交 → 呼叫 cancel_order 取消
        # 真正向交易所撤單，釋放凍結資金（單純從追蹤移除無法解除券商端凍結）
        now_ts = time.time()
        for code, info in list(self._pending_orders.items()):
            if code in resolved:
                continue
            age = now_ts - info.get("created_at", now_ts)
            if age > PENDING_ORDER_TIMEOUT:
                trade_obj = info.get("trade")
                act_str   = info["action"]
                try:
                    if trade_obj is not None:
                        self.api.cancel_order(trade_obj)
                        print(f"[委託追蹤] {act_str} {code} 超過 {PENDING_ORDER_TIMEOUT}s 未成交，已送出取消")
                        # 只在第一次推 Telegram，避免下輪 sync 又進這分支重複 spam 用戶
                        # （cancel_order 對交易所重發無害，但 Telegram 推 N 次會洗版）
                        if not info.get("timeout_notified"):
                            send_notify(
                                f"[委託逾時取消] ⏱️ {stock_label(code)}\n"
                                f"{act_str} {info['qty']}股 @ {info['price']}  金額 {info['amount']:,.0f} 元\n"
                                f"持續 {age:.0f} 秒未成交，已自動撤單"
                            )
                            info["timeout_notified"] = True
                        # 不在這裡 resolved.append → 等 callback Cancelled 事件處理
                        # 若 callback 漏接，下一輪 sync 仍會嘗試 cancel（cancel 重發無害，
                        # 但 Telegram 已用 timeout_notified flag 去重）
                    else:
                        # 沒有 trade 物件就無法撤單，只能清除追蹤
                        print(f"[委託追蹤] {act_str} {code} 超時但無 trade 物件，強制解除凍結")
                        resolved.append(code)
                except Exception as e:
                    print(f"[委託追蹤] {act_str} {code} 超時撤單失敗: {e}，強制解除凍結")
                    resolved.append(code)

        for code in resolved:
            del self._pending_orders[code]

    def effective_slots(self) -> tuple[int, set[str]]:
        """
        計算實際佔用的部位 slot 數量。
        包含：
          - 已成交持倉（self.positions）
          - 未成交買單（_pending_orders Buy，避免短時間連續超買）
          - 未確認賣單（_pending_orders Sell，slot 尚未真正釋放）
        相同 code 在不同集合只算 1 次。
        回傳 (slot 數, 佔用 code 集合)
        """
        occupied = set(self.positions.keys()) | set(self._pending_orders.keys())
        return len(occupied), occupied

    def pending_buy_amount(self) -> float:
        """回傳所有委託中買單的凍結金額"""
        return sum(
            info["amount"]
            for info in self._pending_orders.values()
            if info["action"] == "Buy"
        )

    # ------------------------------------------------------------------
    # 部位校驗：比對 self.positions 與 API 實際持倉
    # ------------------------------------------------------------------
    def _verify_positions(self) -> None:
        """
        買入前呼叫。比對 self.positions 與 list_positions：
        - API 有但 bot 沒有 → 補入（可能是手動買入或上次未紀錄）
        - bot 有但 API 沒有 → 移除（可能已成交賣出但 bot 未同步）
        - 數量不一致 → 以 API 為準更新
        """
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
        except Exception as e:
            print(f"[部位校驗] 查詢失敗: {e}")
            return

        api_codes = {}
        for p in (held or []):
            code = p.code
            qty  = int(getattr(p, "quantity", 0))
            if qty > 0:
                api_codes[code] = {
                    "qty": qty,
                    "price": float(getattr(p, "price", None) or getattr(p, "average_price", 0)),
                    "last": float(getattr(p, "last_price", 0) or 0),
                }

        changed = False

        # API 有但 bot 沒有 → 補入
        for code, info in api_codes.items():
            if code not in self.positions:
                # 排除正在賣出中的（pending sell）
                if code in self._pending_orders and self._pending_orders[code]["action"] == "Sell":
                    continue
                pos = Position(code=code, entry_price=info["price"], qty=info["qty"])
                pos.entry_time = now_tw() - timedelta(days=1)  # 預設為非今日
                self.positions[code] = pos
                print(f"[部位校驗] 補入 {code} {info['qty']}股 均價={info['price']}")
                changed = True

        # bot 有但 API 沒有 → 移除
        stale = [code for code in self.positions if code not in api_codes]
        for code in stale:
            # 排除正在買入中的（pending buy）
            if code in self._pending_orders and self._pending_orders[code]["action"] == "Buy":
                continue
            print(f"[部位校驗] 移除 {code}（API 已無持倉）")
            del self.positions[code]
            changed = True

        # 數量不一致 → 以 API 為準
        for code in list(self.positions):
            if code in api_codes and self.positions[code].qty != api_codes[code]["qty"]:
                old_qty = self.positions[code].qty
                self.positions[code].qty = api_codes[code]["qty"]
                print(f"[部位校驗] {code} 數量更新 {old_qty} → {api_codes[code]['qty']}股")
                changed = True

        if not changed:
            print("[部位校驗] 部位一致")

    # ------------------------------------------------------------------
    # 定期狀態報告：委託 + 部位狀況推播至 Telegram
    # ------------------------------------------------------------------
    def periodic_status_report(self) -> None:
        """每 30 分鐘呼叫一次，推播委託與部位狀態"""
        lines = [f"[定期狀態報告] {now_tw().strftime('%H:%M:%S')}"]

        # 1) 同步委託狀態
        self._sync_pending_orders()

        # 2) 未完成委託
        if self._pending_orders:
            lines.append(f"\n[未完成委託] {len(self._pending_orders)} 筆")
            for code, info in self._pending_orders.items():
                lines.append(
                    f"  {info['action']} {code}  "
                    f"{info['qty']}股 @ {info['price']}  "
                    f"金額 {info['amount']:,.0f} 元"
                )
        else:
            lines.append("\n[未完成委託] 無")

        # 3) 校驗部位
        self._verify_positions()

        # 4) 目前持倉 + 出場條件
        lines.append(f"\n{self.get_positions_with_exits()}")

        report = "\n".join(lines)
        print(report)
        send_notify(report)

    def daily_summary(self) -> str:
        """產生今日交易總結，包含成交紀錄、損益與持倉狀況"""
        lines = [f"[今日交易總結] {now_tw().strftime('%Y-%m-%d')}"]
        lines.append("─" * 32)

        # 成交紀錄
        try:
            try:
                trades = self.api.list_trades(self.api.stock_account)
            except TypeError:
                trades = self.api.list_trades()   # 模擬帳戶不接受 account 參數
            today  = now_tw().strftime("%Y-%m-%d")
            today_trades = [
                t for t in (trades or [])
                if hasattr(t, "status") and
                str(getattr(t.status, "order_datetime", "")).startswith(today)
            ]
            if today_trades:
                lines.append(f"成交紀錄（{len(today_trades)} 筆）：")
                for t in today_trades:
                    action = getattr(t.order, "action", "-")
                    code   = getattr(t.contract, "code", "-")
                    price  = getattr(t.order, "price", "-")
                    qty    = getattr(t.order, "quantity", "-")
                    status = getattr(t.status, "status", "-")
                    lines.append(f"  {action} {code}  {qty}股 @ {price}  {status}")
            else:
                lines.append("成交紀錄：今日無成交")
        except Exception as e:
            lines.append(f"成交紀錄：查詢失敗 ({e})")

        lines.append("─" * 32)

        # 未實現損益（現有持倉）
        summary = self.get_positions_summary()
        lines.append(f"收盤持倉：\n{summary}")

        lines.append("─" * 32)

        # 已實現損益
        try:
            today_str = now_tw().strftime("%Y-%m-%d")
            pnl_list  = self.api.list_profit_loss(
                self.api.stock_account,
                begin_date=today_str,
                end_date=today_str,
            )
            if pnl_list:
                total_realized = sum(getattr(p, "profitloss", 0) or 0 for p in pnl_list)
                lines.append(f"已實現損益：{total_realized:+.0f} 元（{len(pnl_list)} 筆）")
            else:
                lines.append("已實現損益：今日無已實現損益")
        except Exception as e:
            lines.append(f"已實現損益：查詢失敗 ({e})")

        return "\n".join(lines)

    def logout(self) -> None:
        summary = self.daily_summary()
        print(f"\n{summary}")
        send_notify(summary)
        # ── 每日收盤分隔線：方便翻 Telegram 紀錄一眼分辨交易日 ──
        _wd_zh = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][now_tw().weekday()]
        send_notify(
            f"═════════════════════\n"
            f"📅 {now_tw().strftime('%Y-%m-%d')} ({_wd_zh})\n"
            f"🔴 交易日結束\n"
            f"═════════════════════"
        )
        self.api.logout()
        print("[系統] 已登出")


# =============================================================================
# 5. 主程式
# =============================================================================

if __name__ == "__main__":
    bot = AITradingBot()
    market_agg = NewsAggregator(stock_code="")

    print("=" * 55)
    _mode = "simulation=True（模擬）" if bot._simulation else "simulation=False（正式交易⚠️）"
    print(f"AI 交易系統啟動  模式：{_mode}")
    print(f"監控清單（{len(bot.watch_list)} 檔）：\n{bot.format_watch_list()}")
    print(f"最大部位：{MAX_POSITIONS}  單筆：{POSITION_SIZE:,} 元")
    print(f"止損：{STOP_LOSS_PCT:.0%}  移動止盈啟動：{TRAILING_START:.1%}  回吐：{TRAILING_PULLBACK:.1%}")
    print(f"滑點上限：{SLIPPAGE_LIMIT:.1%}")
    print("=" * 55)

    # ── 啟動分析 ──
    print("[啟動分析] 抓取新聞中...")
    startup_news   = market_agg.fetch_headlines(limit=10)
    startup_digest = market_agg.format_telegram_digest(limit=10)
    startup_score, startup_analysis = (
        get_ai_sentiment(startup_news) if startup_news else (0.0, "無法取得新聞")
    )
    print(f"[啟動分析] 情緒分: {startup_score:+.2f}  {startup_analysis}")

    positions_summary = bot.get_positions_summary()
    print(f"[持倉]\n{positions_summary}")

    # ── 累計損益 ──
    pnl_summary = bot.calc_total_pnl()
    print(pnl_summary)

    # ── 啟動委託檢查：偵測遺留未成交單，重新評估買單 ──
    print("[啟動] 檢查遺留委託...")
    order_check_result = bot.startup_order_check()
    print(order_check_result)

    # ── 每日開盤分隔線：方便翻 Telegram 紀錄一眼分辨交易日 ──
    _wd_zh = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][now_tw().weekday()]
    send_notify(
        f"═════════════════════\n"
        f"📅 {now_tw().strftime('%Y-%m-%d')} ({_wd_zh})\n"
        f"🟢 交易日開始\n"
        f"═════════════════════"
    )

    send_notify(
        f"[AI Trade 啟動]\n"
        f"模式：{'simulation=True（模擬）' if bot._simulation else 'simulation=False（正式交易⚠️）'}\n"
        f"部位上限：{MAX_POSITIONS} 檔 | 單筆：{POSITION_SIZE:,} 元\n"
        f"止損 {STOP_LOSS_PCT:.0%} | 移動止盈 {TRAILING_START:.1%}→{TRAILING_PULLBACK:.1%} | 滑點 {SLIPPAGE_LIMIT:.1%}\n"
        f"監控清單（{len(bot.watch_list)} 檔）：\n{bot.format_watch_list()}\n"
        f"啟動時間：{now_tw().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)\n"
        f"\n[目前持倉]\n{positions_summary}\n"
        f"\n{pnl_summary}\n"
        f"\n{order_check_result}\n"
        f"\n[啟動情緒分析]\n"
        f"分數：{startup_score:+.2f}  {sentiment_label(startup_score)}\n"
        f"摘要：{startup_analysis}\n"
        f"\n[最新新聞]\n{startup_digest}"
    )

    last_digest_sent: float = time.time()
    last_budget_refresh: float = time.time()
    last_status_report: float = time.time()

    try:
        while True:
            now = now_tw()   # 台灣時間
            in_market = (
                (now.hour == 9 and now.minute >= 5)
                or (9 < now.hour < 13)
                or (now.hour == 13 and now.minute <= 25)
            )

            if in_market:
                print(f"\n[{now.strftime('%H:%M:%S')} +08:00] 交易時間掃描  部位：{list(bot.positions.keys()) or '無'}")

                # 健康監控：大盤判斷連續失敗超過 1 小時 → 推 Telegram 警示
                # 之前 5 天 0 進場就是 check_market_trend 每分鐘 raise 但靜默吞掉，
                # 主迴圈不知道、用戶不知道。這道防線確保未來這類問題會主動報警。
                _last_ok = getattr(bot, "_last_market_check_success", 0.0)
                if _last_ok > 0:    # 0 表示還沒成功過一次（初始化中），跳過
                    stale_sec = time.time() - _last_ok
                    if stale_sec > 3600:    # 1 小時沒成功
                        bot._notify_anomaly(
                            "market_check_stale",
                            f"bot 已連續 {int(stale_sec/60)} 分鐘無法判斷大盤趨勢，所有進場掃描會被跳過。\n"
                            f"請檢查雲端 log 看 [大盤] 取得失敗原因（可能是 Shioaji API 變動）。",
                            cooldown_sec=3600,
                        )

                # 定期重查 settlements()，確保賣出應收款及時反映至 TOTAL_BUDGET
                if time.time() - last_budget_refresh >= BUDGET_REFRESH_INTERVAL:
                    bot._init_budget(notify=True)
                    last_budget_refresh = time.time()

                # 委託成交確認（每輪必跑，更新凍結資金 / 恢復取消部位）
                bot._sync_pending_orders()

                # 定期狀態報告（每 30 分鐘推播委託 + 部位至 Telegram）
                if time.time() - last_status_report >= STATUS_REPORT_INTERVAL:
                    bot.periodic_status_report()
                    last_status_report = time.time()

                # 出場監控（每輪必跑，不受任何過濾影響）
                bot.monitor_exit()

                # 漏斗掃描（09:20 每日一次，更新當日監控清單）
                bot.run_funnel_if_needed()

                # 滿倉檢查：持倉 + 委託中（買單+賣單）達 MAX_POSITIONS 時跳過
                active_slots, occupied_codes = bot.effective_slots()
                if active_slots >= MAX_POSITIONS:
                    pending_buys  = len([v for v in bot._pending_orders.values() if v["action"] == "Buy"])
                    pending_sells = len([v for v in bot._pending_orders.values() if v["action"] == "Sell"])
                    print(
                        f"[策略] 已佔 {active_slots} 檔（持倉 {len(bot.positions)} "
                        f"+ 待買 {pending_buys} + 待賣 {pending_sells}，上限 {MAX_POSITIONS}），跳過進場掃描。"
                    )
                    # 滿倉時 check_market_trend 不會被呼叫，但 bot 健康無虞 →
                    # 重置 watchdog 時間戳，避免誤觸「60 分鐘無法判斷大盤」警示
                    bot._last_market_check_success = time.time()
                    time.sleep(SCAN_INTERVAL)
                    continue

                # 早盤過濾：09:05~09:20 不執行進場掃描
                # 開盤初期跳空、撮合不穩定，避免追跳空後反彈被套
                # （monitor_exit 仍正常執行，止損保護不受影響）
                if now.hour == 9 and now.minute < 20:
                    print(f"[策略] 早盤過濾（{now.strftime('%H:%M')} < 09:20），跳過進場掃描")
                    # 早盤過濾期間 check_market_trend 不會被呼叫 → 同滿倉處理
                    bot._last_market_check_success = time.time()
                    time.sleep(SCAN_INTERVAL)
                    continue

                # 大盤過濾
                if not bot.check_market_trend():
                    print("[策略] 大盤月線以下，跳過進場掃描。")
                    time.sleep(SCAN_INTERVAL)
                    continue

                # 市場情緒分析（可透過 SENTIMENT_ENABLED 開關控制）
                if SENTIMENT_ENABLED:
                    news_text = market_agg.fetch_headlines(limit=10)
                    if not news_text:
                        print("[新聞] 無法取得今日新聞，跳過本輪。")
                        time.sleep(SCAN_INTERVAL)
                        continue

                    raw_score, analysis = get_ai_sentiment(news_text)
                    score = bot.smooth_sentiment(raw_score)   # 1.1 情緒平滑
                    print(f"[AI] 市場情緒 {score:+.2f}  {sentiment_label(score)}  {analysis}")
                    send_notify(
                        f"[AI 市場情緒] {now.strftime('%H:%M')}\n"
                        f"分數：{score:+.2f}  {sentiment_label(score)}\n"
                        f"摘要：{analysis}"
                    )
                else:
                    score    = 1.0   # 情緒關閉時視為中性偏多，直接進入策略掃描
                    analysis = "（情緒分析已關閉）"
                    print(f"[AI] 情緒分析已停用，以預設分數 {score:+.2f} 執行策略。")

                if score > 0.6:
                    # 3.2 多策略框架：依市場狀態決定策略比重
                    alloc = bot.allocator.allocate()
                    print(f"[策略] {alloc.describe()}")
                    # 策略配置：只在 regime 改變時推播，避免每分鐘重複通知
                    if alloc.regime.value != bot._last_regime:
                        bot._last_regime = alloc.regime.value
                        send_notify(
                            f"[策略配置變更] {alloc.regime.value}\n"
                            f"波動率：{alloc.vol_ann:.1%}\n"
                            f"動能：{alloc.momentum_budget_pct:.0%}  均值回歸：{alloc.mean_reversion_budget_pct:.0%}"
                        )

                    # 買入前校驗：確認委託已成交、部位與 API 一致
                    bot._sync_pending_orders()
                    bot._verify_positions()

                    # 統一由 scan_candidates() 處理策略分配（已內建 RANGING/TRENDING 邏輯）
                    bot.scan_candidates(bot.watch_list, score, analysis, alloc)
                else:
                    print(f"[策略] 市場情緒不足（{score:.2f}），不進場。")

            else:
                print(f"[{now.strftime('%H:%M:%S')} +08:00] 非交易時間  部位：{list(bot.positions.keys()) or '無'}")
                if time.time() - last_digest_sent >= NEWS_DIGEST_INTERVAL:
                    digest = market_agg.format_telegram_digest(limit=10)
                    send_notify(
                        f"[新聞摘要] {now.strftime('%Y-%m-%d %H:%M')}\n"
                        f"監控：{', '.join(bot.watch_list)}\n"
                        f"{'─' * 28}\n{digest}"
                    )
                    last_digest_sent = time.time()

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\n[系統] 使用者中止。")
    finally:
        bot.logout()
