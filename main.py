"""
Shioaji 連線 / 下單 / 帳戶狀態測試腳本

分四階段，每階段透過 .env 開關獨立控制，預設只跑「登入 + CA 啟用」最安全的測試。
未通過的階段印警告但不中斷，方便逐項偵錯。

.env 開關：
    ENABLE_STOCK_TEST=false   # 模擬下證券單測試（模擬戶內安全）
    ENABLE_FUTURES_TEST=false # 模擬下期貨單測試（需有期貨帳戶）
    ENABLE_PROD_TEST=false    # 連正式環境查 API 測試狀態（需 Key 勾「正式環境」權限）

預設全 false → 只跑階段 1 與帳戶資訊列印。
"""

import os
import shioaji as sj
from dotenv import load_dotenv

load_dotenv()


def _flag(name: str) -> bool:
    """讀取 .env 旗標，預設 false（最保守）"""
    return os.environ.get(name, "").strip().lower() in {"true", "1", "yes"}


ENABLE_STOCK_TEST   = _flag("ENABLE_STOCK_TEST")
ENABLE_FUTURES_TEST = _flag("ENABLE_FUTURES_TEST")
ENABLE_PROD_TEST    = _flag("ENABLE_PROD_TEST")


# ─────────────────────────────────────────────────────────────
# 階段 1：登入 + CA 啟用（必跑）
# ─────────────────────────────────────────────────────────────
print("=" * 60)
print("  階段 1：登入 + CA 啟用")
print("=" * 60)

api = sj.Shioaji(simulation=True)
accounts = api.login(
    api_key=os.environ["API_KEY"],
    secret_key=os.environ["SECRET_KEY"],
)
print(f"[OK] 登入成功，回傳 {len(accounts)} 個帳戶")
for i, acc in enumerate(accounts):
    print(f"  [{i}] account_type={acc.account_type} "
          f"account_id={acc.account_id} signed={getattr(acc, 'signed', 'n/a')}")

# 預設帳戶設為第一個（簡化後續呼叫）
api.set_default_account(accounts[0])

api.activate_ca(
    ca_path=os.environ["CA_CERT_PATH"],
    ca_passwd=os.environ["CA_PASSWORD"],
)
print("[OK] CA 啟用成功")
print(f"[OK] shioaji 版本：{sj.__version__}")


# ─────────────────────────────────────────────────────────────
# 階段 2：證券下單測試（買 2890 中信金 1 張 @ 18 元，模擬戶內）
# ─────────────────────────────────────────────────────────────
if ENABLE_STOCK_TEST:
    print("\n" + "=" * 60)
    print("  階段 2：證券下單測試（ENABLE_STOCK_TEST=true）")
    print("=" * 60)
    try:
        stock_contract = api.Contracts.Stocks.TSE["2890"]
        stock_order = api.Order(
            price=18,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            account=api.stock_account,
        )
        stock_trade = api.place_order(stock_contract, stock_order)
        print(f"[OK] 證券下單回傳：{stock_trade}")
    except Exception as e:
        print(f"[FAIL] 證券下單失敗：{e}")
else:
    print("\n[SKIP] 階段 2 證券下單測試（ENABLE_STOCK_TEST=false）")


# ─────────────────────────────────────────────────────────────
# 階段 3：期貨下單測試（需有期貨帳戶）
# ─────────────────────────────────────────────────────────────
if ENABLE_FUTURES_TEST:
    print("\n" + "=" * 60)
    print("  階段 3：期貨下單測試（ENABLE_FUTURES_TEST=true）")
    print("=" * 60)
    try:
        futures_contract = min(
            [x for x in api.Contracts.Futures.TXF
             if x.code[-2:] not in ["R1", "R2"]],
            key=lambda x: x.delivery_date,
        )
        futures_order = api.Order(
            action=sj.constant.Action.Buy,
            price=15000,
            quantity=1,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Auto,
            account=accounts[0],
        )
        futures_trade = api.place_order(futures_contract, futures_order)
        print(f"[OK] 期貨下單回傳：{futures_trade}")
    except Exception as e:
        print(f"[FAIL] 期貨下單失敗（沒申請期貨帳戶就會掛在這）：{e}")
else:
    print("\n[SKIP] 階段 3 期貨下單測試（ENABLE_FUTURES_TEST=false）")

api.logout()


# ─────────────────────────────────────────────────────────────
# 階段 4：正式環境連線測試（需 API Key 勾「正式環境」權限）
# ─────────────────────────────────────────────────────────────
if ENABLE_PROD_TEST:
    print("\n" + "=" * 60)
    print("  階段 4：正式環境連線測試（ENABLE_PROD_TEST=true）")
    print("=" * 60)
    try:
        api_prod = sj.Shioaji(simulation=False)
        prod_accounts = api_prod.login(
            api_key=os.environ["API_KEY"],
            secret_key=os.environ["SECRET_KEY"],
        )
        for acc in prod_accounts:
            signed = getattr(acc, "signed", None)
            status = ("[PASS] 已通過 API 測試"
                      if signed else "[FAIL] 未通過 API 測試（請等審核約 5 分鐘）")
            print(f"  帳戶 {acc.account_id} ({acc.account_type}): {status}")
        api_prod.logout()
    except Exception as e:
        print(f"[FAIL] 正式環境連線失敗（Key 未勾「正式環境」權限就會掛在這）：{e}")
else:
    print("\n[SKIP] 階段 4 正式環境測試（ENABLE_PROD_TEST=false）")


print("\n" + "=" * 60)
print("  測試完成")
print("=" * 60)
