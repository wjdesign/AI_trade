"""
批次回測工具 — 一次驗證多檔股票，輸出比較表。

用法：
    uv run python batch_backtest.py             # 跑預設 82 檔
    uv run python batch_backtest.py 2330,2454   # 自訂清單

輸出：依獲利因子排序的表格 + 標記是否通過 bot.py 入選門檻（PF≥1.1、Sharpe≥0.6）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

# Windows cp950 console fix：強制 UTF-8 輸出，避免 ≥ 等字元爆掉
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import pandas as pd

from backtest import BacktestEngine

TZ_TW = timezone(timedelta(hours=8))

# ── 預設清單：現有 PINNED_STOCKS 75 檔 + Wayne 新增 7 檔 = 82 檔 ───
DEFAULT_CODES = [
    # 12 檔已驗證
    "2059", "8210", "3324", "2454", "3017", "2330", "8996",
    "1590", "2603", "2609", "2357", "2379",
    # 63 檔圖片辨識（未驗證）
    "6664", "6640", "7772", "2464", "6141", "6291", "3265", "6257",
    "3653", "5271", "2485", "2313", "2449", "3701", "4903", "3450",
    "2303", "8033", "6205", "6669", "4958", "6213", "6673", "6826",
    "6488", "4991", "6147", "6442", "2489", "4755", "6231", "2103",
    "3406", "8027", "5498", "4979", "6191", "8021", "3595", "2337",
    "6434", "2486", "3585", "3363", "7769", "7750", "3211", "3455",
    "3016", "8046", "8358", "2426", "3163", "6770", "3661", "3443",
    "5347", "8042", "3037", "6166", "2308", "2408",
    # 7 檔 Wayne 新增
    "8028", "3189", "2327", "6271", "2344", "3481", "3264",
]

PF_THRESHOLD = 1.1
SHARPE_THRESHOLD = 0.6


def main() -> None:
    codes = (
        [c.strip() for c in sys.argv[1].split(",")]
        if len(sys.argv) > 1
        else DEFAULT_CODES
    )

    end = datetime.now(TZ_TW).strftime("%Y-%m-%d")
    start = "2021-01-01"

    engine = BacktestEngine(api=None)
    rows: list[dict] = []
    failed: list[tuple[str, str]] = []

    print(f"[批次回測] 共 {len(codes)} 檔，期間 {start} ~ {end}", file=sys.stderr)
    for i, code in enumerate(codes, 1):
        sys.stderr.write(f"[{i:3d}/{len(codes)}] {code:<6} ... ")
        sys.stderr.flush()
        try:
            result = engine.run(code, start, end, use_yf=True)
            s = result.summary()
            if "error" in s:
                failed.append((code, s["error"]))
                sys.stderr.write(f"SKIP ({s['error']})\n")
                continue
            try:
                pf = float(s["獲利因子"])
                sharpe = float(s["夏普比率"])
            except (TypeError, ValueError):
                pf = 0.0
                sharpe = 0.0
            s["PF_num"] = pf
            s["Sharpe_num"] = sharpe
            s["通過門檻"] = "✓" if (pf >= PF_THRESHOLD and sharpe >= SHARPE_THRESHOLD) else "✗"
            rows.append(s)
            sys.stderr.write(f"PF={pf:.2f} Sharpe={sharpe:.2f} 通過={s['通過門檻']}\n")
        except Exception as e:
            failed.append((code, str(e)[:60]))
            sys.stderr.write(f"EXCEPTION ({e})\n")

    if not rows:
        print("[結果] 沒有可用結果。", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(rows)
    df = df.sort_values("PF_num", ascending=False)

    cols = ["標的", "總交易次數", "勝率", "獲利因子", "夏普比率",
            "最大回撤", "淨損益合計", "通過門檻"]
    display = df[cols].copy()

    print("\n" + "=" * 92)
    print(f"  批次回測比較表（依獲利因子排序，門檻 PF≥{PF_THRESHOLD} 且 Sharpe≥{SHARPE_THRESHOLD}）")
    print("=" * 92)
    print(display.to_string(index=False))

    passed = df[df["通過門檻"] == "✓"]
    not_passed = df[df["通過門檻"] == "✗"]

    print("\n" + "=" * 92)
    print(f"  通過門檻 ({len(passed)}/{len(df)}): "
          + ", ".join(passed["標的"].tolist()))
    print(f"  未通過   ({len(not_passed)}/{len(df)}): "
          + ", ".join(not_passed["標的"].tolist()))
    if failed:
        print(f"  抓取失敗 ({len(failed)}): "
              + ", ".join(f"{c}({e})" for c, e in failed))
    print("=" * 92)


if __name__ == "__main__":
    main()
