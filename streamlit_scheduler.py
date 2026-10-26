"""Планировщик ежедневного парсинга для Streamlit-приложения.

Планировщик живёт в том же процессе, что и Streamlit (BackgroundScheduler),
поэтому отдельный cron не обязателен: достаточно, чтобы приложение было
запущено. Расписание читается из ``System/parse_schedule.json`` и по умолчанию
означает «каждый день в 07:00 по Москве».

Дополнительно реализован «догон»: если приложение было выключено в момент
планового запуска, цикл выполнится при следующем старте (настройка
``run_if_missed``).

Если нужно, чтобы парсинг шёл даже при закрытом Streamlit, используйте
``run_cycle_once.py`` + cron/systemd timer — см. README_STREAMLIT.md.
"""

from __future__ import annotations

import atexit
from datetime import datetime
from threading import Lock, Thread

from apscheduler.schedulers.background import BackgroundScheduler

from logger_config import logger
from services.automation_state import load_automation_state
from services.cycle_runner import prepare_process, run_cycle
from services.parse_schedule import (
    cron_kwargs,
    format_moment,
    load_schedule,
    missed_run_since,
    next_run,
    schedule_timezone,
)

JOB_ID = "daily-parse"

_scheduler: BackgroundScheduler | None = None
_start_lock = Lock()
_shutdown_registered = False
_prepared = False


def _scheduled_job() -> None:
    logger.info("⏰ Плановый запуск: ежедневный парсинг маркетплейсов")
    result = run_cycle("scheduler")
    logger.info(
        f"⏰ Плановый запуск завершён: {result.get('status')} — {result.get('message')}"
    )


def _build_scheduler(schedule: dict) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=schedule_timezone(schedule),
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,  # опоздание до часа всё ещё выполняется
        },
    )
    scheduler.start()
    return scheduler


def get_scheduler() -> BackgroundScheduler:
    """Вернуть (при необходимости — создать) работающий планировщик."""
    global _scheduler, _shutdown_registered, _prepared

    if _scheduler is not None and _scheduler.running:
        return _scheduler

    with _start_lock:
        if _scheduler is not None and _scheduler.running:
            return _scheduler

        if not _prepared:
            prepare_process()
            _prepared = True

        schedule = load_schedule()
        _scheduler = _build_scheduler(schedule)
        apply_schedule(schedule)

        if not _shutdown_registered:
            atexit.register(shutdown_scheduler)
            _shutdown_registered = True

        logger.info(
            "📅 Планировщик Streamlit запущен: "
            f"{('каждый день' if len(schedule['weekdays']) == 7 else ','.join(schedule['weekdays']))} "
            f"в {schedule['time']} ({schedule['timezone']})"
        )

    start_catch_up_if_needed()
    return _scheduler


def apply_schedule(schedule: dict | None = None) -> dict:
    """Пересоздать cron-задачу по актуальному расписанию."""
    schedule = schedule or load_schedule()
    scheduler = _scheduler

    if scheduler is None or not scheduler.running:
        return schedule

    existing = scheduler.get_job(JOB_ID)
    if existing is not None:
        scheduler.remove_job(JOB_ID)

    if not schedule["enabled"]:
        logger.info("⏸ Расписание выключено — задача не создана")
        return schedule

    scheduler.add_job(
        _scheduled_job,
        "cron",
        id=JOB_ID,
        replace_existing=True,
        **cron_kwargs(schedule),
    )
    upcoming = next_run(schedule)
    logger.info(f"🗓 Задача парсинга запланирована: ближайший запуск {format_moment(upcoming)}")
    return schedule


def get_next_run_time() -> datetime | None:
    if _scheduler is None or not _scheduler.running:
        return None
    job = _scheduler.get_job(JOB_ID)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time


def next_run_display() -> str:
    moment = get_next_run_time()
    if moment is not None:
        return format_moment(moment)
    schedule = load_schedule()
    return format_moment(next_run(schedule)) if schedule["enabled"] else "автозапуск выключен"


def start_catch_up_if_needed() -> bool:
    """Догнать пропущенный плановый запуск (например, ПК был выключен утром)."""
    schedule = load_schedule()
    state = load_automation_state()
    last_success = state.get("last_finished") if state.get("last_result") == "success" else None
    missed = missed_run_since(schedule, last_success)
    if missed is None:
        return False

    logger.warning(
        f"⏱ Пропущен плановый запуск {format_moment(missed)} → выполняю догон"
    )

    def _runner() -> None:
        run_cycle("catch-up", check_schedule=False)

    Thread(target=_runner, name="parser-catch-up", daemon=True).start()
    return True


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        try:
            _scheduler.shutdown(wait=False)
            logger.info("📴 Планировщик остановлен")
        except Exception as error:  # noqa: BLE001
            logger.warning(f"⚠️ Ошибка остановки планировщика: {error}")
    _scheduler = None
