"""Streamlit-панель управления парсером маркетплейсов (замена Flask-интерфейса).

Запуск::

    streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port 8501

Что внутри:

* 📊 Обзор — состояние цикла, остатки/цены/флаги, ручной запуск;
* 🗓 Расписание — ежедневный автозапуск (по умолчанию 07:00 МСК);
* 📦 Товары — таблицы маркетплейсов с редактированием;
* 🧾 Заказы, 📜 Логи, ⚙️ Диагностика, ❓ Помощь.

Планировщик запускается вместе с приложением (``streamlit_scheduler``),
поэтому отдельный cron нужен только если Streamlit не работает круглосуточно.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz
import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
SYSTEM_DIR = PROJECT_ROOT / "System"
load_dotenv(dotenv_path=SYSTEM_DIR / ".env")

from logger_config import LOG_DIR, logger  # noqa: E402
from services import market_controls  # noqa: E402
from services.automation_state import load_automation_state  # noqa: E402
from services.cycle_runner import get_last_update_stamp, run_cycle  # noqa: E402
from services.parse_schedule import (  # noqa: E402
    DAY_LABELS,
    DAY_ORDER,
    DEFAULT_TIME,
    DEFAULT_TIMEZONE,
    describe,
    format_moment,
    load_schedule,
    save_schedule,
    upcoming_runs,
)

st.set_page_config(
    page_title="Парсер маркетплейсов",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

MARKET_LABELS = {
    "wildberries": "🟣 Wildberries",
    "ozon": "🔵 Ozon",
    "yandex": "🟡 Яндекс.Маркет",
}
RESULT_LABELS = {
    "success": ("✅", "успешно"),
    "partial": ("⚠️", "с предупреждениями"),
    "error": ("❌", "ошибка"),
    "interrupted": ("⛔", "прервано"),
    "skipped_locked": ("⏭", "уже выполнялся"),
    "running": ("🔄", "выполняется"),
    "never": ("⏳", "ещё не запускался"),
    "disabled": ("⏸", "автозапуск выключен"),
}
STEP_LABELS = {
    "refresh_sklad": "Обновление склада",
    "stocks": "Остатки на маркетплейсах",
    "orders": "Новые заказы",
    "prices": "Цены на маркетплейсах",
}
ENV_KEYS = (
    "SECRET_KEY",
    "PASSWORD_HASH",
    "PASSWORD",
    "STREAMLIT_PASSWORD",
    "WB_API_TOKEN",
    "WB_WAREHOUSE_ID",
    "OZON_CLIENT_ID",
    "OZON_API_KEY",
    "YM_TOKEN",
    "YM_CAMPAIGN_ID",
    "telegram_got_token_error",
    "telegram_chat_id_error",
    "WORLDPRICES_API_KEY",
)


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def _display_path(path: Path) -> str:
    """Путь относительно корня проекта (LOG_DIR задан относительным путём)."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def database_exists() -> bool:
    return market_controls.DB_PATH.exists()


def cycle_is_running() -> bool:
    return bool(load_automation_state().get("running"))


def launch_cycle(trigger: str = "manual") -> bool:
    """Запустить цикл в фоне, чтобы интерфейс оставался отзывчивым."""
    if cycle_is_running():
        return False
    threading.Thread(
        target=run_cycle,
        kwargs={"trigger": trigger, "check_schedule": False},
        name=f"parser-cycle-{trigger}",
        daemon=True,
    ).start()
    return True


def _password_is_valid(entered: str) -> bool:
    expected_hash = os.getenv("PASSWORD_HASH", "").strip()
    if expected_hash:
        try:
            from werkzeug.security import check_password_hash

            return check_password_hash(expected_hash, entered)
        except Exception:  # noqa: BLE001 - повреждённый хеш не должен ломать вход
            logger.warning("⚠️ PASSWORD_HASH задан, но не читается как хеш werkzeug")
            return False
    expected_plain = (os.getenv("STREAMLIT_PASSWORD") or os.getenv("PASSWORD") or "").strip()
    return bool(expected_plain) and entered == expected_plain


def require_login() -> None:
    """Простой вход по паролю из System/.env (как в прежнем Flask-интерфейсе)."""
    if st.session_state.get("authenticated"):
        return

    auth_configured = bool(
        (os.getenv("PASSWORD_HASH") or "").strip()
        or (os.getenv("STREAMLIT_PASSWORD") or "").strip()
        or (os.getenv("PASSWORD") or "").strip()
    )

    if not auth_configured:
        st.session_state["authenticated"] = True
        return

    st.markdown("### 🔐 Вход в панель парсера")
    with st.form("login"):
        password = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти", type="primary", use_container_width=True)
    if submitted:
        if _password_is_valid(password):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Неверный пароль")
    st.stop()


def read_log_tail(lines: int = 200, level: str = "ВСЕ") -> list[str]:
    log_path = LOG_DIR / "app.log"
    if not log_path.exists():
        return []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            content = stream.readlines()
    except OSError as error:
        return [f"Не удалось прочитать лог: {error}"]

    selected = content[-max(lines, 1) :]
    if level != "ВСЕ":
        selected = [line for line in selected if f"| {level}" in line]
    return [line.rstrip("\n") for line in selected]


def human_duration(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds:.0f} с"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} мин {rest} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def format_iso(value) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# Блоки интерфейса
# --------------------------------------------------------------------------- #
def render_sidebar() -> str:
    schedule = load_schedule()
    with st.sidebar:
        st.markdown("## 📦 Парсер маркетплейсов")
        st.caption(f"Streamlit · {datetime.now(pytz.timezone(schedule['timezone'])).strftime('%H:%M')} "
                   f"({schedule['timezone']})")

        page = st.radio(
            "Раздел",
            (
                "📊 Обзор",
                "🗓 Расписание",
                "📦 Товары",
                "🧾 Заказы",
                "📜 Логи",
                "⚙️ Диагностика",
                "❓ Помощь",
            ),
            label_visibility="collapsed",
        )

        st.divider()
        st.markdown("**Автозапуск**")
        st.write(describe(schedule))
        st.caption(f"Ближайший запуск: {format_moment(upcoming_runs(schedule, 1)[0] if schedule['enabled'] else None)}")

        st.divider()
        if st.button("🚀 Запустить цикл сейчас", use_container_width=True, type="primary"):
            if launch_cycle("manual"):
                st.toast("Цикл запущен в фоне", icon="🚀")
            else:
                st.toast("Цикл уже выполняется", icon="⏳")
            time.sleep(0.3)
            st.rerun()

        if not database_exists():
            st.warning("База `System/marketplace_base.db` не найдена — работают только демо-функции.")
        if (SYSTEM_DIR / ".demo_mode").exists():
            st.info("Демо-данные. Реальные ключи добавьте в `System/.env`.")
        return page


def render_status_cards(state: dict, schedule: dict) -> None:
    result = state.get("last_result") or "never"
    icon, label = RESULT_LABELS.get(result, ("•", str(result)))
    last_run = format_iso(state.get("last_finished"))

    columns = st.columns(4)
    columns[0].metric("Последний запуск", last_run, icon + " " + label, delta_color="off")
    columns[1].metric(
        "Ближайший запуск",
        format_moment(upcoming_runs(schedule, 1)[0] if schedule["enabled"] else None),
        describe(schedule),
        delta_color="off",
    )
    columns[2].metric(
        "Длительность",
        human_duration(state.get("last_duration_seconds")),
        f"триггер: {state.get('trigger') or '—'}",
        delta_color="off",
    )
    columns[3].metric(
        "Склад обновлён",
        get_last_update_stamp() or "—",
        "System/last_update.txt",
        delta_color="off",
    )

    if state.get("running"):
        st.info("🔄 Цикл синхронизации выполняется прямо сейчас…")
    message = state.get("last_message")
    if message and not state.get("running"):
        if result == "success":
            st.success(f"{icon} {message}")
        elif result in {"partial", "skipped_locked"}:
            st.warning(f"{icon} {message}")
        elif result in {"error", "interrupted"}:
            st.error(f"{icon} {message}")


def render_market_controls() -> None:
    flags = market_controls.load_flags()
    st.subheader("Маркетплейсы и поставщики")

    columns = st.columns(3)
    for column, market in zip(columns, ("wildberries", "ozon", "yandex")):
        enabled = bool(flags.get(market, True))
        with column:
            st.markdown(f"**{MARKET_LABELS[market]}**")
            st.caption("ON — остатки публикуются" if enabled else "OFF — остатки обнулены")
            if st.toggle("Включён", value=enabled, key=f"market-{market}"):
                pass
            if enabled != bool(flags.get(market, True)):
                with st.spinner(f"Переключаю {market}…"):
                    try:
                        market_controls.toggle_market(market, actor="streamlit")
                        st.toast(f"{market}: переключено", icon="✅")
                    except market_controls.MarketControlError as error:
                        st.error(str(error))
                st.rerun()

    st.markdown("**Поставщики**")
    supplier_columns = st.columns(4)
    for column, supplier in zip(supplier_columns, ("Invask", "Okno", "United", "Sklad")):
        current = bool(flags.get("suppliers", {}).get(supplier, True))
        with column:
            value = st.checkbox(supplier, value=current, key=f"supplier-{supplier}")
            if value != current:
                with st.spinner(f"Переключаю {supplier}…"):
                    try:
                        market_controls.toggle_supplier(supplier, actor="streamlit")
                        st.toast(f"{supplier}: переключено", icon="✅")
                    except market_controls.MarketControlError as error:
                        st.error(str(error))
                st.rerun()


def render_steps_table() -> None:
    from services.control_store import list_process_runs

    try:
        runs = list_process_runs(limit=10)
    except Exception as error:  # noqa: BLE001
        st.caption(f"Журнал циклов недоступен: {error}")
        return
    if not runs:
        st.caption("Журнал пока пуст — цикл ещё не запускался.")
        return

    frame = pd.DataFrame(runs)
    columns = [
        column
        for column in ("started_at", "process_name", "trigger", "status", "duration_seconds", "message")
        if column in frame.columns
    ]
    st.dataframe(
        frame[columns].rename(
            columns={
                "started_at": "Начало",
                "process_name": "Процесс",
                "trigger": "Триггер",
                "status": "Статус",
                "duration_seconds": "Секунды",
                "message": "Сообщение",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )


def render_overview() -> None:
    state = load_automation_state()
    schedule = load_schedule()
    st.title("📊 Обзор")
    st.caption("Полный цикл: склад → остатки → заказы → цены")

    render_status_cards(state, schedule)

    st.divider()
    left, right = st.columns((1.15, 1))
    with left:
        render_market_controls()
    with right:
        st.subheader("Журнал последних циклов")
        render_steps_table()

        st.subheader("Последние записи лога")
        tail = read_log_tail(lines=18)
        st.code("\n".join(tail) if tail else "Лог пуст", language="text")


def render_schedule_page() -> None:
    st.title("🗓 Расписание автозапуска")
    schedule = load_schedule()

    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)

    st.caption(
        "Парсинг запускается планировщиком внутри Streamlit — отдельный cron не нужен, "
        "пока приложение запущено."
    )

    with st.form("schedule-form"):
        enabled = st.toggle("Автоматический парсинг включён", value=schedule["enabled"])

        col_time, col_tz = st.columns(2)
        with col_time:
            time_value = st.text_input(
                "Время запуска (ЧЧ:ММ)",
                value=schedule["time"],
                help="По умолчанию 07:00 — как требовалось в задаче.",
            )
        with col_tz:
            timezones = list(pytz.common_timezones)
            if schedule["timezone"] not in timezones:
                timezones = [schedule["timezone"]] + timezones
            timezone_value = st.selectbox(
                "Часовой пояс",
                timezones,
                index=timezones.index(schedule["timezone"]),
            )

        weekdays = st.multiselect(
            "Дни недели",
            options=list(DAY_ORDER),
            default=schedule["weekdays"],
            format_func=lambda day: DAY_LABELS[day],
            help="По умолчанию — все семь дней, то есть ежедневно.",
        )
        run_if_missed = st.checkbox(
            "Догонять пропущенный запуск после простоя",
            value=schedule["run_if_missed"],
            help="Если приложение было выключено в 07:00, цикл выполнится при следующем старте.",
        )

        saved = st.form_submit_button("💾 Сохранить расписание", type="primary", use_container_width=True)

    if saved:
        try:
            updated = save_schedule(
                {
                    "enabled": enabled,
                    "time": time_value or DEFAULT_TIME,
                    "weekdays": weekdays,
                    "timezone": timezone_value or DEFAULT_TIMEZONE,
                    "run_if_missed": run_if_missed,
                }
            )
            import streamlit_scheduler

            streamlit_scheduler.apply_schedule(updated)
            st.session_state["flash"] = f"Сохранено: {describe(updated)}"
            st.rerun()
        except ValueError as error:
            st.error(str(error))

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Ближайшие запуски")
        runs = upcoming_runs(schedule, 5)
        if not runs:
            st.warning("Автозапуск выключен — цикл выполняется только по кнопке.")
        else:
            st.table(
                pd.DataFrame(
                    {
                        "Дата и время": [format_moment(moment) for moment in runs],
                        "День": [DAY_LABELS[moment.strftime("%a").lower()] for moment in runs],
                    }
                )
            )
        st.caption(f"Сейчас в поясе {schedule['timezone']}: "
                   f"{datetime.now(pytz.timezone(schedule['timezone'])).strftime('%d.%m.%Y %H:%M')}")
    with right:
        st.subheader("Как это работает")
        st.markdown(
            f"""
- Планировщик (APScheduler) стартует вместе с Streamlit и держит одну задачу `daily-parse`.
- Время берётся из `System/parse_schedule.json` в часовом поясе **{schedule['timezone']}**.
- Если приложение было выключено, включите «догон» или используйте системный планировщик.
- Пока идёт цикл, повторный запуск блокируется (межпроцессный lock-файл).
            """
        )
        st.code(
            "# запуск вручную из терминала\npython run_cycle_once.py\n\n"
            "# systemd timer / cron — см. README_STREAMLIT.md",
            language="bash",
        )


def render_products_page() -> None:
    st.title("📦 Товары")
    if not database_exists():
        st.error(
            "Файл `System/marketplace_base.db` не найден. "
            "Для демонстрации создайте тестовую базу:\n\n"
            "`python tools/create_demo_db.py`"
        )
        return

    market = st.selectbox(
        "Маркетплейс",
        options=list(MARKET_LABELS),
        format_func=lambda code: MARKET_LABELS[code],
    )

    search = st.text_input("Поиск по модели / артикулу", "")
    try:
        frame = market_controls.load_market_table(market)
    except Exception as error:  # noqa: BLE001
        st.error(f"Не удалось прочитать таблицу: {error}")
        return

    if frame.empty:
        st.warning("В таблице нет строк.")
        return

    if search:
        mask = frame.apply(
            lambda row: search.lower() in " ".join(
                str(row.get(column, "")) for column in ("Модель", "Sklad", "Invask", "Okno", "United")
            ).lower(),
            axis=1,
        )
        filtered = frame[mask]
    else:
        filtered = frame

    metrics = st.columns(3)
    metrics[0].metric("Товаров", f"{len(frame)}")
    metrics[1].metric("Суммарный остаток", f"{int(pd.to_numeric(filtered.get('Нал', 0), errors='coerce').fillna(0).sum())}")
    metrics[2].metric("Средняя наценка", f"{pd.to_numeric(filtered.get('%', 0), errors='coerce').mean():.1f} %")

    editable_columns = ["Статус", "%", "Нал", "Опт", "Цена", "Комментарий"]
    column_config = {}
    if "Статус" in filtered.columns:
        column_config["Статус"] = st.column_config.SelectboxColumn("Статус", options=["вкл.", "выкл."])
    for column in ("%", "Нал", "Опт", "Цена"):
        if column in filtered.columns:
            column_config[column] = st.column_config.NumberColumn(column, format="%d")

    edited = st.data_editor(
        filtered.reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
        disabled=[column for column in filtered.columns if column not in editable_columns],
        column_config=column_config,
        key=f"editor-{market}",
    )

    action_left, action_right = st.columns(2)
    with action_left:
        if st.button("💾 Сохранить изменения", type="primary", use_container_width=True):
            try:
                changed = market_controls.save_market_table_edits(market, edited)
                st.success(f"Сохранено строк: {changed}. Остатки и цены пересчитаны.")
                time.sleep(0.4)
                st.rerun()
            except Exception as error:  # noqa: BLE001
                st.error(f"Не удалось сохранить: {error}")
    with action_right:
        if st.button("🔄 Пересчитать остатки и цены", use_container_width=True):
            with st.spinner("Пересчёт…"):
                try:
                    updated = market_controls.recompute_marketplace_core(market)
                    st.success(f"Пересчёт завершён, изменено строк: {updated}")
                except Exception as error:  # noqa: BLE001
                    st.error(f"Ошибка пересчёта: {error}")

    csv = filtered.drop(columns=["_rowid"], errors="ignore").to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⤓ Скачать CSV",
        data=csv,
        file_name=f"{market}-{datetime.now():%Y-%m-%d}.csv",
        mime="text/csv",
    )


def render_orders_page() -> None:
    st.title("🧾 Заказы")
    from services.control_store import delete_order, init_control_store, list_orders

    try:
        init_control_store()
        orders = list_orders(limit=200)
    except Exception as error:  # noqa: BLE001
        st.error(f"Журнал заказов недоступен: {error}")
        return

    if not orders:
        st.info("Заказов пока нет — они появятся после первого успешного цикла получения заказов.")
        return

    frame = pd.DataFrame(orders)
    display_columns = [
        column
        for column in (
            "marketplace", "order_id", "status", "created_at", "product_summary",
            "supplier", "quantity", "amount", "updated_at",
        )
        if column in frame.columns
    ]
    st.dataframe(frame[display_columns], use_container_width=True, hide_index=True)

    with st.expander("Удалить заказ из журнала"):
        options = [
            f"{row.get('marketplace')} · {row.get('order_id')}"
            for row in orders
        ]
        selected = st.selectbox("Заказ", options)
        if st.button("🗑 Удалить", type="secondary"):
            marketplace, order_id = (part.strip() for part in selected.split("·", 1))
            if delete_order(marketplace, order_id):
                st.success("Заказ удалён")
                time.sleep(0.3)
                st.rerun()
            else:
                st.error("Не удалось удалить заказ")


def render_logs_page() -> None:
    st.title("📜 Логи")
    left, right = st.columns((1, 3))
    with left:
        lines = st.slider("Строк", min_value=50, max_value=2000, value=300, step=50)
        level = st.selectbox("Уровень", ("ВСЕ", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"))
        if st.button("🔄 Обновить", use_container_width=True):
            st.rerun()
    with right:
        tail = read_log_tail(lines=lines, level=level)
        st.code("\n".join(tail) if tail else "Лог пуст", language="text")
        log_file = LOG_DIR / "app.log"
        if log_file.exists():
            st.download_button(
                "⤓ Скачать app.log",
                data=log_file.read_bytes(),
                file_name="app.log",
                mime="text/plain",
            )


def render_diagnostics_page() -> None:
    st.title("⚙️ Диагностика")
    schedule = load_schedule()
    state = load_automation_state()

    st.subheader("Ключи из System/.env")
    rows = []
    for key in ENV_KEYS:
        value = os.getenv(key) or ""
        rows.append(
            {
                "Переменная": key,
                "Статус": "✅ задана" if value else "— пусто",
                "Значение": (value[:4] + "…" + value[-4:]) if len(value) > 12 else ("***" if value else ""),
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if not (SYSTEM_DIR / ".env").exists():
        st.warning("Файл `System/.env` не найден — скопируйте `System/.env.example` и заполните ключи.")

    st.subheader("Файлы и базы")
    file_rows = []
    for path in (
        SYSTEM_DIR / "marketplace_base.db",
        SYSTEM_DIR / "!YMWB.db",
        SYSTEM_DIR / "control_center.db",
        SYSTEM_DIR / "stock_flags.json",
        SYSTEM_DIR / "parse_schedule.json",
        LOG_DIR / "app.log",
    ):
        exists = path.exists()
        file_rows.append(
            {
                "Файл": _display_path(path),
                "Есть": "✅" if exists else "—",
                "Размер": f"{path.stat().st_size / 1024:.0f} КБ" if exists else "",
                "Изменён": datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
                if exists
                else "",
            }
        )
    st.dataframe(pd.DataFrame(file_rows), use_container_width=True, hide_index=True)

    st.subheader("Флаги маркетплейсов (stock_flags.json)")
    st.json(market_controls.load_flags())

    st.subheader("Расписание (parse_schedule.json)")
    st.json(schedule)

    st.subheader("Состояние цикла (automation_state.json)")
    st.json(state)

    st.subheader("Отдельные шаги")
    st.caption("Ручной запуск одного шага — удобно для проверки ключей API.")
    columns = st.columns(4)
    steps = (
        ("Склад", _run_refresh_only),
        ("Остатки", _run_stocks_only),
        ("Заказы", _run_orders_only),
        ("Цены", _run_prices_only),
    )
    for column, (label, handler) in zip(columns, steps):
        with column:
            if st.button(label, use_container_width=True, key=f"step-{label}"):
                with st.spinner(f"{label}: выполняется…"):
                    st.write(handler())


def _run_refresh_only() -> str:
    from services.cycle_runner import refresh_sklad_task

    try:
        refresh_sklad_task()
        return "✅ Склад обновлён"
    except Exception as error:  # noqa: BLE001
        return f"❌ {type(error).__name__}: {error}"


def _run_stocks_only() -> str:
    from services.marketplace_cycle import run_stock_updates

    return "✅ Остатки отправлены" if run_stock_updates() else "⚠️ Не все остатки отправлены"


def _run_orders_only() -> str:
    try:
        from order_notifications import check_for_new_orders

        check_for_new_orders()
        return "✅ Заказы проверены"
    except Exception as error:  # noqa: BLE001
        return f"❌ {type(error).__name__}: {error}"


def _run_prices_only() -> str:
    try:
        from price_updater_master import update_all_prices

        update_all_prices()
        return "✅ Цены обновлены"
    except Exception as error:  # noqa: BLE001
        return f"❌ {type(error).__name__}: {error}"


def render_help_page() -> None:
    st.title("❓ Помощь")
    st.markdown(
        """
### Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt

cp System/.env.example System/.env  # заполните ключи API
streamlit run streamlit_app.py
```

Панель откроется на `http://localhost:8501`.

### Ежедневный парсинг в 07:00

* Пока Streamlit запущен, планировщик внутри приложения сам выполнит цикл
  каждый день в 07:00 (раздел «🗓 Расписание»).
* Чтобы парсинг шёл даже при закрытом Streamlit, используйте
  `run_cycle_once.py` + systemd timer / cron / Планировщик задач Windows —
  готовые файлы лежат в `deploy/`.

### Что делает цикл

1. Обновляет локальную базу склада из Google Sheets.
2. Публикует остатки на Wildberries, Ozon, Яндекс.Маркет.
3. Забирает новые заказы и отправляет уведомления в Telegram.
4. Публикует рассчитанные цены.

### Если что-то не работает

1. Раздел «⚙️ Диагностика» → проверьте, что ключи в `System/.env` заданы.
2. Раздел «📜 Логи» → найдите строку с ошибкой (файл `System/logs/app.log`).
3. Убедитесь, что базы `System/marketplace_base.db` и `System/!YMWB.db` на месте.
        """
    )


def main() -> None:
    require_login()

    import streamlit_scheduler

    # Планировщик поднимается один раз за процесс и переживает перерисовки UI.
    streamlit_scheduler.get_scheduler()

    page = render_sidebar()

    if page == "📊 Обзор":
        render_overview()
    elif page == "🗓 Расписание":
        render_schedule_page()
    elif page == "📦 Товары":
        render_products_page()
    elif page == "🧾 Заказы":
        render_orders_page()
    elif page == "📜 Логи":
        render_logs_page()
    elif page == "⚙️ Диагностика":
        render_diagnostics_page()
    else:
        render_help_page()


if __name__ == "__main__":
    main()
