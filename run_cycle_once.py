#!/usr/bin/env python3
"""Один полный цикл парсинга из командной строки.

Нужен для системных планировщиков (cron, systemd timer, Планировщик задач
Windows): они вызывают скрипт в 07:00, скрипт выполняет цикл и завершается.

Примеры::

    python run_cycle_once.py                # полный цикл
    python run_cycle_once.py --check        # показать расписание и выйти
    python run_cycle_once.py --step orders  # только проверка заказов
    python run_cycle_once.py --force        # выполнить, даже если автозапуск выключен

Код возврата: 0 — успех или частичный успех, 1 — ошибка, 2 — цикл уже идёт.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=PROJECT_ROOT / "System" / ".env")

from logger_config import logger  # noqa: E402
from services.cycle_runner import run_cycle  # noqa: E402
from services.parse_schedule import describe, format_moment, load_schedule, next_run  # noqa: E402

STEPS = {
    "refresh_sklad": "Обновление склада",
    "stocks": "Остатки на маркетплейсах",
    "orders": "Новые заказы",
    "prices": "Цены на маркетплейсах",
}


def run_single_step(step: str) -> int:
    if step == "refresh_sklad":
        from services.cycle_runner import refresh_sklad_task

        refresh_sklad_task()
    elif step == "stocks":
        from services.marketplace_cycle import run_stock_updates

        if not run_stock_updates():
            logger.warning("⚠️ Не все остатки опубликованы")
            return 1
    elif step == "orders":
        from order_notifications import check_for_new_orders

        check_for_new_orders()
    elif step == "prices":
        from price_updater_master import update_all_prices

        update_all_prices()
    else:  # pragma: no cover - argparse ограничивает значения
        raise ValueError(step)
    logger.success(f"✅ Шаг «{STEPS[step]}» выполнен")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Один цикл парсинга маркетплейсов")
    parser.add_argument("--check", action="store_true", help="показать расписание и выйти")
    parser.add_argument("--force", action="store_true", help="игнорировать выключенное расписание")
    parser.add_argument(
        "--step",
        choices=sorted(STEPS),
        help="выполнить только один шаг цикла",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="напечатать результат в JSON (удобно для мониторинга)",
    )
    arguments = parser.parse_args()

    schedule = load_schedule()

    if arguments.check:
        payload = {
            "schedule": schedule,
            "description": describe(schedule),
            "next_run": format_moment(next_run(schedule)),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if arguments.step:
        return run_single_step(arguments.step)

    result = run_cycle("cli", check_schedule=not arguments.force)

    if arguments.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"Результат: {result.get('status')} — {result.get('message')} "
            f"({result.get('duration_seconds', '?')} с)"
        )

    if result.get("status") == "skipped_locked":
        return 2
    return 0 if result.get("status") in {"success", "partial", "disabled"} else 1


if __name__ == "__main__":
    sys.exit(main())
