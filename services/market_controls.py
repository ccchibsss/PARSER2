"""Действия над остатками маркетплейсов и поставщиками.

Модуль содержит «чистую» бизнес-логику без Flask: переключение маркетплейсов
и поставщиков ON/OFF, пересчёт остатков/цен и чтение флагов.

Раньше эта логика жила прямо в обработчиках Flask (``web_app.py``), из-за чего
её нельзя было вызвать из Streamlit или из планировщика. Теперь единственный
источник правды — этот модуль:

* ``streamlit_app.py`` — интерфейс;
* ``web_app.py`` — прежний Flask-интерфейс (делегирует сюда же);
* ``services/cycle_runner.py`` — автоматический цикл.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from logger_config import logger
from utilities.atomic_files import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_DIR = PROJECT_ROOT / "System"
DB_PATH = SYSTEM_DIR / "marketplace_base.db"
SUPPLIER_DB_PATH = SYSTEM_DIR / "!YMWB.db"
FLAGS_PATH = SYSTEM_DIR / "stock_flags.json"
TEMP_BACKUP_DB_PATH = SYSTEM_DIR / "temp_stock_backup.db"

ALLOWED_MARKETS = {"yandex", "ozon", "wildberries"}
ALLOWED_SUPPLIERS = {"Invask", "Okno", "United", "Sklad"}
SUPPLIER_PRIORITY = ["Invask", "Okno", "United"]
DEFAULT_MARKET_FLAGS = {
    "yandex": True,
    "ozon": True,
    "wildberries": True,
    "suppliers": {},
}

_supplier_table_cache: str | None = None


class MarketControlError(RuntimeError):
    """Ошибка переключения маркетплейса или поставщика."""


# --------------------------------------------------------------------------- #
# Флаги
# --------------------------------------------------------------------------- #
def load_flags() -> dict:
    """Прочитать ``System/stock_flags.json`` (с безопасными значениями)."""
    try:
        import json

        with FLAGS_PATH.open("r", encoding="utf-8") as stream:
            flags = json.load(stream)
        if not isinstance(flags, dict):
            raise ValueError("ожидался JSON-объект")
    except FileNotFoundError:
        flags = {}
    except Exception as error:  # noqa: BLE001 - файл может быть повреждён
        logger.warning(f"⚠️ Не удалось прочитать {FLAGS_PATH}: {error}")
        flags = {}

    for market in ("yandex", "ozon", "wildberries"):
        flags.setdefault(market, True)
    suppliers = flags.get("suppliers")
    if not isinstance(suppliers, dict):
        suppliers = {}
    for supplier in ALLOWED_SUPPLIERS:
        suppliers.setdefault(supplier, True)
    flags["suppliers"] = suppliers
    return flags


def save_flags(flags: dict) -> dict:
    atomic_write_json(FLAGS_PATH, flags)
    return flags


def is_market_enabled(market: str, flags: dict | None = None) -> bool:
    flags = flags if flags is not None else load_flags()
    return bool(flags.get(market, True))


def is_supplier_enabled(supplier: str, flags: dict | None = None) -> bool:
    flags = flags if flags is not None else load_flags()
    return bool(flags.get("suppliers", {}).get(supplier, True))


# --------------------------------------------------------------------------- #
# Каталог поставщиков
# --------------------------------------------------------------------------- #
def _normalise_article(value) -> str:
    compact = "".join(str(value or "").split())
    return compact.lstrip("0") or ("0" if compact else "")


def load_supplier_catalog() -> dict[tuple[str, str], tuple[int, float | None]]:
    """Загрузить остатки поставщиков один раз на операцию."""
    catalog: dict[tuple[str, str], tuple[int, float | None]] = {}
    with sqlite3.connect(SUPPLIER_DB_PATH, timeout=10) as connection:
        rows = connection.execute(
            'SELECT "Поставщик", "Артикул", COALESCE("Наличие", 0), "ОПТ" FROM prices'
        ).fetchall()
    for supplier, article, stock_value, opt_value in rows:
        key = (str(supplier or "").strip().casefold(), _normalise_article(article))
        try:
            stock_value = int(str(stock_value).strip() or 0)
        except (TypeError, ValueError):
            stock_value = 0
        try:
            opt_value = float(str(opt_value).replace(" ", "").replace("р.", ""))
        except (TypeError, ValueError):
            opt_value = None
        catalog.setdefault(key, (stock_value, opt_value))
    return catalog


def _fetch_stock_for(catalog, supplier: str, code: str):
    if not code or not str(code).strip():
        return 0, None
    if isinstance(catalog, dict):
        return catalog.get(
            (supplier.strip().casefold(), _normalise_article(code)),
            (0, None),
        )

    sup_conn = None
    try:
        sup_conn = sqlite3.connect(SUPPLIER_DB_PATH, timeout=5)
        cursor = sup_conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE("Наличие",0), "ОПТ"
              FROM prices
             WHERE UPPER(TRIM("Поставщик")) = UPPER(?)
               AND (
                    REPLACE(REPLACE(REPLACE(CAST("Артикул" AS TEXT), ' ', ''), ' ', ''), CHAR(9), '')
                        = REPLACE(REPLACE(REPLACE(?, ' ', ''), ' ', ''), CHAR(9), '')
                 OR REPLACE(REPLACE(REPLACE(LTRIM(CAST("Артикул" AS TEXT), '0'), ' ', ''), ' ', ''), CHAR(9), '')
                        = REPLACE(REPLACE(REPLACE(LTRIM(?, '0'), ' ', ''), ' ', ''), CHAR(9), '')
               )
             LIMIT 1
        """,
            (supplier, str(code).strip(), str(code).strip()),
        )
        row = cursor.fetchone()
    except Exception as error:  # noqa: BLE001
        logger.warning(f"❌ SUPPLIERS_DB read failed: {error}")
        row = None
    finally:
        try:
            if sup_conn is not None:
                sup_conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not row:
        return 0, None

    stock, opt = row
    try:
        stock = int(str(stock).strip() or 0)
    except (TypeError, ValueError):
        stock = 0
    try:
        opt = float(str(opt).replace(" ", "").replace("р.", "")) if opt is not None else None
    except (TypeError, ValueError):
        opt = None
    return stock, opt


def choose_best_supplier_for_row(
    row: dict,
    catalog=None,
    use_row_sklad: bool = True,
    flags: dict | None = None,
) -> tuple[str, int, float | None]:
    """Выбрать поставщика для строки ``marketplace``.

    * если остаток своего склада ``Sklad`` >= 1 — берём его;
    * иначе среди Invask/Okno/United с остатком > 0 выбирается минимальная
      закупочная цена (при равенстве — по приоритету);
    * если кандидатов нет — ``('', 0, None)``.
    """
    flags = flags if flags is not None else load_flags()
    suppliers_flags = flags.get("suppliers", {})

    sklad_code = str(row.get("Sklad") or "").strip()
    sklad_enabled = bool(suppliers_flags.get("Sklad", True))

    if sklad_code and sklad_enabled and use_row_sklad:
        stock_ext, opt_ext = _fetch_stock_for(catalog, "Sklad", sklad_code)
        if stock_ext >= 1:
            return "Sklad", stock_ext, opt_ext

    best: tuple[str, int, float | None] = ("", 0, None)
    for supplier in SUPPLIER_PRIORITY:
        code = str(row.get(supplier) or "").strip()
        if not code:
            continue
        if not bool(suppliers_flags.get(supplier, True)):
            continue
        stock, opt = _fetch_stock_for(catalog, supplier, code)
        if stock and stock > 0 and opt is not None:
            if best[0] == "":
                best = (supplier, stock, opt)
            elif opt < best[2]:
                best = (supplier, stock, opt)
            elif opt == best[2] and SUPPLIER_PRIORITY.index(supplier) < SUPPLIER_PRIORITY.index(best[0]):
                best = (supplier, stock, opt)
    return best


def calc_price(opt_value, markup_raw) -> int | None:
    try:
        opt = float(str(opt_value).replace(" ", "").replace("р.", ""))
        markup = float(str(markup_raw).replace("%", "").replace(" ", ""))
        return int(round((opt + opt * markup / 100.0) / 100.0) * 100)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Пересчёт остатков и цен
# --------------------------------------------------------------------------- #
def recompute_marketplace_core(market: str, flags: dict | None = None) -> int:
    """Пересчитать «Нал»/«Опт»/«Цена» для одного маркетплейса.

    Возвращает количество изменённых строк.
    """
    if market not in ALLOWED_MARKETS:
        raise ValueError(f"Неизвестный маркетплейс: {market}")

    flags = flags if flags is not None else load_flags()
    if not is_market_enabled(market, flags):
        logger.info(f"⏭ {market.upper()} выключен → пересчёт пропущен, нули сохраняем")
        return 0

    supplier_catalog = load_supplier_catalog()
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()

    rows = cursor.execute(
        """
        SELECT rowid, Sklad, Invask, Okno, United,
               "%", Цена, Опт, Нал, Статус, Модель
          FROM marketplace
         WHERE Маркетплейс = ?
        """,
        (market,),
    ).fetchall()

    updated = 0
    for record in rows:
        row = dict(record)
        chosen_supplier, stock, opt = choose_best_supplier_for_row(
            row, supplier_catalog, use_row_sklad=True, flags=flags
        )

        if chosen_supplier == "":
            new_stock = 0
            new_opt = row.get("Опт")
        else:
            new_stock = int(stock or 0)
            new_opt = opt if opt is not None else row.get("Опт")

        try:
            markup = float(str(row.get("%", "0")).replace("%", "").replace(" ", ""))
        except Exception:  # noqa: BLE001
            markup = 0.0

        if str(row.get("Статус", "")).strip().lower() == "выкл.":
            new_stock = 0

        # 🚫 Заморозка: при Нал=0 не трогаем Опт/Цену
        freeze_price = int(new_stock or 0) == 0

        new_price = None
        if not freeze_price and new_opt is not None:
            new_price = calc_price(new_opt, markup)

        changed = False
        sets: list[str] = []
        values: list = []

        if int(row.get("Нал") or 0) != int(new_stock):
            sets.append("Нал = ?")
            values.append(int(new_stock))
            changed = True

        try:
            current_opt = float(str(row.get("Опт") or "0").replace(" ", "").replace("р.", ""))
        except Exception:  # noqa: BLE001
            current_opt = None
        if (not freeze_price) and (new_opt is not None):
            try:
                new_opt_float = float(new_opt)
            except Exception:  # noqa: BLE001
                new_opt_float = current_opt
            if current_opt is None or (new_opt_float is not None and new_opt_float != current_opt):
                sets.append("Опт = ?")
                values.append(new_opt_float)
                changed = True

        try:
            current_price = int(str(row.get("Цена") or "0").replace(" ", "").replace("р.", ""))
        except Exception:  # noqa: BLE001
            current_price = 0
        if new_price is not None and new_price != current_price:
            sets.append("Цена = ?")
            values.append(int(new_price))
            changed = True

        if changed:
            sets.append('"Дата изменения" = ?')
            values.append(datetime.now().strftime("%d.%m.%Y %H:%M"))
            values.append(record["rowid"])
            cursor.execute(f"UPDATE marketplace SET {', '.join(sets)} WHERE rowid = ?", values)
            updated += 1

    connection.commit()
    connection.close()

    logger.info(f"📊 {market.upper()}: обработано {len(rows)} строк, изменено {updated}")
    logger.success(f"✅ Пересчёт завершён для {market.upper()}")
    return updated


# --------------------------------------------------------------------------- #
# Переключение маркетплейсов
# --------------------------------------------------------------------------- #
def _zero_stock_sender(market: str) -> None:
    """Финальное обнуление остатков перед выключением канала."""
    from services.marketplace_cycle import send_zero_stocks_once

    send_zero_stocks_once(market)


def toggle_market(
    market: str,
    *,
    zero_stock_sender: Callable[[str], None] | None = None,
    actor: str = "streamlit",
) -> dict:
    """Переключить маркетплейс ON/OFF, синхронизировав базу и флаги.

    Логика сохранена как в Flask-версии: перед выключением остатки один раз
    обнуляются на стороне маркетплейса, а значения «Нал» сохраняются в
    ``temp_stock_backup.db``, чтобы восстановить их при включении.
    """
    if market not in ALLOWED_MARKETS:
        raise MarketControlError(f"Неизвестный маркетплейс: {market}")

    from services.control_store import record_operator_action

    zero_stock_sender = zero_stock_sender or _zero_stock_sender
    flags = load_flags()
    previous_state = bool(flags[market])
    target_state = not previous_state

    connection_main = None
    connection_backup = None
    state_committed = False

    try:
        # ШАГ 1. Перед выключением один раз обнуляем остатки на маркетплейсе.
        if not target_state:
            zero_stock_sender(market)

        # ШАГ 2. Только после успешного ответа API фиксируем состояние.
        flags[market] = target_state
        save_flags(flags)
        logger.info(f"🟡 Переключение {market}: {'ON' if target_state else 'OFF'}")

        connection_main = sqlite3.connect(DB_PATH, timeout=10)
        cursor = connection_main.cursor()
        connection_backup = sqlite3.connect(TEMP_BACKUP_DB_PATH, timeout=10)
        backup_cursor = connection_backup.cursor()

        if not target_state:
            backup_cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {market}_backup (
                    Маркетплейс TEXT,
                    Sklad TEXT,
                    Нал INTEGER,
                    PRIMARY KEY (Маркетплейс, Sklad)
                )
                """
            )
            backup_cursor.execute(f"DELETE FROM {market}_backup")
            cursor.execute(
                """
                SELECT Sklad, Нал
                  FROM marketplace
                 WHERE LOWER(TRIM("Маркетплейс")) = LOWER(TRIM(?))
                """,
                (market.lower(),),
            )
            data = cursor.fetchall()
            backup_cursor.executemany(
                f"INSERT INTO {market}_backup (Маркетплейс, Sklad, Нал) VALUES (?, ?, ?)",
                [(market, article, stock) for article, stock in data],
            )
            cursor.execute(
                """
                UPDATE marketplace
                   SET Нал = 0
                 WHERE LOWER(TRIM("Маркетплейс")) = LOWER(TRIM(?))
                """,
                (market.lower(),),
            )
            logger.info(f"📦 {market}: сохранено {len(data)} строк, остатки обнулены")
        else:
            backup_cursor.execute(
                f"""
                SELECT Sklad, Нал
                  FROM {market}_backup
                 WHERE LOWER(TRIM("Маркетплейс")) = LOWER(TRIM(?))
                """,
                (market.lower(),),
            )
            backup_data = backup_cursor.fetchall()
            for article, stock in backup_data:
                cursor.execute(
                    """
                    UPDATE marketplace
                       SET Нал = ?
                     WHERE LOWER(TRIM("Маркетплейс")) = LOWER(TRIM(?))
                       AND TRIM(COALESCE(Sklad,'')) = TRIM(?)
                    """,
                    (stock, market.lower(), article),
                )
            backup_cursor.execute(f"DELETE FROM {market}_backup")
            logger.info(f"🔁 {market}: восстановлено {len(backup_data)} строк")

        connection_main.commit()
        connection_backup.commit()
        state_committed = True

        recompute_marketplace_core(market, flags)

        record_operator_action(
            actor,
            "market.toggle",
            f"{market} {'включён' if target_state else 'выключен'}",
            target_type="market",
            target_code=market,
            details={"enabled": target_state},
        )
        return {"market": market, "enabled": target_state}

    except Exception as error:
        for connection in (connection_main, connection_backup):
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
        if not state_committed:
            flags = load_flags()
            flags[market] = previous_state
            save_flags(flags)
        logger.exception("❌ toggle_market failed")
        raise MarketControlError(
            f"Не удалось переключить {market}. Маркетплейс не ответил "
            "или соединение было прервано. Попробуйте ещё раз."
        ) from error
    finally:
        for connection in (connection_main, connection_backup):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass


# --------------------------------------------------------------------------- #
# Переключение поставщиков
# --------------------------------------------------------------------------- #
def toggle_supplier(supplier: str, *, actor: str = "streamlit") -> dict:
    """Переключить поставщика ON/OFF с сохранением остатков в бэкап-таблицы."""
    if supplier not in ALLOWED_SUPPLIERS:
        raise MarketControlError(f"Неизвестный поставщик: {supplier}")

    from services.control_store import record_operator_action

    flags = load_flags()
    previous_state = bool(flags["suppliers"].get(supplier, True))
    target_state = not previous_state

    connection_main = None
    connection_temp = None
    state_committed = False

    try:
        flags["suppliers"][supplier] = target_state
        save_flags(flags)
        logger.info(f"🔁 Поставщик {supplier} переключён: {'ON' if target_state else 'OFF'}")

        column_by_supplier = {"Invask": "Invask", "Okno": "Okno", "United": "United", "Sklad": None}
        column = column_by_supplier.get(supplier)
        if supplier == "Sklad":
            where_clause = """
                COALESCE(Invask,'')='' AND COALESCE(Okno,'')='' AND COALESCE(United,'')=''
                AND TRIM(COALESCE(Sklad,''))<>'' AND Маркетплейс = ?
            """
        else:
            where_clause = f"{column} IS NOT NULL AND TRIM({column}) <> '' AND Маркетплейс = ?"

        connection_main = sqlite3.connect(DB_PATH, timeout=10)
        connection_temp = sqlite3.connect(TEMP_BACKUP_DB_PATH, timeout=10)
        cursor_main = connection_main.cursor()
        cursor_temp = connection_temp.cursor()

        for market in ("yandex", "ozon", "wildberries"):
            if not bool(flags.get(market, True)):
                logger.info(f"⏭ {market.upper()} выключен → поставщика {supplier} не трогаем")
                continue

            table_backup = f"backup_supplier_{supplier}_{market}"
            cursor_main.execute(f"SELECT Sklad, Нал FROM marketplace WHERE {where_clause}", (market,))
            rows = cursor_main.fetchall()

            if not target_state:
                cursor_temp.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table_backup} (
                        Sklad TEXT PRIMARY KEY,
                        Нал INTEGER
                    )
                    """
                )
                cursor_temp.execute(f"DELETE FROM {table_backup}")
                for article, stock in rows:
                    cursor_temp.execute(
                        f"INSERT INTO {table_backup} (Sklad, Нал) VALUES (?, ?)",
                        (article, stock),
                    )
                cursor_main.execute(f"UPDATE marketplace SET Нал = 0 WHERE {where_clause}", (market,))
            else:
                for article, _ in rows:
                    cursor_temp.execute(
                        f"SELECT Нал FROM {table_backup} WHERE Sklad = ?", (article,)
                    )
                    result = cursor_temp.fetchone()
                    if result:
                        cursor_main.execute(
                            """
                            UPDATE marketplace
                               SET Нал = ?
                             WHERE Sklad = ? AND Маркетплейс = ?
                            """,
                            (result[0], article, market),
                        )
                        cursor_temp.execute(
                            f"DELETE FROM {table_backup} WHERE Sklad = ?", (article,)
                        )

        connection_main.commit()
        connection_temp.commit()
        state_committed = True

        for market in ("yandex", "ozon", "wildberries"):
            recompute_marketplace_core(market, flags)

        record_operator_action(
            actor,
            "supplier.toggle",
            f"Поставщик {supplier} {'включён' if target_state else 'выключен'}",
            target_type="supplier",
            target_code=supplier,
            details={"enabled": target_state},
        )
        return {"supplier": supplier, "enabled": target_state}

    except Exception as error:
        for connection in (connection_main, connection_temp):
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
        if not state_committed:
            flags = load_flags()
            flags["suppliers"][supplier] = previous_state
            save_flags(flags)
        logger.exception("❌ toggle_supplier failed")
        raise MarketControlError(
            f"Не удалось переключить поставщика {supplier}: {type(error).__name__}"
        ) from error
    finally:
        for connection in (connection_main, connection_temp):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass


# --------------------------------------------------------------------------- #
# Чтение таблиц для интерфейса
# --------------------------------------------------------------------------- #
def load_market_table(market: str, limit: int | None = None):
    """Прочитать таблицу маркетплейса из ``marketplace_base.db``."""
    import pandas as pd

    if market not in ALLOWED_MARKETS:
        raise ValueError(f"Неизвестный маркетплейс: {market}")

    query = """
        SELECT rowid AS "_rowid", Маркетплейс, Модель, Sklad, Invask, Okno, United,
               "WB Barcode", Статус, "%", Нал, Опт, Цена, Комментарий, "Дата изменения"
          FROM marketplace
         WHERE Маркетплейс = ?
         ORDER BY Модель COLLATE NOCASE
    """
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        frame = pd.read_sql_query(query, connection, params=(market,))
    if limit is not None:
        frame = frame.head(limit)
    return frame


def save_market_table_edits(market: str, edited) -> int:
    """Сохранить изменения, сделанные пользователем в таблице Streamlit."""
    import pandas as pd

    if market not in ALLOWED_MARKETS:
        raise ValueError(f"Неизвестный маркетплейс: {market}")

    editable = ["Статус", "%", "Нал", "Опт", "Цена", "Комментарий"]
    changed = 0
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        cursor = connection.cursor()
        for _, row in edited.iterrows():
            rowid = row.get("_rowid")
            if pd.isna(rowid):
                continue
            assignments = []
            values = []
            for column in editable:
                if column not in edited.columns:
                    continue
                value = row.get(column)
                if isinstance(value, float) and pd.isna(value):
                    value = None
                assignments.append(f'"{column}" = ?')
                values.append(value)
            assignments.append('"Дата изменения" = ?')
            values.append(datetime.now().strftime("%d.%m.%Y %H:%M"))
            values.append(int(rowid))
            cursor.execute(
                f"UPDATE marketplace SET {', '.join(assignments)} WHERE rowid = ?",
                values,
            )
            changed += cursor.rowcount
        connection.commit()

    recompute_marketplace_core(market)
    return changed
