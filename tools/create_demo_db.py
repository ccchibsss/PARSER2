#!/usr/bin/env python3
"""Создать демонстрационные базы, чтобы посмотреть панель без ключей API.

Скрипт **не перезаписывает** существующие файлы (если нужна перезапись —
запустите с ``--force``).

    python tools/create_demo_db.py

Создаёт:
* ``System/marketplace_base.db`` — таблица ``marketplace`` (остатки/цены по МП);
* ``System/!YMWB.db`` — таблица ``prices`` (остатки поставщиков);
* маркер ``System/.demo_mode`` — панель показывает плашку «демо-данные».
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_DIR = PROJECT_ROOT / "System"
MARKETPLACE_DB = SYSTEM_DIR / "marketplace_base.db"
SUPPLIER_DB = SYSTEM_DIR / "!YMWB.db"
DEMO_MARKER = SYSTEM_DIR / ".demo_mode"

MARKETS = ("wildberries", "ozon", "yandex")
MODELS = (
    "Кресло офисное Ergo", "Стол письменный Дуб", "Стеллаж 4 полки", "Кровать Джулия",
    "Шкаф 2-дверный", "Комод Топаз", "Диван-кровать Оскар", "Тумба прикроватная",
    "Полка навесная", "Табурет Лофт", "Кухонный стол 120", "Матрас Ортопедик",
)


def build_rows(randomizer: random.Random) -> list[dict]:
    rows: list[dict] = []
    for index in range(len(MODELS)):
        article = f"SKL{1000 + index}"
        invask = f"INV{2000 + index}"
        okno = f"OKN{3000 + index}" if index % 3 else ""
        united = f"UNI{4000 + index}" if index % 4 else ""
        for market in MARKETS:
            rows.append(
                {
                    "Маркетплейс": market,
                    "Модель": MODELS[index],
                    "Sklad": article,
                    "Invask": invask,
                    "Okno": okno,
                    "United": united,
                    "WB Barcode": f"20{index:010d}" if market == "wildberries" else "",
                    "WB Артикул": f"WB{index:05d}" if market == "wildberries" else "",
                    "Статус": "выкл." if index == len(MODELS) - 1 else "вкл.",
                    "%": randomizer.choice([30, 35, 40, 45, 50]),
                    "Нал": randomizer.randint(0, 12),
                    "Опт": 0,
                    "Цена": 0,
                    "Комментарий": "демо-строка",
                    "Дата изменения": datetime.now().strftime("%d.%m.%Y %H:%M"),
                }
            )
        # остатки поставщиков
        rows.append(
            {
                "__prices__": True,
                "Поставщик": "Sklad",
                "Артикул": article,
                "Наименование": MODELS[index],
                "Наличие": randomizer.randint(0, 15),
                "ОПТ": randomizer.randint(1200, 9000),
                "РРЦ": 0,
            }
        )
        rows.append(
            {
                "__prices__": True,
                "Поставщик": "Invask",
                "Артикул": invask,
                "Наименование": MODELS[index],
                "Наличие": randomizer.randint(0, 9),
                "ОПТ": randomizer.randint(1100, 8800),
                "РРЦ": 0,
            }
        )
    return rows


CREATE_MARKETPLACE = """
CREATE TABLE IF NOT EXISTS marketplace (
    "Маркетплейс" TEXT,
    "Модель" TEXT,
    "Sklad" TEXT,
    "Invask" TEXT,
    "Okno" TEXT,
    "United" TEXT,
    "WB Barcode" TEXT,
    "WB Артикул" TEXT,
    "Статус" TEXT,
    "%" INTEGER,
    "Нал" INTEGER,
    "Опт" INTEGER,
    "Цена" INTEGER,
    "Комментарий" TEXT,
    "Дата изменения" TEXT
)
"""

CREATE_PRICES = """
CREATE TABLE IF NOT EXISTS prices (
    "Поставщик" TEXT,
    "Артикул" TEXT,
    "Наименование" TEXT,
    "Наличие" INTEGER,
    "ОПТ" INTEGER,
    "РРЦ" INTEGER
)
"""

MARKETPLACE_COLUMNS = (
    "Маркетплейс", "Модель", "Sklad", "Invask", "Okno", "United",
    "WB Barcode", "WB Артикул", "Статус", "%", "Нал", "Опт", "Цена",
    "Комментарий", "Дата изменения",
)
PRICES_COLUMNS = ("Поставщик", "Артикул", "Наименование", "Наличие", "ОПТ", "РРЦ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="перезаписать существующие базы")
    arguments = parser.parse_args()

    SYSTEM_DIR.mkdir(parents=True, exist_ok=True)

    if MARKETPLACE_DB.exists() and not arguments.force:
        print(f"Файл {MARKETPLACE_DB} уже существует — ничего не меняю (--force для перезаписи).")
        return 0

    randomizer = random.Random(42)
    rows = build_rows(randomizer)
    marketplace_rows = [row for row in rows if not row.get("__prices__")]
    prices_rows = [dict(row) for row in rows if row.get("__prices__")]

    for path, statement in ((MARKETPLACE_DB, CREATE_MARKETPLACE), (SUPPLIER_DB, CREATE_PRICES)):
        if path.exists() and arguments.force:
            path.unlink()

    with sqlite3.connect(MARKETPLACE_DB) as connection:
        connection.execute(CREATE_MARKETPLACE)
        connection.execute('CREATE INDEX IF NOT EXISTS idx_marketplace_market_sklad ON marketplace("Маркетплейс","Sklad")')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_marketplace_sklad ON marketplace("Sklad")')
        placeholders = ", ".join("?" for _ in MARKETPLACE_COLUMNS)
        columns = ", ".join(f'"{column}"' for column in MARKETPLACE_COLUMNS)
        connection.executemany(
            f"INSERT INTO marketplace ({columns}) VALUES ({placeholders})",
            [tuple(row.get(column, "") for column in MARKETPLACE_COLUMNS) for row in marketplace_rows],
        )

    with sqlite3.connect(SUPPLIER_DB) as connection:
        connection.execute(CREATE_PRICES)
        connection.execute('CREATE INDEX IF NOT EXISTS idx_prices_supplier_article ON prices("Поставщик","Артикул")')
        placeholders = ", ".join("?" for _ in PRICES_COLUMNS)
        columns = ", ".join(f'"{column}"' for column in PRICES_COLUMNS)
        connection.executemany(
            f"INSERT INTO prices ({columns}) VALUES ({placeholders})",
            [tuple(row.get(column) for column in PRICES_COLUMNS) for row in prices_rows],
        )

    DEMO_MARKER.write_text("demo\n", encoding="utf-8")

    print(
        f"✅ Демо-данные созданы:\n"
        f"   {MARKETPLACE_DB} — строк: {len(marketplace_rows)}\n"
        f"   {SUPPLIER_DB} — строк: {len(prices_rows)}\n"
        f"Теперь запустите: streamlit run streamlit_app.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
