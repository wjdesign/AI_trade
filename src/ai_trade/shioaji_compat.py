"""Compatibility helpers for Shioaji API changes."""

from __future__ import annotations

import inspect
from typing import Any, Callable


def login_with_compatible_kwargs(
    api: Any,
    *,
    api_key: str,
    secret_key: str,
    fetch_contract: bool | None = None,
    contracts_timeout: int | None = None,
    contracts_cb: Callable[[Any], None] | None = None,
    logger: Callable[[str], None] | None = None,
) -> Any:
    """Call ``api.login`` while tolerating Shioaji keyword changes."""
    base_kwargs = {
        "api_key": api_key,
        "secret_key": secret_key,
    }
    optional_kwargs = {
        "fetch_contract": fetch_contract,
        "contracts_timeout": contracts_timeout,
        "contracts_cb": contracts_cb,
    }
    optional_kwargs = {k: v for k, v in optional_kwargs.items() if v is not None}

    kwargs = dict(base_kwargs)
    unsupported: list[str] = []

    try:
        parameters = inspect.signature(api.login).parameters
    except (TypeError, ValueError):
        parameters = {}

    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )

    if accepts_var_kwargs or not parameters:
        kwargs.update(optional_kwargs)
    else:
        for name, value in optional_kwargs.items():
            if name in parameters:
                kwargs[name] = value
            else:
                unsupported.append(name)

    if unsupported and logger:
        logger(
            "[初始化] 偵測到目前 Shioaji login() 不支援參數："
            + ", ".join(unsupported)
            + "；改用相容模式忽略。"
        )

    try:
        return api.login(**kwargs)
    except TypeError as exc:
        if kwargs == base_kwargs or not optional_kwargs:
            raise
        if logger:
            logger("[初始化] login() 參數與目前 Shioaji 版本不相容，改用基本登入參數重試。")
        return api.login(**base_kwargs)
