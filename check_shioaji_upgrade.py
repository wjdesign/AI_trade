"""Shioaji 升版冒煙測試 —— 放寬 requirements 上界前，先在本地跑一遍。

背景
----
requirements.txt / pyproject.toml 目前把 shioaji 鎖在 <1.7，因為 1.7.0 移除了
login(fetch_contract=..., contracts_timeout=..., contracts_cb=...) 參數，直接升會讓
bot.py 登入拋 TypeError，並可能重踩 GitHub Actions runner 上 fetch_contracts 的
"exclusive access lost (concurrent API call started)" 崩潰。

bot.py 的 _login_with_contracts() 已能相容新舊簽名，但「能不能連上永豐、能不能抓到
合約」只有實測才知道。這支腳本就是那個實測：過了才放寬 CI 上界。

用法
----
    # 先在「隔離」環境裝最新版，別汙染專案環境
    python -m venv .venv_sjnext
    .venv_sjnext\\Scripts\\pip install -U shioaji python-dotenv
    .venv_sjnext\\Scripts\\python check_shioaji_upgrade.py

需要 .env 內的 API_KEY / SECRET_KEY（CA 為選測，需 CA_CERT_PATH / CA_PASSWORD）。
一律用 simulation=True，不動用真實資金。

判讀
----
    exit 0 → 登入 + 合約抽樣通過，可以考慮把 requirements 上界放寬到涵蓋此版本。
    exit 1 → 有問題，維持 <1.7 鎖版，把輸出貼回來一起看。
"""

import inspect
import os
import sys

import shioaji as sj
from dotenv import load_dotenv

# 用幾檔 PINNED_STOCKS 當合約抽樣目標（與 bot.py 一致的驗證邏輯）
SAMPLE_CODES = ("2330", "2454", "2317", "2603", "2379")


def main() -> int:
    load_dotenv()

    ver = getattr(sj, "__version__", "unknown")
    print("=" * 60)
    print(f"[冒煙測試] Shioaji 版本：{ver}")

    login_params = inspect.signature(sj.Shioaji.login).parameters
    has_fetch_contract = "fetch_contract" in login_params
    print(f"[冒煙測試] login 支援 fetch_contract：{has_fetch_contract} "
          f"({'舊簽名 <1.7 路徑' if has_fetch_contract else '新簽名 1.7+ 路徑'})")

    api_key = os.environ.get("API_KEY", "").strip()
    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        print("[冒煙測試] ❌ .env 缺 API_KEY / SECRET_KEY，無法測登入")
        return 1

    api = sj.Shioaji(simulation=True)
    print("[冒煙測試] Shioaji 實例建立完成（simulation=True）")

    def _cb(security_type):
        print(f"[冒煙測試][contracts_cb] {security_type} 載入完成")

    # ---- 登入 + 合約下載（相容新舊簽名，與 bot._login_with_contracts 同邏輯）----
    try:
        if has_fetch_contract:
            accounts = api.login(
                api_key=api_key, secret_key=secret_key,
                fetch_contract=True, contracts_timeout=30000, contracts_cb=_cb,
            )
        else:
            accounts = api.login(api_key=api_key, secret_key=secret_key)
            print("[冒煙測試] 呼叫 fetch_contracts()...")
            api.fetch_contracts(
                contract_download=True, contracts_timeout=30000, contracts_cb=_cb,
            )
    except Exception as e:
        print(f"[冒煙測試] ❌ 登入/抓合約失敗：{type(e).__name__}: {e}")
        return 1

    print(f"[冒煙測試] 登入成功，帳戶數：{len(accounts)}")

    # ---- 合約抽樣驗證 ----
    ok = 0
    for code in SAMPLE_CODES:
        try:
            if api.Contracts.Stocks[code] is not None:
                ok += 1
        except Exception as e:
            print(f"[冒煙測試] 取得 {code} 失敗：{type(e).__name__}: {e}")
    print(f"[冒煙測試] 合約抽樣：{ok}/{len(SAMPLE_CODES)} 檔可取得")

    # ---- CA 憑證啟用（選測）----
    ca_path = os.environ.get("CA_CERT_PATH", "").strip()
    ca_pass = os.environ.get("CA_PASSWORD", "").strip()
    if ca_path and ca_pass and os.path.exists(ca_path):
        try:
            api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)
            print("[冒煙測試] CA 憑證啟用成功")
        except Exception as e:
            print(f"[冒煙測試] ⚠️ CA 憑證啟用失敗（不影響合約結論）：{type(e).__name__}: {e}")
    else:
        print("[冒煙測試] （略過 CA 測試：.env 未設 CA_CERT_PATH/CA_PASSWORD 或檔案不存在）")

    try:
        api.logout()
    except Exception:
        pass

    passed = ok >= 3
    print("=" * 60)
    if passed:
        print(f"[冒煙測試] ✅ 通過。此版本（{ver}）登入與合約皆正常，")
        print("[冒煙測試]    可考慮把 requirements.txt / pyproject.toml 的 shioaji 上界放寬涵蓋它。")
    else:
        print(f"[冒煙測試] ❌ 未通過（合約抽樣 {ok}/{len(SAMPLE_CODES)}）。維持 shioaji<1.7 鎖版。")
    print("=" * 60)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
