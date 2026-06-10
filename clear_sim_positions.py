"""
Sinopac 模擬戶持倉清除工具
==========================

問題：反覆執行 bot.py 會在 Sinopac 模擬戶上累積歷史持倉（沙箱 stateful），
     導致 [目前持倉] 顯示鉅額虛擬部位、實測損益失真。

這個腳本會：
  1. 登入 Shioaji 模擬戶（強制 simulation=True，正式戶絕對碰不到）
  2. 列出所有現有持倉 + 市值
  3. 要求互動確認（輸入 YES 才繼續）
  4. 對每檔依「市價 -8% 限價」下零股賣單（盤中零股不支援市價單）
  5. 等待撮合，再次查詢確認剩餘

執行：盤中時段（09:00-13:25）跑最有效
    uv run python clear_sim_positions.py

安全保護：
  - 強制 sj.Shioaji(simulation=True)
  - 三段確認：自動讀 .env、列出持倉、輸入 YES
  - 正式戶完全不受影響（不同沙箱）
"""

from __future__ import annotations

import os
import sys
import time

import shioaji as sj
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    print("=" * 60)
    print("  Sinopac 模擬戶持倉清除工具")
    print("=" * 60)
    print()
    print("⚠️  強制 simulation=True — 正式戶完全不受影響")
    print()

    # 強制模擬模式（多一層保護，無視 .env 的 SIMULATION 設定）
    api = sj.Shioaji(simulation=True)

    print("[1/5] 登入 Shioaji 模擬戶...")
    try:
        api.login(
            api_key=os.environ["API_KEY"],
            secret_key=os.environ["SECRET_KEY"],
            fetch_contract=True,
            contracts_timeout=30000,
        )
    except Exception as e:
        print(f"❌ 登入失敗: {type(e).__name__}: {e}")
        sys.exit(1)

    try:
        api.activate_ca(
            ca_path=os.environ["CA_CERT_PATH"],
            ca_passwd=os.environ["CA_PASSWORD"],
        )
    except Exception as e:
        print(f"⚠️ activate_ca 失敗（仍嘗試繼續）: {type(e).__name__}: {e}")

    print(f"✓ 登入成功，stock_account={api.stock_account}")
    print()

    # ── 列出當前持倉 ─────────────────────────────────────
    print("[2/5] 查詢持倉中...")
    try:
        positions = api.list_positions(
            api.stock_account,
            unit=sj.constant.Unit.Share,
        )
    except Exception as e:
        print(f"❌ list_positions 失敗: {e}")
        api.logout()
        sys.exit(1)

    if not positions:
        print("✓ 模擬戶目前無任何持倉，無需清除。")
        api.logout()
        return

    print(f"📊 偵測到 {len(positions)} 檔持倉：")
    print("-" * 60)
    total_value = 0.0
    for p in positions:
        code  = p.code
        qty   = int(p.quantity)
        price = float(p.last_price)
        mv    = qty * price
        total_value += mv
        print(f"  {code}  {qty:>10,} 股  @ {price:>8.2f}  市值 {mv:>15,.0f} 元")
    print("-" * 60)
    print(f"  總市值（虛擬）: {total_value:,.0f} 元")
    print()

    # ── 互動確認 ───────────────────────────────────────
    print("[3/5] 互動確認")
    print("⚠️  將對上述全部持倉送出「市價 -8% 限價」零股賣單。")
    print("    盤中（09:00-13:25）才會被撮合；盤後送出但不會成交。")
    print()
    confirm = input("輸入 YES（全大寫）確認清除，其他任意輸入取消: ").strip()
    if confirm != "YES":
        print("已取消，未送出任何賣單。")
        api.logout()
        return

    # ── 送賣單 ────────────────────────────────────────
    print()
    print("[4/5] 送出賣單中...")
    success = 0
    fail    = 0
    for p in positions:
        code  = p.code
        qty   = int(p.quantity)
        price = float(p.last_price)
        # 零股不支援市價單，用「市價 -8% 限價」確保盤中能被吃單
        sell_price = round(price * 0.92, 2)
        try:
            contract = api.Contracts.Stocks[code]
            order = api.Order(
                price=sell_price,
                quantity=qty,
                action=sj.constant.Action.Sell,
                price_type=sj.constant.StockPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                order_lot=sj.constant.StockOrderLot.IntradayOdd,
                account=api.stock_account,
            )
            trade = api.place_order(contract, order)
            op_code = getattr(getattr(trade, "operation", None), "op_code", "00")
            if op_code == "00":
                print(f"  ✓ {code} 賣單 {qty:,} 股 @ {sell_price} 已送出")
                success += 1
            else:
                op_msg = getattr(getattr(trade, "operation", None), "op_msg", "")
                print(f"  ❌ {code} 拒單 op_code={op_code} {op_msg}")
                fail += 1
        except Exception as e:
            print(f"  ❌ {code} 送單失敗: {type(e).__name__}: {e}")
            fail += 1
        time.sleep(0.5)  # 避免觸發 50req/5s rate limit

    print()
    print(f"送單結果：成功 {success} 檔、失敗 {fail} 檔")

    # ── 等待撮合 + 再查 ─────────────────────────────────
    print()
    print("[5/5] 等待 15 秒讓交易所撮合...")
    time.sleep(15)

    try:
        positions_after = api.list_positions(
            api.stock_account,
            unit=sj.constant.Unit.Share,
        )
    except Exception as e:
        print(f"⚠️ 殘餘查詢失敗: {e}")
        positions_after = positions

    if not positions_after:
        print("✅ 全部清除完成！模擬戶持倉已歸零。")
    else:
        print(f"⚠️ 仍剩 {len(positions_after)} 檔未完全清掉：")
        for p in positions_after:
            print(f"  {p.code}  剩 {int(p.quantity):,} 股")
        print()
        print("可能原因：")
        print("  - 非盤中時段（盤後零股無法撮合）→ 明早 09:00-13:25 再跑一次")
        print("  - 流動性差，限價沒人吃 → 隔天市場開盤後會被撮掉")
        print("  - 部分模擬戶配發的歷史單，撮合機制不同")

    api.logout()
    print()
    print("已登出 Shioaji。")


if __name__ == "__main__":
    main()
