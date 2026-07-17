from __future__ import annotations

import unittest

from src.ai_trade.shioaji_compat import login_with_compatible_kwargs


class LoginCompatibilityTests(unittest.TestCase):
    def test_passes_optional_login_kwargs_when_supported(self) -> None:
        class FakeAPI:
            def login(
                self,
                *,
                api_key: str,
                secret_key: str,
                fetch_contract: bool,
                contracts_timeout: int,
                contracts_cb,
            ):
                return {
                    "api_key": api_key,
                    "secret_key": secret_key,
                    "fetch_contract": fetch_contract,
                    "contracts_timeout": contracts_timeout,
                    "contracts_cb": contracts_cb,
                }

        def callback(*_: object) -> None:
            pass

        result = login_with_compatible_kwargs(
            FakeAPI(),
            api_key="key",
            secret_key="secret",
            fetch_contract=True,
            contracts_timeout=30000,
            contracts_cb=callback,
        )

        self.assertTrue(result["fetch_contract"])
        self.assertEqual(result["contracts_timeout"], 30000)
        self.assertIs(result["contracts_cb"], callback)

    def test_preserves_explicit_false_optional_kwargs(self) -> None:
        class FakeAPI:
            def login(self, *, api_key: str, secret_key: str, fetch_contract: bool):
                return {
                    "api_key": api_key,
                    "secret_key": secret_key,
                    "fetch_contract": fetch_contract,
                }

        result = login_with_compatible_kwargs(
            FakeAPI(),
            api_key="key",
            secret_key="secret",
            fetch_contract=False,
        )

        self.assertFalse(result["fetch_contract"])

    def test_ignores_unsupported_login_kwargs_when_running_newer_shioaji(self) -> None:
        class FakeAPI:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def login(self, *, api_key: str, secret_key: str):
                kwargs = {
                    "api_key": api_key,
                    "secret_key": secret_key,
                }
                self.calls.append(kwargs)
                return kwargs

        api = FakeAPI()
        logged_warnings: list[str] = []

        def callback(*_: object) -> None:
            pass

        result = login_with_compatible_kwargs(
            api,
            api_key="key",
            secret_key="secret",
            fetch_contract=True,
            contracts_timeout=30000,
            contracts_cb=callback,
            logger=logged_warnings.append,
        )

        self.assertEqual(result, {"api_key": "key", "secret_key": "secret"})
        self.assertEqual(api.calls, [{"api_key": "key", "secret_key": "secret"}])
        self.assertTrue(logged_warnings)
        self.assertIn("不支援參數", logged_warnings[0])
        self.assertNotIn("contracts_cb", api.calls[0])


if __name__ == "__main__":
    unittest.main()
