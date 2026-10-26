from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_DB_PATH = PROJECT_ROOT / "System" / "control_center.db"

PROCESS_DEFINITIONS = (
    (
        "marketplace_cycle",
        "Полная синхронизация",
        "Склад → остатки → заказы → цены",
        "cycle",
    ),
    (
        "refresh_sklad",
        "Обновление склада",
        "Получает данные поставщиков и обновляет локальную базу",
        "cycle_step",
    ),
    (
        "stocks",
        "Остатки маркетплейсов",
        "Публикует актуальные остатки в Yandex, Ozon и Wildberries",
        "cycle_step",
    ),
    (
        "orders",
        "Новые заказы",
        "Проверяет заказы, списывает остатки и отправляет уведомления",
        "cycle_step",
    ),
    (
        "prices",
        "Цены маркетплейсов",
        "Публикует рассчитанные цены на площадках",
        "cycle_step",
    ),
    (
        "ozon_actions",
        "Акции Ozon",
        "Проверяет и обновляет участие товаров в акциях",
        "scheduled",
    ),
    (
        "database_backup",
        "Резервное копирование",
        "Создаёт проверенную резервную копию товарной базы",
        "scheduled",
    ),
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    CONTROL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CONTROL_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_control_store() -> None:
    with connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS processes (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                kind TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_status TEXT NOT NULL DEFAULT 'never',
                last_message TEXT,
                last_started_at TEXT,
                last_finished_at TEXT,
                last_success_at TEXT,
                last_duration_seconds REAL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                current_run_id INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS process_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_code TEXT NOT NULL,
                trigger TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT,
                duration_seconds REAL,
                details_json TEXT,
                FOREIGN KEY (process_code) REFERENCES processes(code)
            );

            CREATE INDEX IF NOT EXISTS idx_process_runs_started
            ON process_runs(started_at DESC);

            CREATE TABLE IF NOT EXISTS order_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL,
                order_id TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                product_summary TEXT,
                items_json TEXT,
                total_amount REAL,
                supplier TEXT,
                last_message TEXT,
                raw_json TEXT,
                UNIQUE (marketplace, order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_order_history_updated
            ON order_history(updated_at DESC);

            CREATE TABLE IF NOT EXISTS order_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL,
                order_id TEXT NOT NULL,
                step_code TEXT NOT NULL,
                step_name TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                UNIQUE (marketplace, order_id, step_code)
            );

            CREATE TABLE IF NOT EXISTS operator_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT,
                target_code TEXT,
                summary TEXT NOT NULL,
                details_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_operator_actions_created
            ON operator_actions(created_at DESC);
            """
        )
        timestamp = now_iso()
        for code, name, description, kind in PROCESS_DEFINITIONS:
            connection.execute(
                """
                INSERT INTO processes (
                    code, name, description, kind, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    kind = excluded.kind
                """,
                (code, name, description, kind, timestamp),
            )


def _decode_json_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("details_json", "items_json", "raw_json"):
        if key not in result:
            continue
        value = result.pop(key)
        try:
            result[key.removesuffix("_json")] = json.loads(value) if value else None
        except (TypeError, json.JSONDecodeError):
            result[key.removesuffix("_json")] = None
    return result


def list_processes() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM processes
            ORDER BY CASE code
                WHEN 'marketplace_cycle' THEN 1
                WHEN 'refresh_sklad' THEN 2
                WHEN 'stocks' THEN 3
                WHEN 'orders' THEN 4
                WHEN 'prices' THEN 5
                WHEN 'ozon_actions' THEN 6
                WHEN 'database_backup' THEN 7
                ELSE 99
            END
            """
        ).fetchall()
    return [dict(row) for row in rows]

def set_process_status(process_code: str, status: str, message: str) -> None:
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            UPDATE processes
            SET last_status = ?,
                last_message = ?,
                last_started_at = CASE
                    WHEN ? = 'running' THEN ?
                    ELSE last_started_at
                END,
                last_finished_at = CASE
                    WHEN ? IN ('success', 'error', 'skipped') THEN ?
                    ELSE last_finished_at
                END,
                last_success_at = CASE
                    WHEN ? = 'success' THEN ?
                    ELSE last_success_at
                END,
                consecutive_failures = CASE
                    WHEN ? = 'success' THEN 0
                    WHEN ? = 'error' THEN consecutive_failures + 1
                    ELSE consecutive_failures
                END,
                updated_at = ?
            WHERE code = ?
            """,
            (
                status,
                message[:1000],
                status,
                timestamp,
                status,
                timestamp,
                status,
                timestamp,
                status,
                status,
                timestamp,
                process_code,
            ),
        )


def start_process_run(process_code: str, trigger: str) -> int:
    timestamp = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO process_runs (
                process_code, trigger, started_at, status
            ) VALUES (?, ?, ?, 'running')
            """,
            (process_code, trigger, timestamp),
        )
        run_id = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE processes
            SET last_status = 'running',
                last_message = 'Выполняется',
                last_started_at = ?,
                current_run_id = ?,
                updated_at = ?
            WHERE code = ?
            """,
            (timestamp, run_id, timestamp, process_code),
        )
    return run_id


def finish_process_run(
    run_id: int,
    process_code: str,
    *,
    status: str,
    message: str,
    duration_seconds: float,
    details: dict[str, Any] | None = None,
) -> None:
    timestamp = now_iso()
    successful = status == "success"
    with connect() as connection:
        connection.execute(
            """
            UPDATE process_runs
            SET finished_at = ?,
                status = ?,
                message = ?,
                duration_seconds = ?,
                details_json = ?
            WHERE id = ?
            """,
            (
                timestamp,
                status,
                message[:2000],
                duration_seconds,
                json.dumps(details, ensure_ascii=False) if details else None,
                run_id,
            ),
        )
        connection.execute(
            """
            UPDATE processes
            SET last_status = ?,
                last_message = ?,
                last_finished_at = ?,
                last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                last_duration_seconds = ?,
                consecutive_failures = CASE
                    WHEN ? THEN 0
                    ELSE consecutive_failures + 1
                END,
                current_run_id = NULL,
                updated_at = ?
            WHERE code = ?
            """,
            (
                status,
                message[:1000],
                timestamp,
                int(successful),
                timestamp,
                duration_seconds,
                int(successful),
                timestamp,
                process_code,
            ),
        )


def record_cycle_steps(run_id: int, steps: dict[str, Any]) -> None:
    timestamp = now_iso()
    with connect() as connection:
        for code, value in steps.items():
            status = "skipped" if value is None else ("success" if value else "error")
            connection.execute(
                """
                UPDATE processes
                SET last_status = ?,
                    last_message = ?,
                    last_finished_at = ?,
                    last_success_at = CASE
                        WHEN ? = 'success' THEN ?
                        ELSE last_success_at
                    END,
                    consecutive_failures = CASE
                        WHEN ? = 'success' THEN 0
                        WHEN ? = 'error' THEN consecutive_failures + 1
                        ELSE consecutive_failures
                    END,
                    updated_at = ?
                WHERE code = ?
                """,
                (
                    status,
                    {
                        "success": "Этап завершён",
                        "error": "Этап завершён с ошибкой",
                        "skipped": "Этап пропущен",
                    }[status],
                    timestamp,
                    status,
                    timestamp,
                    status,
                    status,
                    timestamp,
                    code,
                ),
            )


def list_process_runs(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 500))
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT r.*, p.name AS process_name
            FROM process_runs r
            JOIN processes p ON p.code = r.process_code
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [_decode_json_fields(row) for row in rows]


def upsert_order(
    marketplace: str,
    order_id: str,
    *,
    status: str,
    product_summary: str | None = None,
    items: list[dict[str, Any]] | None = None,
    total_amount: float | None = None,
    supplier: str | None = None,
    message: str | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO order_history (
                marketplace, order_id, detected_at, updated_at, status,
                product_summary, items_json, total_amount, supplier,
                last_message, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marketplace, order_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                status = excluded.status,
                product_summary = COALESCE(
                    excluded.product_summary, order_history.product_summary
                ),
                items_json = COALESCE(excluded.items_json, order_history.items_json),
                total_amount = COALESCE(
                    excluded.total_amount, order_history.total_amount
                ),
                supplier = COALESCE(excluded.supplier, order_history.supplier),
                last_message = excluded.last_message,
                raw_json = COALESCE(excluded.raw_json, order_history.raw_json)
            """,
            (
                marketplace,
                str(order_id),
                timestamp,
                timestamp,
                status,
                product_summary,
                json.dumps(items, ensure_ascii=False) if items else None,
                total_amount,
                supplier,
                message,
                json.dumps(raw, ensure_ascii=False) if raw else None,
            ),
        )


def record_order_step(
    marketplace: str,
    order_id: str,
    step_code: str,
    step_name: str,
    status: str,
    message: str | None = None,
) -> None:
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO order_steps (
                marketplace, order_id, step_code, step_name, status,
                message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marketplace, order_id, step_code) DO UPDATE SET
                status = excluded.status,
                message = excluded.message,
                updated_at = excluded.updated_at,
                attempts = order_steps.attempts + 1
            """,
            (
                marketplace,
                str(order_id),
                step_code,
                step_name,
                status,
                message,
                timestamp,
                timestamp,
            ),
        )


def list_orders(
    *,
    query: str = "",
    marketplace: str = "",
    status: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if query:
        clauses.append(
            "(order_id LIKE ? OR product_summary LIKE ? OR supplier LIKE ?)"
        )
        pattern = f"%{query.strip()}%"
        values.extend((pattern, pattern, pattern))
    if marketplace:
        clauses.append("marketplace = ?")
        values.append(marketplace)
    if status:
        clauses.append("status = ?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(1, min(int(limit), 500)))
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM order_history
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            values,
        ).fetchall()
    return [_decode_json_fields(row) for row in rows]


def get_order(marketplace: str, order_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM order_history
            WHERE marketplace = ? AND order_id = ?
            """,
            (marketplace, str(order_id)),
        ).fetchone()
        if row is None:
            return None
        steps = connection.execute(
            """
            SELECT *
            FROM order_steps
            WHERE marketplace = ? AND order_id = ?
            ORDER BY id
            """,
            (marketplace, str(order_id)),
        ).fetchall()
    result = _decode_json_fields(row)
    result["steps"] = [dict(step) for step in steps]
    return result


def delete_order(marketplace: str, order_id: str) -> bool:
    """Delete an order and its recorded processing steps from the local control DB."""
    normalized_order_id = str(order_id)
    with connect() as connection:
        exists = connection.execute(
            """
            SELECT 1
            FROM order_history
            WHERE marketplace = ? AND order_id = ?
            """,
            (marketplace, normalized_order_id),
        ).fetchone()
        if exists is None:
            return False
        connection.execute(
            "DELETE FROM order_steps WHERE marketplace = ? AND order_id = ?",
            (marketplace, normalized_order_id),
        )
        connection.execute(
            "DELETE FROM order_history WHERE marketplace = ? AND order_id = ?",
            (marketplace, normalized_order_id),
        )
    return True

def record_operator_action(
    actor: str,
    action: str,
    summary: str,
    *,
    target_type: str | None = None,
    target_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO operator_actions (
                created_at, actor, action, target_type,
                target_code, summary, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                actor,
                action,
                target_type,
                target_code,
                summary,
                json.dumps(details, ensure_ascii=False) if details else None,
            ),
        )


def list_operator_actions(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 500))
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM operator_actions
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [_decode_json_fields(row) for row in rows]
