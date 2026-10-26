from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
load_dotenv(dotenv_path=PROJECT_ROOT / "System" / ".env")

from logger_config import logger
from services.marketplace_cycle import run_marketplace_cycle, send_telegram_message


def handle_exit_signal(signum, frame) -> None:
    logger.info("📴 Получен сигнал завершения")
    try:
        send_telegram_message("Программа завершена❗️")
    except Exception as error:
        logger.warning(
            "Не удалось отправить Telegram-уведомление о завершении: "
            f"{type(error).__name__}: {error}"
        )
    raise SystemExit(0)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, handle_exit_signal)
    signal.signal(signal.SIGINT, handle_exit_signal)


def main() -> dict:
    """Ручной запуск цикла без запуска веб-приложения."""

    # ШАГ 1. ПОДГОТОВКА РУЧНОГО ЗАПУСКА
    logger.info("🚀 Ручной запуск фонового цикла из main.py")

    # ШАГ 2. ЗАПУСК ЕДИНОГО ЦИКЛА СИНХРОНИЗАЦИИ
    result = run_marketplace_cycle()

    # ШАГ 3. ВЫВОД ИТОГОВ РУЧНОГО ЗАПУСКА
    logger.info(
        f"📋 Результат ручного запуска: "
        f"{result.get('status')} — {result.get('message')}"
    )
    return result


if __name__ == "__main__":
    install_signal_handlers()
    main()
