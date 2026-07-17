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
            return None

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
        messages: list[str] = []

        def callback(*_: object) -> None:
            return None

        result = login_with_compatible_kwargs(
            api,
            api_key="key",
            secret_key="secret",
            fetch_contract=True,
            contracts_timeout=30000,
            contracts_cb=callback,
            logger=messages.append,
        )

        self.assertEqual(result, {"api_key": "key", "secret_key": "secret"})
        self.assertEqual(api.calls, [{"api_key": "key", "secret_key": "secret"}])
        self.assertTrue(messages)
        self.assertIn("不支援參數", messages[0])


if __name__ == "__main__":
    unittest.main()
