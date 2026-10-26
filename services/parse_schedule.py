"""Расписание ежедневного парсинга (по умолчанию — каждый день в 07:00).

Настройки хранятся в ``System/parse_schedule.json`` и редактируются прямо из
Streamlit. Модуль не зависит от APScheduler: он умеет только читать/проверять
настройки и считать ближайшие запуски, поэтому его удобно использовать и в
CLI (``run_cycle_once.py``), и в планировщике внутри Streamlit.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path

import pytz

from logger_config import logger
from utilities.atomic_files import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = PROJECT_ROOT / "System" / "parse_schedule.json"

DAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}
# Соответствие «день недели Python → ключ APScheduler»
PYTHON_WEEKDAY_TO_KEY = dict(enumerate(DAY_ORDER))

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_TIME = "07:00"

DEFAULT_SCHEDULE = {
    "enabled": True,
    "time": DEFAULT_TIME,
    "weekdays": list(DAY_ORDER),  # каждый день
    "timezone": DEFAULT_TIMEZONE,
    "run_if_missed": True,  # догнать запуск, пропущенный из-за простоя
}


class ScheduleConfigError(ValueError):
    """Некорректные настройки расписания."""


def _normalise_time(value) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as error:
        raise ScheduleConfigError("Время должно быть в формате ЧЧ:ММ, например 07:00") from error
    return parsed.strftime("%H:%M")


def _normalise_weekdays(value) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple, set)):
        raise ScheduleConfigError("Дни недели должны быть списком")

    days: list[str] = []
    for raw in value:
        key = str(raw).strip().lower()
        if key in {"daily", "everyday", "все"}:
            days = list(DAY_ORDER)
            break
        if key not in DAY_LABELS:
            raise ScheduleConfigError(f"Неизвестный день недели: {raw}")
        if key not in days:
            days.append(key)

    if not days:
        raise ScheduleConfigError("Выберите хотя бы один день недели")
    return [day for day in DAY_ORDER if day in days]


def _normalise_timezone(value) -> str:
    name = str(value or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        pytz.timezone(name)
    except pytz.UnknownTimeZoneError as error:
        raise ScheduleConfigError(f"Неизвестный часовой пояс: {name}") from error
    return name


def validate_schedule(raw: object) -> dict:
    if isinstance(raw, str):
        raw = {"time": raw}
    if not isinstance(raw, dict):
        raise ScheduleConfigError("Настройки расписания должны быть объектом")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ScheduleConfigError("Поле enabled должно быть логическим")

    return {
        "enabled": enabled,
        "time": _normalise_time(raw.get("time", DEFAULT_TIME)),
        "weekdays": _normalise_weekdays(raw.get("weekdays", DEFAULT_SCHEDULE["weekdays"])),
        "timezone": _normalise_timezone(raw.get("timezone", DEFAULT_TIMEZONE)),
        "run_if_missed": bool(raw.get("run_if_missed", True)),
    }


def load_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        return dict(DEFAULT_SCHEDULE, weekdays=list(DAY_ORDER))
    try:
        with SCHEDULE_PATH.open("r", encoding="utf-8") as stream:
            return validate_schedule(json.load(stream))
    except (OSError, ValueError, TypeError) as error:
        logger.warning(f"⚠️ Не удалось прочитать расписание {SCHEDULE_PATH}: {error}")
        return dict(DEFAULT_SCHEDULE, weekdays=list(DAY_ORDER))


def save_schedule(raw: object) -> dict:
    schedule = validate_schedule(raw)
    atomic_write_json(SCHEDULE_PATH, schedule)
    logger.info(
        "🗓 Расписание сохранено: "
        f"{'вкл' if schedule['enabled'] else 'выкл'}, {schedule['time']}, "
        f"дни {','.join(schedule['weekdays'])}, {schedule['timezone']}"
    )
    return schedule


def cron_kwargs(schedule: dict) -> dict:
    """Параметры для ``scheduler.add_job(..., 'cron', **kwargs)``."""
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    weekdays = schedule["weekdays"]
    return {
        "hour": hour,
        "minute": minute,
        "day_of_week": ",".join(weekdays) if len(weekdays) < 7 else "*",
    }


def schedule_timezone(schedule: dict):
    return pytz.timezone(schedule["timezone"])


def next_run(schedule: dict, now: datetime | None = None) -> datetime | None:
    """Ближайший плановый запуск (в часовом поясе расписания)."""
    if not schedule["enabled"]:
        return None

    tz = schedule_timezone(schedule)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    hour, minute = (int(part) for part in schedule["time"].split(":"))

    for offset in range(0, 8):
        day = (current + timedelta(days=offset)).date()
        day_key = PYTHON_WEEKDAY_TO_KEY[day.weekday()]
        if day_key not in schedule["weekdays"]:
            continue
        candidate = tz.localize(datetime.combine(day, time(hour=hour, minute=minute)))
        if candidate > current:
            return candidate
    return None


def upcoming_runs(schedule: dict, count: int = 3, now: datetime | None = None) -> list[datetime]:
    runs: list[datetime] = []
    current = now
    for _ in range(count):
        moment = next_run(schedule, current)
        if moment is None:
            break
        runs.append(moment)
        # сдвигаемся на минуту вперёд, чтобы получить следующий запуск
        current = moment + timedelta(minutes=1)
    return runs


def describe(schedule: dict) -> str:
    if not schedule["enabled"]:
        return "Автоматический парсинг выключен"
    days = "каждый день" if len(schedule["weekdays"]) == 7 else ", ".join(
        DAY_LABELS[day] for day in schedule["weekdays"]
    )
    return f"{days} в {schedule['time']} ({schedule['timezone']})"


def format_moment(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return moment.strftime("%d.%m.%Y %H:%M")


def missed_run_since(schedule: dict, last_success_iso: str | None, now: datetime | None = None) -> datetime | None:
    """Вернуть плановый запуск, который был пропущен и ещё не «догнан».

    Нужен для случая «компьютер был выключен в 07:00»: при следующем старте
    Streamlit мы сравниваем ближайший плановый запуск в прошлом с временем
    последнего успешного цикла. Если успешных запусков ещё не было, догон
    не выполняется.
    """
    if not schedule["enabled"] or not schedule["run_if_missed"]:
        return None

    tz = schedule_timezone(schedule)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    hour, minute = (int(part) for part in schedule["time"].split(":"))

    pending: datetime | None = None
    for offset in range(0, 8):
        day = (current - timedelta(days=offset)).date()
        day_key = PYTHON_WEEKDAY_TO_KEY[day.weekday()]
        if day_key not in schedule["weekdays"]:
            continue
        candidate = tz.localize(datetime.combine(day, time(hour=hour, minute=minute)))
        if candidate <= current:
            pending = candidate
            break

    if pending is None:
        return None

    if not last_success_iso:
        # Успешных запусков ещё не было: не форсируем цикл при первом открытии
        # панели — он выполнится по расписанию или по кнопке.
        return None

    try:
        last_success = datetime.fromisoformat(str(last_success_iso))
        if last_success.tzinfo is None:
            last_success = tz.localize(last_success)
        last_success = last_success.astimezone(tz)
    except (TypeError, ValueError):
        return pending

    return pending if last_success < pending else None
