from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from logger_config import logger
from services.automation_state import acquire_cycle_lock
from services.control_store import set_process_status


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLAGS_PATH = PROJECT_ROOT / "System" / "stock_flags.json"

STOCK_FIELD_NAMES = {
    "нал",
    "наличие",
    "остаток",
    "остатки",
    "quantity",
    "quantities",
    "stock",
    "available",
    "available_stock",
    "amount",
    "count",
}

def report_process_status(code: str, status: str, message: str) -> None:
    try:
        set_process_status(code, status, message)
    except Exception as error:
        logger.warning(
            f"⚠️ Не удалось сохранить состояние процесса {code}: "
            f"{type(error).__name__}: {error}"
        )


def load_stock_flags() -> dict:
    try:
        with FLAGS_PATH.open("r", encoding="utf-8") as stream:
            flags = json.load(stream)
    except Exception as error:
        logger.warning(
            f"⚠️ Не удалось прочитать {FLAGS_PATH}: {error}. "
            "Используем все маркетплейсы ON."
        )
        flags = {}

    flags.setdefault("wildberries", True)
    flags.setdefault("yandex", True)
    flags.setdefault("ozon", True)
    flags.setdefault("suppliers", {})
    return flags


def is_stock_field(field_name) -> bool:
    normalized = str(field_name).strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in STOCK_FIELD_NAMES


def _zero_stock_payload(payload):
    if hasattr(payload, "copy") and hasattr(payload, "columns"):
        result = payload.copy()
        stock_columns = [column for column in result.columns if is_stock_field(column)]
        for column in stock_columns:
            result[column] = 0
        return result, len(stock_columns)

    if isinstance(payload, dict):
        result = {}
        changed = 0
        for key, value in payload.items():
            if isinstance(value, (dict, list, tuple)):
                result[key], nested_changed = _zero_stock_payload(value)
                changed += nested_changed
            elif is_stock_field(key):
                result[key] = 0
                changed += 1
            else:
                result[key] = value
        return result, changed

    if isinstance(payload, list):
        result = []
        changed = 0
        for item in payload:
            new_item, item_changed = _zero_stock_payload(item)
            result.append(new_item)
            changed += item_changed
        return result, changed

    if isinstance(payload, tuple):
        result = []
        changed = 0
        for item in payload:
            new_item, item_changed = _zero_stock_payload(item)
            result.append(new_item)
            changed += item_changed
        return tuple(result), changed

    return payload, 0


def zero_stock_payload(payload, market: str):
    zeroed_payload, changed = _zero_stock_payload(payload)
    if changed == 0:
        raise RuntimeError(
            f"{market}: не найдено ни одного поля остатка для обнуления"
        )
    logger.info(
        f"🧯 {market.upper()} выключен → "
        f"перед отправкой обнулены поля остатков: {changed}"
    )
    return zeroed_payload


def send_zero_stocks_once(market: str) -> None:
    """Один раз отправляет нулевые остатки перед выключением маркетплейса."""
    from stock import gen_sklad, oz_update, wb_update, ym_update

    market_tasks = {
        "wildberries": (wb_update, 0),
        "yandex": (ym_update, 1),
        "ozon": (oz_update, 2),
    }
    if market not in market_tasks:
        raise ValueError(f"Неизвестный маркетплейс: {market}")

    logger.info(f"🧯 {market.upper()}: готовим финальное обнуление остатков")
    payloads = gen_sklad()
    update_func, payload_index = market_tasks[market]
    zero_payload = zero_stock_payload(payloads[payload_index], market)
    update_func(zero_payload)
    logger.success(
        f"✅ {market.upper()}: нулевые остатки отправлены, синхронизацию можно выключить"
    )


def send_telegram_message(message: str) -> None:
    import stock

    stock.telegram.notify(
        token=stock.telegram_got_token_error,
        chat_id=stock.telegram_chat_id_error,
        message=message,
    )


def run_safe(
    action_description: str,
    func: Callable,
    *,
    notify_on_error: bool = True,
) -> tuple[bool, object | None]:
    try:
        logger.info(f"▶ Начало: {action_description}")
        result = func()
        logger.success(f"✅ Завершено: {action_description}")
        return True, result
    except Exception as error:
        logger.error(
            f"❌ Ошибка при {action_description}: "
            f"{type(error).__name__}: {error}"
        )
        if notify_on_error:
            try:
                send_telegram_message(
                    f"😨 Ошибка при {action_description}: {error}"
                )
            except Exception as notification_error:
                logger.warning(
                    "Не удалось отправить Telegram-уведомление: "
                    f"{type(notification_error).__name__}: {notification_error}"
                )
        logger.warning(
            f"⚠️ {action_description}: операция пропущена, "
            "выполнение продолжается"
        )
        return False, None


def run_stock_updates() -> bool:
    from stock import gen_sklad, oz_update, wb_update, ym_update

    flags = load_stock_flags()
    enabled_markets = {
        market for market in ("wildberries", "yandex", "ozon")
        if bool(flags.get(market, True))
    }
    if not enabled_markets:
        logger.info("⏭ Остатки пропущены: все маркетплейсы выключены")
        return True

    try:
        logger.info("📦 Получаем складские остатки...")
        wb_data, ym_data, oz_data = gen_sklad()
        logger.success("✅ Остатки успешно получены")
    except Exception as error:
        logger.error(
            "❌ Ошибка при получении данных из БД: "
            f"{type(error).__name__}: {error}"
        )
        return False

    market_tasks = (
        ("wildberries", "обновлении WB", wb_update, wb_data),
        ("yandex", "обновлении YM", ym_update, ym_data),
        ("ozon", "обновлении OZ", oz_update, oz_data),
    )

    all_successful = True
    for market, description, update_func, payload in market_tasks:
        if market not in enabled_markets:
            logger.info(
                f"⏭ {market.upper()} выключен → отправка остатков пропущена"
            )
            continue

        successful, _ = run_safe(
            description,
            lambda update_func=update_func, payload=payload: update_func(payload),
            notify_on_error=False,
        )
        all_successful = all_successful and successful

    return all_successful


def run_marketplace_cycle(
    refresh_sklad: Callable | None = None,
    *,
    acquire_lock: bool = True,
) -> dict:
    """Выполняет один полный последовательный цикл YMWB."""

    started_at = time.monotonic()

    if acquire_lock:
        with acquire_cycle_lock() as lock_acquired:
            if not lock_acquired:
                logger.warning(
                    "⏭ Автоматический цикл пропущен: другой экземпляр уже работает"
                )
                return {
                    "status": "skipped_locked",
                    "message": "Другой экземпляр цикла уже выполняется",
                    "steps": {},
                    "duration_seconds": 0,
                }
            return run_marketplace_cycle(
                refresh_sklad,
                acquire_lock=False,
            )

    logger.info("🚀 Запущен единый цикл фоновой синхронизации")
    steps: dict[str, bool | None] = {}

    # ШАГ 1. ОБНОВЛЕНИЕ ЛОКАЛЬНОЙ БАЗЫ СКЛАДА
    if refresh_sklad is None:
        steps["refresh_sklad"] = None
        report_process_status("refresh_sklad", "skipped", "Функция обновления не передана")
        logger.info("⏭ Шаг 1 пропущен: функция обновления склада не передана")
    else:
        report_process_status("refresh_sklad", "running", "Обновляется локальная база склада")
        steps["refresh_sklad"], _ = run_safe(
            "обновлении локальной базы склада",
            refresh_sklad,
        )
        report_process_status(
            "refresh_sklad",
            "success" if steps["refresh_sklad"] else "error",
            "Локальная база склада обновлена" if steps["refresh_sklad"] else "Ошибка обновления склада",
        )

    # ШАГ 2. ОТПРАВКА ОСТАТКОВ НА МАРКЕТПЛЕЙСЫ
    report_process_status("stocks", "running", "Публикуются остатки маркетплейсов")
    steps["stocks"] = run_stock_updates()
    report_process_status(
        "stocks",
        "success" if steps["stocks"] else "error",
        "Остатки опубликованы" if steps["stocks"] else "Не все остатки опубликованы",
    )

    # ШАГ 3. ПРОВЕРКА И ОБРАБОТКА НОВЫХ ЗАКАЗОВ
    from order_notifications import check_for_new_orders

    report_process_status("orders", "running", "Проверяются новые заказы")
    steps["orders"], _ = run_safe(
        "проверке новых заказов маркетплейсов",
        check_for_new_orders,
    )
    report_process_status(
        "orders",
        "success" if steps["orders"] else "error",
        "Проверка заказов завершена" if steps["orders"] else "Ошибка проверки заказов",
    )

    # ШАГ 4. ОБНОВЛЕНИЕ ЦЕН НА МАРКЕТПЛЕЙСАХ
    from price_updater_master import update_all_prices

    report_process_status("prices", "running", "Публикуются цены маркетплейсов")
    steps["prices"], _ = run_safe(
        "обновлении всех цен",
        update_all_prices,
    )
    report_process_status(
        "prices",
        "success" if steps["prices"] else "error",
        "Цены опубликованы" if steps["prices"] else "Ошибка обновления цен",
    )

    # ШАГ 5. ФОРМИРОВАНИЕ ИТОГОВОГО СТАТУСА ЦИКЛА
    completed_steps = [value for value in steps.values() if value is not None]
    all_successful = bool(completed_steps) and all(completed_steps)
    status = "success" if all_successful else "partial"
    message = (
        "Все этапы завершены успешно"
        if all_successful
        else "Цикл завершён с временными ошибками отдельных этапов"
    )
    duration = round(time.monotonic() - started_at, 1)

    if all_successful:
        logger.success(f"🏁 {message}; время: {duration} сек.")
    else:
        logger.warning(f"🏁 {message}; время: {duration} сек.")

    return {
        "status": status,
        "message": message,
        "steps": steps,
        "duration_seconds": duration,
    }
