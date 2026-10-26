from copy import deepcopy
from datetime import datetime
from pathlib import Path

from logger_config import logger
from utilities.atomic_files import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = PROJECT_ROOT / "System" / "market_schedule.json"

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

DEFAULT_SCHEDULE = {
    "enabled": False,
    "off_day": "thu",
    "off_time": "23:00",
    "on_day": "sun",
    "on_time": "15:00",
}


def _normalise_time(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as error:
        raise ValueError("Время должно быть указано в формате ЧЧ:ММ") from error
    return parsed.strftime("%H:%M")


def validate_market_schedule(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Настройки расписания должны быть объектом")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("Поле enabled должно быть логическим")

    off_day = str(raw.get("off_day", "")).strip().lower()
    on_day = str(raw.get("on_day", "")).strip().lower()
    if off_day not in DAY_LABELS or on_day not in DAY_LABELS:
        raise ValueError("Выбран неизвестный день недели")

    off_time = _normalise_time(raw.get("off_time"))
    on_time = _normalise_time(raw.get("on_time"))
    if off_day == on_day and off_time == on_time:
        raise ValueError("Включение и выключение не могут происходить одновременно")

    return {
        "enabled": enabled,
        "off_day": off_day,
        "off_time": off_time,
        "on_day": on_day,
        "on_time": on_time,
    }


def load_market_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        return deepcopy(DEFAULT_SCHEDULE)

    try:
        import json

        with SCHEDULE_PATH.open("r", encoding="utf-8") as stream:
            return validate_market_schedule(json.load(stream))
    except (OSError, ValueError, TypeError) as error:
        logger.warning(f"Не удалось прочитать недельное расписание: {error}")
        return deepcopy(DEFAULT_SCHEDULE)


def save_market_schedule(raw: object) -> dict:
    schedule = validate_market_schedule(raw)
    atomic_write_json(SCHEDULE_PATH, schedule)
    return schedule


def _week_minute(day: str, time_value: str) -> int:
    hour, minute = (int(part) for part in time_value.split(":", 1))
    return DAY_ORDER.index(day) * 24 * 60 + hour * 60 + minute


def markets_should_be_enabled(schedule: dict, now: datetime | None = None) -> bool:
    """Возвращает требуемое состояние каналов в текущем недельном интервале."""
    current = now or datetime.now()
    current_minute = current.weekday() * 24 * 60 + current.hour * 60 + current.minute
    off_minute = _week_minute(schedule["off_day"], schedule["off_time"])
    on_minute = _week_minute(schedule["on_day"], schedule["on_time"])

    if off_minute < on_minute:
        disabled = off_minute <= current_minute < on_minute
    else:
        disabled = current_minute >= off_minute or current_minute < on_minute
    return not disabled


def get_next_event(schedule: dict, now: datetime | None = None) -> dict | None:
    if not schedule["enabled"]:
        return None

    current = now or datetime.now()
    current_minute = current.weekday() * 24 * 60 + current.hour * 60 + current.minute
    week_minutes = 7 * 24 * 60
    candidates = []

    for action, day_key, time_key in (
        ("off", "off_day", "off_time"),
        ("on", "on_day", "on_time"),
    ):
        target = _week_minute(schedule[day_key], schedule[time_key])
        delta = (target - current_minute) % week_minutes
        if delta == 0:
            delta = week_minutes
        candidates.append((delta, action, schedule[day_key], schedule[time_key]))

    _, action, day, time_value = min(candidates)
    action_label = "выкл." if action == "off" else "вкл."
    return {
        "action": action,
        "day": day,
        "day_label": DAY_LABELS[day],
        "time": time_value,
        "display": f"{action_label} {DAY_LABELS[day]} {time_value}",
    }


def get_market_schedule_status(now: datetime | None = None) -> dict:
    schedule = load_market_schedule()
    next_event = get_next_event(schedule, now)
    return {
        **schedule,
        "days": [{"value": day, "label": DAY_LABELS[day]} for day in DAY_ORDER],
        "next_event": next_event,
        "next_event_display": (
            f"Следующее: {next_event['display']}"
            if next_event
            else "Расписание WB выключено"
        ),
        "target_market": "wildberries",
        "desired_markets_enabled": (
            markets_should_be_enabled(schedule, now)
            if schedule["enabled"]
            else None
        ),
    }
