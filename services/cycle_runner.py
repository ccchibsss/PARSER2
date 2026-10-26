"""Единый цикл синхронизации, независимый от Flask.

Полный цикл (тот же, что был в ``web_app.run_automation_cycle``):

1. обновление локальной базы склада (Google Sheets → SQLite);
2. публикация остатков на WB / Ozon / Яндекс.Маркет;
3. получение новых заказов + уведомления в Telegram;
4. обновление цен на маркетплейсах.

Модуль используется:

* планировщиком внутри Streamlit (``streamlit_scheduler.py``);
* CLI ``run_cycle_once.py`` (для cron / systemd timer);
* прежним Flask-интерфейсом.

Состояние цикла пишется в ``System/automation_state.json``, поэтому его видят
все интерфейсы сразу, а межпроцессная блокировка не даёт запустить два цикла
одновременно (например, по кнопке и по расписанию).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from logger_config import logger
from services.automation_state import (
    load_automation_state,
    now_iso,
    reset_interrupted_run,
    update_automation_state,
)
from services.control_store import (
    finish_process_run,
    init_control_store,
    record_cycle_steps,
    start_process_run,
)
from services.market_controls import load_flags, recompute_marketplace_core
from services.marketplace_cycle import run_marketplace_cycle
from utilities.atomic_files import atomic_write_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAST_UPDATE_FILE = PROJECT_ROOT / "System" / "last_update.txt"

_cycle_lock = Lock()


def refresh_sklad_task() -> None:
    """Обновить локальную базу склада из Google Sheets и пересчитать остатки."""
    from auto_stock_updater import update
    from update_sklad import gen_sklad, update_sklad_db, upsert_ymwb_prices_from_sklad

    try:
        logger.success("🔁 Обновление склада через update_sklad.py...")
        flags = load_flags()

        # 1) Свежие данные склада
        frame = gen_sklad()

        # 2) Синхронизация !YMWB.db/prices (Sklad): UPDATE/INSERT + DELETE
        upsert_ymwb_prices_from_sklad(frame)

        # 3) Обновление marketplace_base.db из склада (Нал/ОПТ/Цена)
        update_sklad_db(frame)

        # 4) Полный пересчёт выбора поставщика по всей базе
        update(flags)

        # 5) Точечный пересчёт по каждому маркетплейсу
        for market in ("yandex", "ozon", "wildberries"):
            recompute_marketplace_core(market, flags)

        stamp = datetime.now().strftime("%d.%m.%Y - %H:%M")
        atomic_write_text(LAST_UPDATE_FILE, stamp)
        logger.success("✅ Склад успешно обновлён.")
    except Exception:
        logger.exception("❌ Ошибка при обновлении склада")
        raise


def get_last_update_stamp() -> str | None:
    try:
        return LAST_UPDATE_FILE.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, OSError):
        return None


def run_cycle(trigger: str = "scheduler", *, check_schedule: bool | None = None) -> dict:
    """Выполнить один полный цикл синхронизации.

    :param trigger: ``scheduler``, ``manual`` или ``cli`` — попадает в журнал.
    :param check_schedule: если True (по умолчанию для ``scheduler``), цикл
        не выполняется, когда расписание выключено.
    """
    if check_schedule is None:
        check_schedule = trigger == "scheduler"

    if check_schedule:
        from services.parse_schedule import load_schedule

        schedule = load_schedule()
        if not schedule["enabled"]:
            logger.info("⏸ Расписание выключено — плановый запуск пропущен")
            return {"status": "disabled", "message": "Автоматический парсинг выключен", "steps": {}}

    if not _cycle_lock.acquire(blocking=False):
        logger.warning("⏭ Цикл уже выполняется в текущем процессе")
        return {"status": "skipped_locked", "message": "Цикл уже выполняется", "steps": {}}

    started_at = datetime.now()
    try:
        init_control_store()
        # Межпроцессная блокировка берётся внутри run_marketplace_cycle(),
        # поэтому второй (вложенный) захват здесь не нужен.
        run_id = start_process_run("marketplace_cycle", trigger)
        update_automation_state(
            running=True,
            last_started=now_iso(),
            last_result="running",
            last_message="Выполняется единый цикл синхронизации",
            trigger=trigger,
        )
        logger.info(f"🚀 Запуск цикла ({trigger})")

        try:
            result = run_marketplace_cycle(refresh_sklad=refresh_sklad_task)
            duration = result.get(
                "duration_seconds",
                round((datetime.now() - started_at).total_seconds(), 1),
            )
            update_automation_state(
                running=False,
                last_finished=now_iso(),
                last_result=result["status"],
                last_message=result["message"],
                last_duration_seconds=duration,
                trigger=trigger,
            )
            record_cycle_steps(run_id, result.get("steps", {}))
            finish_process_run(
                run_id,
                "marketplace_cycle",
                status=result["status"],
                message=result["message"],
                duration_seconds=duration,
                details={"steps": result.get("steps", {})},
            )
            return result

        except Exception as error:  # noqa: BLE001
            logger.error(
                "❌ Необработанная ошибка единого цикла: "
                f"{type(error).__name__}: {error}"
            )
            duration = round((datetime.now() - started_at).total_seconds(), 1)
            message = f"{type(error).__name__}: {error}"
            update_automation_state(
                running=False,
                last_finished=now_iso(),
                last_result="error",
                last_message=message,
                last_duration_seconds=duration,
                trigger=trigger,
            )
            finish_process_run(
                run_id,
                "marketplace_cycle",
                status="error",
                message=message,
                duration_seconds=duration,
            )
            return {"status": "error", "message": message, "steps": {}}
    finally:
        _cycle_lock.release()


def prepare_process() -> None:
    """Подготовка перед стартом планировщика: индексы, журнал, сброс «зависших»."""
    from db.schema import ensure_indexes
    from services.market_controls import DB_PATH, SUPPLIER_DB_PATH

    try:
        ensure_indexes(DB_PATH, SUPPLIER_DB_PATH)
    except Exception as error:  # noqa: BLE001
        logger.warning(f"⚠️ Не удалось проверить индексы БД: {type(error).__name__}: {error}")
    init_control_store()
    reset_interrupted_run()


def automation_status() -> dict:
    """Статус автоматизации для интерфейса (без привязки к планировщику)."""
    state = load_automation_state()
    state["last_update_stamp"] = get_last_update_stamp()
    return state
