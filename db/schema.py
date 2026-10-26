from __future__ import annotations

from pathlib import Path

from db.connections import transaction


def ensure_indexes(
    marketplace_db: str | Path = "System/marketplace_base.db",
    suppliers_db: str | Path = "System/!YMWB.db",
) -> None:
    """Create only non-unique indexes that match the application's hot lookups."""
    with transaction(marketplace_db, immediate=True) as connection:
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_marketplace_market_sklad '
            'ON marketplace("Маркетплейс", "Sklad")'
        )
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_marketplace_sklad '
            'ON marketplace("Sklad")'
        )
    with transaction(suppliers_db, immediate=True) as connection:
        connection.execute(
            'CREATE INDEX IF NOT EXISTS idx_prices_supplier_article '
            'ON prices("Поставщик", "Артикул")'
        )

