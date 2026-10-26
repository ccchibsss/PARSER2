"""HTTP-клиент проекта: обёртка над ``requests`` с таймаутами и повторами.

Модули ``stock.py``, ``price_updater_master.py``, ``order_notifications.py``
и ``ozon_actions.py`` импортируют его как ``from clients import http_client as
requests`` и вызывают ``get/post/put`` так же, как обычный ``requests``.

Отличия от «сырого» requests:

* всегда подставляется таймаут (по умолчанию 30 с), даже если вызывающий код
  его не передал — иначе один «зависший» ответ маркетплейса останавливает весь
  цикл синхронизации;
* запросы к API повторяются при сетевых ошибках и ответах 5xx/429
  (экспоненциальная пауза), но не повторяются при 4xx — это осмысленный отказ;
* все ошибки пишутся в общий лог Loguru.
"""

from __future__ import annotations

import time
from typing import Any

import requests as _requests
from requests import Response, Session  # noqa: F401  (реэкспорт для совместимости)

from logger_config import logger

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

__all__ = [
    "Response",
    "Session",
    "request",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "RequestException",
    "HTTPError",
    "ConnectionError",
    "Timeout",
]

RequestException = _requests.RequestException
HTTPError = _requests.HTTPError
ConnectionError = _requests.ConnectionError
Timeout = _requests.Timeout


def _redact_headers(headers: dict | None) -> dict:
    """Не пишем в лог токены целиком."""
    if not headers:
        return {}
    redacted = {}
    for key, value in headers.items():
        text = str(value)
        redacted[key] = f"{text[:4]}…{text[-4:]}" if len(text) > 12 else "***"
    return redacted


def request(
    method: str,
    url: str,
    *,
    timeout: float | tuple[float, float] | None = None,
    retries: int = DEFAULT_RETRIES,
    retry_on_status: bool = True,
    **kwargs: Any,
) -> Response:
    """Выполнить HTTP-запрос с таймаутом и повторами."""

    kwargs.setdefault("timeout", timeout or DEFAULT_TIMEOUT)
    attempts = max(1, int(retries))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = _requests.request(method, url, **kwargs)
        except (_requests.Timeout, _requests.ConnectionError) as error:
            last_error = error
            logger.warning(
                f"🌐 {method} {url}: попытка {attempt}/{attempts} не удалась — "
                f"{type(error).__name__}"
            )
            if attempt == attempts:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS ** attempt)
            continue

        if (
            retry_on_status
            and response.status_code in RETRY_STATUS_CODES
            and attempt < attempts
        ):
            logger.warning(
                f"🌐 {method} {url}: HTTP {response.status_code}, "
                f"повтор {attempt}/{attempts}"
            )
            time.sleep(RETRY_BACKOFF_SECONDS ** attempt)
            continue

        return response

    # Сюда попадаем только если все попытки исчерпаны сетевыми ошибками.
    assert last_error is not None
    raise last_error


def get(url: str, **kwargs: Any) -> Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    return request("POST", url, **kwargs)


def put(url: str, **kwargs: Any) -> Response:
    return request("PUT", url, **kwargs)


def patch(url: str, **kwargs: Any) -> Response:
    return request("PATCH", url, **kwargs)


def delete(url: str, **kwargs: Any) -> Response:
    return request("DELETE", url, **kwargs)


def head(url: str, **kwargs: Any) -> Response:
    return request("HEAD", url, **kwargs)
