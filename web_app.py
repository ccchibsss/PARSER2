import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort, has_request_context
from apscheduler.schedulers.background import BackgroundScheduler
from update_sklad import gen_sklad, update_sklad_db, upsert_ymwb_prices_from_sklad
from auto_stock_updater import update
from datetime import datetime
import pandas as pd
from flask import session, Response
from functools import wraps
from dotenv import load_dotenv
import os
import re
from logger_config import logger, LOG_DIR
from pathlib import Path
from datetime import timedelta
import stock
import json
import sqlite3
import secrets
import atexit
from threading import Lock
from flask import send_file
from copy import deepcopy
from io import BytesIO
from unlisted import generate_unlisted
from ozon_actions import remove_all_products_from_all_actions
from db.backups import create_rotating_backup
from db.schema import ensure_indexes
from services.automation_state import (
    load_automation_state,
    now_iso,
    reset_interrupted_run,
    set_automation_enabled,
    set_automation_interval,
    update_automation_state,
)
from services.market_schedule import (
    get_market_schedule_status,
    load_market_schedule,
    save_market_schedule,
)
from services.control_store import (
    delete_order,
    finish_process_run,
    get_order,
    init_control_store,
    list_operator_actions,
    list_orders,
    list_process_runs,
    list_processes,
    record_cycle_steps,
    record_operator_action,
    start_process_run,
)
from utilities.atomic_files import atomic_write_json, atomic_write_text
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash


last_download_time = None
LAST_UPDATE_FILE = "System/last_update.txt"
FLAGS_PATH = "System/stock_flags.json"

# Глобальные флаги синхронизации (False = маркетплейс полностью исключён из цикла)
global_stock_flags = {
    "yandex": True,
    "ozon": True,
    "wildberries": True
}
toggle_lock = Lock()
automation_cycle_lock = Lock()
scheduler_start_lock = Lock()
scheduler_instance = None
scheduler_shutdown_registered = False

def send_telegram_message(message: str):
    stock.telegram.notify(
        token=stock.telegram_got_token_error,
        chat_id=stock.telegram_chat_id_error,
        message=message
    )


CRON_SUPPLIERS = ("Invask", "Okno", "United", "Sklad")
MARKET_SCHEDULE_TARGET = "wildberries"
MARKET_SCHEDULE_JOB_IDS = (
    "market-schedule-off",
    "market-schedule-on",
)


def set_supplier_state_if_needed(supplier: str, enabled: bool):
    try:
        current = global_stock_flags.get("suppliers", {}).get(supplier, True)

        if current == enabled:
            state = "ON" if enabled else "OFF"
            logger.info(f"⏱️ CRON: {supplier} уже {state} → ничего не делаем")
            return

        action = "включаем" if enabled else "выключаем"
        logger.info(f"⏱️ CRON: {supplier} сейчас {'ON' if current else 'OFF'} → {action} (toggle)")

        with app.app_context():
            toggle_supplier(supplier)

    except Exception as e:
        action = "включении" if enabled else "отключении"
        logger.warning(f"❌ CRON: Ошибка при {action} {supplier}: {e}")


def set_market_state_if_needed(market: str, enabled: bool) -> None:
    """Приводит канал продаж к нужному состоянию без лишнего переключения."""
    try:
        current = bool(global_stock_flags.get(market, True))
        if current == enabled:
            logger.info(
                f"📆 Недельное расписание WB: {market} уже "
                f"{'включён' if enabled else 'выключен'}"
            )
            return

        logger.info(
            "📆 Недельное расписание WB: выполняется "
            f"{'включение' if enabled else 'выключение'} {market}"
        )
        with app.app_context():
            toggle_stock(market)

        if bool(global_stock_flags.get(market, True)) != enabled:
            raise RuntimeError("канал не перешёл в требуемое состояние")
        logger.success(
            f"✅ Недельное расписание WB: {market} успешно "
            f"{'включён' if enabled else 'выключен'}"
        )
    except Exception as error:
        logger.warning(
            f"❌ Недельное расписание WB: не удалось "
            f"{'включить' if enabled else 'выключить'} {market}: "
            f"{type(error).__name__}: {error}"
        )


def apply_market_schedule_event(enabled: bool) -> None:
    """Включает или выключает Wildberries по событию недельного расписания."""
    set_market_state_if_needed(MARKET_SCHEDULE_TARGET, enabled)


def configure_market_schedule_jobs(scheduler: BackgroundScheduler) -> None:
    """Создаёт будущие еженедельные события без изменения текущего состояния."""
    for job_id in MARKET_SCHEDULE_JOB_IDS:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)

    schedule = load_market_schedule()
    if not schedule["enabled"]:
        return

    off_hour, off_minute = (int(part) for part in schedule["off_time"].split(":"))
    on_hour, on_minute = (int(part) for part in schedule["on_time"].split(":"))
    scheduler.add_job(
        apply_market_schedule_event,
        "cron",
        day_of_week=schedule["off_day"],
        hour=off_hour,
        minute=off_minute,
        args=[False],
        id="market-schedule-off",
        replace_existing=True,
    )
    scheduler.add_job(
        apply_market_schedule_event,
        "cron",
        day_of_week=schedule["on_day"],
        hour=on_hour,
        minute=on_minute,
        args=[True],
        id="market-schedule-on",
        replace_existing=True,
    )


def run_ozon_actions_if_enabled() -> None:
    """Не обращается к Ozon Actions, когда маркетплейс полностью выключен."""
    if not bool(global_stock_flags.get("ozon", True)):
        logger.info("⏭ Ozon выключен → проверка акций пропущена")
        return
    remove_all_products_from_all_actions()


def backup_database():
    backup_filename = create_rotating_backup(
        "System/marketplace_base.db",
        "System/backups",
        keep=14,
    )
    logger.info(f"💾 Бэкап базы создан: {backup_filename}")

def get_last_download_time():
    if os.path.exists(LAST_UPDATE_FILE):
        with open(LAST_UPDATE_FILE, "r") as f:
            return f.read().strip()
    return None


def update_sklad_task():
    """Обновление склада: делегирует в services.cycle_runner (общий код)."""
    from services.cycle_runner import refresh_sklad_task

    try:
        with toggle_lock:
            refresh_sklad_task()
            global last_download_time
            last_download_time = get_last_download_time()
    except Exception:
        logger.exception("❌ Ошибка при обновлении склада")
        raise


def load_stock_flags():
    try:
        with open(FLAGS_PATH, 'r') as f:
            flags = json.load(f)
            if "suppliers" not in flags:
                flags["suppliers"] = {}
            return flags
    except Exception:
        return {
            "yandex": True,
            "ozon": True,
            "wildberries": True,
            "suppliers": {}
        }


SUPPLIERS = ['Invask', 'Okno', 'United']  # приоритет на равных ценах: Invask > Okno > United (можно поменять)

# Кэшируем авто-обнаруженную таблицу с колонками Поставщик/Артикул/Наличие/ОПТ
_SUP_TBL_CACHE = None
_SUP_DB_PATH = "System/!YMWB.db"


def _normalise_article(value) -> str:
    compact = "".join(str(value or "").split())
    return compact.lstrip("0") or ("0" if compact else "")


from services.market_controls import (  # noqa: E402  (единая бизнес-логика)
    MarketControlError,
    choose_best_supplier_for_row,
    load_supplier_catalog,
    load_flags as _load_flags_from_disk,
    recompute_marketplace_core as _recompute_marketplace_core_impl,
    save_market_table_edits,
    toggle_market as _toggle_market_impl,
    toggle_supplier as _toggle_supplier_impl,
)
from services.market_controls import calc_price as _calc_price  # noqa: E402


global_stock_flags = load_stock_flags()

app = Flask(__name__)
DB_PATH = "System/marketplace_base.db"
load_dotenv(dotenv_path=os.path.join("System", ".env"))
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
if not os.getenv("SECRET_KEY"):
    logger.warning("SECRET_KEY не задан: используется временный ключ процесса; сессии сбросятся после перезапуска")
USERNAME = "admin"
PASSWORD = os.getenv('PASSWORD')
PASSWORD_HASH = os.getenv("PASSWORD_HASH")
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    MAX_CONTENT_LENGTH=int(os.getenv("YMWB_MAX_REQUEST_BYTES", str(1024 * 1024))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("YMWB_HTTPS", "").lower() in {"1", "true", "yes"},
)

SAFE_ENDPOINTS = {"login", "static", "favicon"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALLOWED_MARKETS = {"yandex", "ozon", "wildberries"}
ALLOWED_SUPPLIERS = {"Invask", "Okno", "United", "Sklad"}
ALLOWED_COLUMNS = {
    "Sklad", "Invask", "Okno", "United", "WB Barcode", "WB Артикул",
    "Модель", "Статус", "Нал", "Опт", "%", "Цена", "Комментарий",
    "Дата изменения", "Маркетплейс",
}


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_request():
    # Запуск фонового планировщика при первом запросе к Flask-приложению.
    # Это работает и при прямом запуске web_app.py, и при команде `flask run`.
    ensure_scheduler_started()

    endpoint = request.endpoint or ""
    if endpoint not in SAFE_ENDPOINTS and not session.get("logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            abort(401)
        return redirect(url_for("login"))
    if request.method in MUTATING_METHODS:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not secrets.compare_digest(str(supplied), str(expected)):
            abort(400, description="Недействительный CSRF-токен")

@app.after_request
def no_index_headers(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'"
    )
    return response

@app.route('/favicon.ico')
def favicon():
    from flask import send_from_directory
    import os
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'icons.png',
        mimetype='image/png'
    )

@app.route('/toggle_stock/<market>', methods=['POST'])
def toggle_stock(market):
    if market not in ALLOWED_MARKETS:
        return jsonify({"status": "error", "message": "unknown market"}), 400

    actor = USERNAME if has_request_context() else "scheduler"
    try:
        with toggle_lock:
            result = _toggle_market_impl(market, actor=actor)
        return jsonify({"status": "ok", "market": result["market"], "enabled": result["enabled"]}), 200
    except MarketControlError as error:
        logger.error(f"❌ toggle_stock({market}): {error}")
        return jsonify({"status": "error", "message": str(error)}), 500


@app.route('/run_update', methods=['POST'])
def run_manual_update():
    try:
        update_sklad_task()
        logger.success("✅ Обновление по кнопке завершено.")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return '', 204
        return redirect(request.referrer or url_for('index'))
    except Exception as e:
        logger.exception("❌ Ошибка при обновлении через кнопку")
        return Response("Ошибка", status=500)


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        supplied_password = request.form.get("password", "")
        password_matches = (
            check_password_hash(PASSWORD_HASH, supplied_password)
            if PASSWORD_HASH
            else bool(PASSWORD) and secrets.compare_digest(supplied_password, PASSWORD)
        )
        username_matches = secrets.compare_digest(request.form.get("username", ""), USERNAME)
        if username_matches and password_matches:
            session.clear()
            session.permanent = True
            session['logged_in'] = True
            csrf_token()
            logger.success(f"👤 Успешный вход пользователя {USERNAME}")
            return redirect(url_for('index'))
        else:
            logger.warning("❌ Неверный логин или пароль при попытке входа")
            return "Неверный логин или пароль", 401
    return '''
        <!doctype html>
        <html>
        <head>
            <title>Вход</title>
            <meta charset="utf-8">
            <link rel="icon" href="/static/icons.png" type="image/png">
            <style>
                body {
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    background: #f0f2f5;
                    margin: 0;
                    font-family: 'Inter', sans-serif;
                }
                .login-container {
                    background: white;
                    padding: 30px 40px;
                    border-radius: 15px;
                    box-shadow: 0 4px 20px rgba(0,0,0,0.1);
                    width: 300px;
                    text-align: center;
                }
                .login-container h2 {
                    margin-bottom: 20px;
                    font-size: 24px;
                }
                .login-container input[type="text"],
                .login-container input[type="password"],
                .login-container input[type="submit"] {
                    width: 100%;
                    padding: 10px;
                    margin-bottom: 15px;
                    border: 1px solid #ccc;
                    border-radius: 8px;
                    font-size: 16px;
                    box-sizing: border-box;
                }
                .login-container input[type="submit"] {
                    background-color: #007bff;
                    color: white;
                    border: none;
                    cursor: pointer;
                    transition: background-color 0.3s;
                }
                .login-container input[type="submit"]:hover {
                    background-color: #0056b3;
                }
            </style>
        </head>
        <body>

        <div class="login-container">
            <h2>Вход</h2>
            <form method="post">
                <input type="hidden" name="_csrf_token" value="''' + csrf_token() + '''">
                <input type="text" name="username" placeholder="Логин" required><br>
                <input type="password" name="password" placeholder="Пароль" required><br>
                <input type="submit" value="Войти">
            </form>
        </div>

        </body>
        </html>
        '''

@app.route('/toggle_supplier/<supplier>', methods=['POST'])
def toggle_supplier(supplier):
    if supplier not in ALLOWED_SUPPLIERS:
        return jsonify({"status": "error", "message": "unknown supplier"}), 400

    actor = USERNAME if has_request_context() else "scheduler"
    try:
        with toggle_lock:
            result = _toggle_supplier_impl(supplier, actor=actor)
        return jsonify({"status": "ok", "supplier": result["supplier"], "enabled": result["enabled"]}), 200
    except MarketControlError as error:
        logger.error(f"❌ toggle_supplier({supplier}): {error}")
        return jsonify({"status": "error", "message": str(error)}), 500


@app.route('/')
@requires_auth
def index():
    return show_table('wildberries')


@app.route('/table/<table_name>')
@requires_auth
def show_table(table_name):
    if table_name not in ALLOWED_MARKETS:
        abort(404)
    logger.info(f"📊 Открыта таблица: {table_name}")
    sort_column = request.args.get("sort")
    sort_order = request.args.get("order")  # None, если параметра нет

    if not sort_column:
        # дефолтная сортировка по Модели
        sort_column = "Модель"
        sort_order = "asc"
    elif sort_column == "Нал" and sort_order is None:
        # 👇 для "Нал" первый клик = desc
        sort_order = "desc"
    elif sort_order is None:
        sort_order = "asc"
    last_download_time = get_last_download_time()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]


    query = "SELECT * FROM marketplace WHERE Маркетплейс = ?"
    df = pd.read_sql_query(query, conn, params=(table_name,))
    if "Маркетплейс" in df.columns:
        df.drop(columns=["Маркетплейс"], inplace=True)
    # Желаемый порядок колонок (WB — отдельный порядок)
    if table_name == "wildberries":
        preferred = [
            'Sklad', 'Invask', 'Okno', 'United',
            'WB Barcode', 'WB Артикул',
            'Модель',
            'Статус', 'Нал', 'Опт', '%', 'Цена',
            'Комментарий', 'Дата изменения'
        ]
    else:
        preferred = [
            'Sklad', 'Invask', 'Okno', 'United',
            'Модель',
            'Статус', 'Нал', 'Опт', '%', 'Цена',
            'Комментарий', 'Дата изменения'
        ]

    preferred = [c for c in preferred if c in df.columns]
    others = [c for c in df.columns if c not in preferred]
    df = df[preferred + others]
    if sort_column not in set(df.columns):
        sort_column = "Модель"
        sort_order = "asc"
    search_term = request.args.get('search', '').strip().lower()
    if search_term:
        df = df[df.apply(lambda row: any(
            search_term in str(row.get(col, '')).lower()
            for col in ['Sklad', 'Invask', 'Okno', 'United', 'Модель']
        ), axis=1)]
    letter_filter = request.args.get('letter', '').strip().lower()
    if letter_filter:
        if letter_filter == '0-9':
            df = df[df['Модель'].str.match(r'^\d', na=False)]
        elif letter_filter == 'а-я':
            df = df[df['Модель'].str.match(r'^[а-яА-Я]', na=False)]
        else:
            df = df[df['Модель'].str.lower().str.startswith(letter_filter)]
    if '_id' in df.columns:
        df.drop(columns=['_id'], inplace=True)
    # Преобразуем "Дата изменения" в datetime для правильной сортировки
    if "Дата изменения" in df.columns:
        try:
            df["Дата изменения"] = pd.to_datetime(df["Дата изменения"], format="%d.%m.%Y %H:%M", errors="coerce")
        except Exception as e:
            logger.warning(f"❌ Ошибка преобразования дат: {e}")
    if all(col in df.columns for col in ['Опт', '%', 'Цена', 'Нал']):
        def recalc_price(opt, markup):
            try:
                opt = float(str(opt).replace(' ', '').replace('р.', ''))
                markup = float(str(markup).replace('%', '').replace(' ', ''))
                return int(round((opt + (opt * markup / 100)) / 100.0) * 100)
            except:
                return None  # не трогаем, если не смогли посчитать

        mask = pd.to_numeric(df['Нал'], errors='coerce').fillna(0) > 0
        df.loc[mask, 'Цена'] = df.loc[mask].apply(
            lambda row: recalc_price(row['Опт'], row['%']), axis=1
        ).fillna(df.loc[mask, 'Цена'])
    conn.close()

    supplier_catalog = load_supplier_catalog()
    if sort_column and sort_column in df.columns:
        # 👇 Добавляем признак "выключен"
        df['_disabled_flag'] = df['Статус'].astype(str).str.lower().eq('выкл.').astype(int)

        # 👇 Особая логика для колонок поставщиков
        if sort_column in ["Sklad", "Invask", "Okno", "United"]:
            def highlight_sort(row):
                active_supplier = choose_best_supplier_for_row(row.to_dict(), supplier_catalog, use_row_sklad=True)[0]
                return 1 if active_supplier == sort_column else 0

            df['_highlight_sort'] = df.apply(highlight_sort, axis=1)
            # 🔑 по умолчанию (asc) цветные сверху
            df = df.sort_values(
                by=['_disabled_flag', '_highlight_sort'],
                ascending=[True, False if sort_order == "asc" else True]
            )
            df.drop(columns=['_highlight_sort'], inplace=True)

        # 👇 Обычная сортировка для остальных колонок
        elif sort_column == "Модель":
            df = df.sort_values(
                by=['_disabled_flag', sort_column],
                key=lambda x: x.str.lower() if x.name == sort_column else x,
                ascending=[True, sort_order == "asc"]
            )
        else:
            df = df.sort_values(
                by=['_disabled_flag', sort_column],
                ascending=[True, sort_order == "asc"]
            )

        # убираем временный флаг
        df.drop(columns=['_disabled_flag'], inplace=True)

    df.insert(0, "№", range(1, len(df) + 1))
    # Удаляем лишние столбцы для Yandex и Ozon
    if table_name != "wildberries":
        for col in ["WB Barcode", "WB Артикул"]:
            if col in df.columns:
                df.drop(columns=[col], inplace=True)
    # Если глобальный флаг отключен — принудительно ставим Нал = 0
    if not global_stock_flags.get(table_name, True):
        if 'Нал' in df.columns:
            df['Нал'] = 0
    # 👇 Мини-статистика
    total_rows = len(df)
    in_stock = df[df['Нал'].astype(str).str.replace(r'\D', '', regex=True).astype(float) > 0].shape[0]
    disabled = df[df['Статус'].astype(str).str.lower() == 'выкл.'].shape[0]

    price_col = 'Цена'

    def safe_avg(col):
        try:
            return round(
                pd.to_numeric(df[col].astype(str).str.replace(r'\D', '', regex=True), errors='coerce').dropna().mean())
        except:
            return 0

    avg_price = safe_avg(price_col)
    avg_markup = safe_avg('%')

    stats = {
        'Всего товаров': total_rows,
        'В наличии': in_stock,
        'Отключено': disabled,
        f'Средняя цена {price_col.split()[-1]}': f'{avg_price:,} р.'.replace(',', ' '),
        'Средняя наценка': f'{avg_markup} %'
    }

    suppliers_list = ["Invask", "Okno", "United", "Sklad"]

    saved_form_data = session.pop('saved_form', {})
    # Форматируем дату обратно в нужный вид (дд.мм.гггг чч:мм)
    if "Дата изменения" in df.columns:
        df["Дата изменения"] = df["Дата изменения"].dt.strftime("%d.%m.%Y %H:%M")
    has_errors = has_error_products()

    logger.debug(f"🔥 has_errors = {has_errors}")
    # === ВСТАВИТЬ ПЕРЕД return render_template(...) ===
    # ВСТАВИТЬ ПЕРЕД return render_template(...)
    active_suppliers = []

    # Берём только нужные поля; отсутствующие — заполняем пустыми
    need_cols = ['Sklad', 'Invask', 'Okno', 'United', '%', 'Цена', 'Опт', 'Нал', 'Статус', 'Модель']
    df_for_pick = df.copy()
    for c in need_cols:
        if c not in df_for_pick.columns:
            df_for_pick[c] = ''

    for _, r in df_for_pick[need_cols].fillna('').iterrows():
        row_dict = dict(r)

        # выключенные товары не подсвечиваем
        if str(row_dict.get('Статус', '')).strip().lower() == 'выкл.':
            active_suppliers.append('')
            continue

        chosen_sup, _, _ = choose_best_supplier_for_row(row_dict, supplier_catalog, use_row_sklad=True)

        active_suppliers.append(chosen_sup or '')
    return render_template(
        "index.html",
        tables=tables,
        table_data=df,
        selected_table=table_name,
        sort_column=sort_column,
        sort_order=sort_order,
        zip=zip,
        stats=stats,
        last_download_time=last_download_time,
        global_stock_flags=global_stock_flags,
        saved_form_data=saved_form_data,
        suppliers_list=suppliers_list,
        active_suppliers=active_suppliers,
        has_errors=has_errors,
        automation_status=get_automation_status(),
        market_schedule=get_market_schedule_status(),
        active_page="products",

    )


@app.route('/delete/<table>/<item_id>', methods=['POST'])
def delete_row(table, item_id):
    if table not in ALLOWED_MARKETS:
        abort(404)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Получаем модель и Sklad до удаления
    cursor.execute("SELECT Модель, Sklad FROM marketplace WHERE Sklad = ? AND Маркетплейс = ?", (item_id, table))
    result = cursor.fetchone()
    model, art_mc = result if result else ("", "")

    cursor.execute("DELETE FROM marketplace WHERE Sklad = ? AND Маркетплейс = ?", (item_id, table))
    conn.commit()
    conn.close()

    # Отправляем уведомление
    send_telegram_message(f"🗑 Удалён из {table.upper()}:\n{model} / {art_mc}")
    logger.warning(f"🗑 Удалён товар из {table.upper()}: {model} / {art_mc}")

    return redirect(url_for('show_table', table_name=table, search=''))


@app.route('/update/<table>/<item_id>', methods=['POST'])
def update_row(table, item_id):
    if table not in ALLOWED_MARKETS:
        abort(404)
    data = {
        key: value for key, value in request.form.to_dict().items()
        if key in ALLOWED_COLUMNS
    }
    if "Sklad" in data:
        del data["Sklad"]

    # Получаем старые данные для сравнения
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM marketplace WHERE Sklad = ? AND Маркетплейс = ?", (item_id, table))
    row = cursor.fetchone()
    column_names = [description[0] for description in cursor.description]
    old_data = dict(zip(column_names, row)) if row else {}

    if not old_data:
        conn.close()
        logger.warning(f"⚠️ Товар с Sklad = {item_id} не найден.")
        return '', 400

    if not global_stock_flags.get(table, True):
        logger.info(f"⚙️ Редактирование товара в выключенном маркетплейсе: {table}")
        if 'Нал' in data:
            del data['Нал']

    model = old_data.get("Модель", "—")
    opt_old = int(old_data.get("Опт", 0))
    stock_old = int(old_data.get("Нал", 0))
    price_old = 0

    price_old = int(old_data.get("Цена", 0) or 0)

    try:
        stock_new = int(data.get("Нал", 0))
        opt_new = int(data.get("Опт", 0))
        markup = float(data.get("%", "0").replace('%', '').replace(' ', ''))
        price_new = round((opt_new + opt_new * markup / 100) / 100.0) * 100
    except Exception as e:
        logger.warning(f"❌ Ошибка при парсинге чисел: {e}")
        stock_new, opt_new, price_new = stock_old, opt_old, price_old

    if (stock_old != stock_new) or (opt_old != opt_new) or (price_old != price_new):
        logger.debug(
            f"✅ {table} | {item_id} ({model}) → "
            f"stock: {stock_old} → {stock_new}, "
            f"opt: {opt_old} → {opt_new}, "
            f"price: {price_old} → {price_new}"
        )
    # Принудительное обнуление "Нал", если статус "выкл."
    if data.get('Статус', '').strip() == 'выкл.':
        data['Нал'] = '0'
    elif not global_stock_flags.get(table, True):
        if 'Нал' in data:
            del data['Нал']

    # Удалить Sklad из обновляемых данных

    try:
        opt = float(data.get('Опт', '0').replace(' ', '').replace('р.', ''))
        markup = float(data.get('%', '0').replace(' ', '').replace('%', ''))
        raw_price = opt + (opt * markup / 100)
        price = int(round(raw_price / 100.0) * 100)
        formatted_price = str(price)

        # Получаем список колонок таблицы
        conn_check = sqlite3.connect(DB_PATH)
        cur_check = conn_check.cursor()
        cur_check.execute("PRAGMA table_info(marketplace)")
        table_columns = [col[1] for col in cur_check.fetchall()]
        conn_check.close()

        data['Цена'] = formatted_price  # В новой структуре колонка 'Цена' всегда есть

        if '%' in data:
            data['%'] = str(int(markup))

    except ValueError:
        logger.warning("❌ Невалидные данные в Опт/Наценка для пересчёта цены.")

    # Ключевые поля, которые влияют на "Дата изменения"
    important_fields = [
        "Invask", "Okno", "United", "Модель", "Статус", "Нал", "Опт", "%", "Комментарий",
        "Цена", "WB Артикул", "WB Barcode"
    ]

    # Подготовка к сравнению
    changed = False
    for field in important_fields:
        old_val = str(old_data.get(field, "")).strip()
        new_val = str(data.get(field, "")).strip()
        if field == "Нал" and old_data.get("Статус", "").strip() == "выкл." and old_val == "0" and new_val != old_val:
            # Разрешаем отличия в Нал, если товар выключен и Нал=0 — не считаем изменением
            new_val = "0"
        if old_val != new_val:
            changed = True
            break

    if changed:
        data["Дата изменения"] = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not data:
        return Response("Нет допустимых полей", status=400)
    columns = list(data.keys())
    values = list(data.values())
    update_clause = ", ".join([f'"{col}" = ?' for col in columns])

    logger.debug(f"🧩 SQL запрос: UPDATE '{table}' SET {update_clause} WHERE \"Sklad\" = ?")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE marketplace SET {update_clause} WHERE Sklad = ? AND Маркетплейс = ?",
            values + [item_id, table]
        )
        conn.commit()
        logger.debug(f"🧾 Кол-во обновлённых строк: {cursor.rowcount}")
        logger.success("✅ Успешно обновлено!")

        # 🔍 Проверяем смену статуса
        cursor.execute("""
            SELECT Статус, Модель
              FROM marketplace
             WHERE Sklad = ? AND LOWER(Маркетплейс) = LOWER(?)
        """, (item_id, table))
        row = cursor.fetchone()
        if row:
            new_status, model = row
            old_status = old_data.get("Статус", "").strip().lower()
            new_status = (new_status or "").strip().lower()
            if old_status != new_status:
                action = "🔴 ОТКЛЮЧЕН" if new_status == "выкл." else "🟢 ВКЛЮЧЕН"
                logger.info(f"{action}: {model} ({item_id}) в таблице {table.upper()}")

    except Exception:
        logger.exception("❌ Ошибка при обновлении:")
        return Response("Ошибка обновления", status=500)
    finally:
        conn.close()

    return '', 204


@app.route('/bulk_markup/<market>', methods=['POST'])
def bulk_markup(market):
    """
    Массовая корректировка наценки для выбранного маркетплейса.
    delta = +1 или -1 (в теле запроса x-www-form-urlencoded: delta=1|-1)
    Пересчитывает 'Цена' по формуле округления к сотне.
    """
    try:
        delta = int(request.form.get('delta', '0'))
    except Exception:
        return Response("Bad delta", status=400)
    if delta not in (-1, 1):
        return Response("Bad delta", status=400)

    if market not in ('yandex', 'ozon', 'wildberries'):
        return Response("Bad market", status=400)

    # Если маркетплейс глобально выключен — меняем только наценку и цену, остатки не трогаем
    from datetime import datetime
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Наценка хранится без знака %, Опт и Цена — числа/строки-числа.
    # Обновляем наценку и сразу цену с округлением к сотне.
    try:
        cur.execute("""
            UPDATE marketplace
               SET "%" = COALESCE(CAST("%" AS INTEGER), 0) + ?,
                   Цена = CAST(
                              ROUND(
                                  (CAST(Опт AS FLOAT) + CAST(Опт AS FLOAT) * (COALESCE(CAST("%" AS INTEGER),0) + ?)/100.0)
                                  / 100.0, 0
                              ) * 100 AS INTEGER
                          ),
                   "Дата изменения" = ?
             WHERE Маркетплейс = ?
        """, (delta, delta, now_str, market))
        conn.commit()
        updated = cur.rowcount
    except Exception as e:
        conn.rollback()
        logger.exception("❌ Ошибка массового изменения наценки")
        return Response("Server error", status=500)
    finally:
        conn.close()

    logger.success(f"📈 Массовое изменение наценки {market}: delta={delta}, затронуто строк: {updated}")
    return '', 204


@app.route('/download_log')
def download_log():
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_file_path = LOG_DIR / "app.log"

    if not log_file_path.exists():
        logger.warning("📁 Файл лога не найден для загрузки.")
        return "Файл лога не найден", 404

    return send_file(log_file_path, as_attachment=True)


@app.route('/add/<table_name>', methods=['POST'])
def add_item(table_name):
    if table_name not in ALLOWED_MARKETS:
        abort(404)
    from datetime import datetime
    data = {
        key: value for key, value in request.form.to_dict().items()
        if key in ALLOWED_COLUMNS
    }
    # 🧹 Убираем пробелы по бокам во всех текстовых полях
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}

    if 'Комментарий' in data and data['Комментарий'] is None:
        data['Комментарий'] = data.get('Комментарий') or ''

    if not global_stock_flags.get(table_name, True):
        # Если маркетплейс OFF — принудительно Нал = 0
        data['Нал'] = '0'
        logger.info(f"➖ Добавление товара в выключенный маркетплейс {table_name}: остаток принудительно 0")

    art_mc = data.get('Sklad', '').strip()
    invask = (data.get('Invask', '') or '').strip()
    okno = (data.get('Okno', '') or '').strip()
    united = (data.get('United', '') or '').strip()

    # Проверяем обязательные поля
    if not art_mc or not data.get('Модель') or not data.get('Нал') or not data.get('Опт') or not data.get('%'):
        logger.warning("❌ Не заполнены обязательные поля (Sklad, Модель, Нал, Опт, %).")
        return redirect(url_for('show_table', table_name=table_name))

    # Дополнительно проверка для Wildberries
    if table_name == "wildberries":
        if not data.get('WB Barcode') or not data.get('WB Артикул'):
            logger.warning("❌ Для Wildberries обязательны WB Barcode и WB Артикул.")
            return redirect(url_for('show_table', table_name=table_name))

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    model = data.get('Модель', '').strip()
    wb_barcode = data.get('WB Barcode', '').strip()
    wb_artikul = data.get('WB Артикул', '').strip()

    # Проверка дубликатов: учитываем только непустые поля
    conditions = []
    params = [table_name]

    if art_mc:
        conditions.append("Sklad = ?")
        params.append(art_mc)
    if model:
        conditions.append("Модель = ?")
        params.append(model)
    if wb_barcode:
        conditions.append("\"WB Barcode\" = ?")
        params.append(wb_barcode)
    if wb_artikul:
        conditions.append("\"WB Артикул\" = ?")
        params.append(wb_artikul)

    existing_count = 0
    if conditions:
        query = f"""
            SELECT COUNT(*) FROM marketplace
            WHERE Маркетплейс = ?
              AND ({' OR '.join(conditions)})
        """
        cursor.execute(query, params)
        existing_count = cursor.fetchone()[0]

    if existing_count > 0:
        conn.close()
        logger.warning("⚠️ Товар с таким Sklad, Модель, WB Barcode или WB Артикул уже существует.")
        session['saved_form'] = data
        return redirect(url_for('show_table', table_name=table_name, duplicate='1'))

    try:
        opt = float(data.get('Опт', '').replace(' ', '').replace('р.', ''))
        markup = float(data.get('%', '').replace('%', '').replace(' ', ''))
        stock = int(data.get('Нал', '').replace(' ', ''))
        if data.get('Статус', '').strip() == 'выкл.':
            stock = 0

        data['Нал'] = str(stock)
        price_ym = int(round((opt + (opt * markup / 100)) / 100.0) * 100)

        data['Опт'] = str(opt)
        data['%'] = str(int(markup))
        data['Нал'] = str(stock)

        data['Цена'] = str(price_ym)

        # Устанавливаем дату изменения только перед вставкой
        data["Дата изменения"] = datetime.now().strftime("%d.%m.%Y %H:%M")

        data["Маркетплейс"] = table_name  # добавляем до формирования списков

        columns = list(data.keys())
        values = [data[col] for col in columns]
        placeholders = ", ".join(["?"] * len(columns))
        escaped_columns = [f'"{col}"' for col in columns]
        insert_query = f"INSERT INTO marketplace ({', '.join(escaped_columns)}) VALUES ({placeholders})"

        cursor.execute(insert_query, values)
        conn.commit()
        send_telegram_message(f"✅ В {table_name.upper()} добавлен:\n{data.get('Модель', '')} / {art_mc}")
        logger.success(f"✅ Добавлен товар в {table_name.upper()}: {data.get('Модель', '')} / {art_mc}, поставщик: {data.get('Поставщик', '')}")


    except Exception as e:
        logger.exception("❌ Ошибка при добавлении")
        return Response("Ошибка добавления", status=500)
    finally:
        conn.close()

    return redirect(url_for('show_table', table_name=table_name, added='1'))



@app.route('/statistic')
@requires_auth
def show_statistic():
    logger.info("📈 Открыта страница статистики")
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT Sklad, Invask, Okno, United, Модель, Статус, Маркетплейс, Опт, Нал FROM marketplace",
        conn
    )
    conn.close()

    data = {}
    supplier_stats = {
        "Sklad": {"Yandex": 0, "Ozon": 0, "Wildberries": 0, "Всего": 0, "Активно": 0, "Неактивно": 0},
        "Invask": {"Yandex": 0, "Ozon": 0, "Wildberries": 0, "Всего": 0, "Активно": 0, "Неактивно": 0},
        "Okno": {"Yandex": 0, "Ozon": 0, "Wildberries": 0, "Всего": 0, "Активно": 0, "Неактивно": 0},
        "United": {"Yandex": 0, "Ozon": 0, "Wildberries": 0, "Всего": 0, "Активно": 0, "Неактивно": 0},
    }

    for _, row in df.iterrows():
        art_mc = row['Sklad']
        status = (row.get('Статус') or '').strip().lower()
        mp = row.get('Маркетплейс', '').capitalize()

        if art_mc not in data:
            data[art_mc] = {
                'Sklad': art_mc,
                'Модель': row.get('Модель', '')
            }

        # отмечаем наличие товара на маркетплейсе
        data[art_mc][mp] = True
        if status == 'выкл.':
            data[art_mc][f'Статус_{mp}'] = 'выкл.'

        # обновляем статистику по каждому поставщику
        for sup in ["Sklad", "Invask", "Okno", "United"]:
            if sup == "Sklad":
                cond = (not row['Invask'] and not row['Okno'] and not row['United'] and row['Sklad'])
            else:
                cond = bool(row[sup])

            if cond:
                supplier_stats[sup][mp] += 1
                supplier_stats[sup]['Всего'] += 1
                if status == 'выкл.':
                    supplier_stats[sup]['Неактивно'] += 1
                else:
                    supplier_stats[sup]['Активно'] += 1

    errors = detect_errors_across_marketplaces()

    return render_template(
        "statistic.html",
        stats_data=list(data.values()),
        supplier_stats=supplier_stats,
        errors=errors,
        active_page="statistics",
        automation_status=get_automation_status(),
    )





def has_error_products():
    errors = detect_errors_across_marketplaces()
    return len(errors) > 0

def detect_errors_across_marketplaces():
    import sqlite3
    import pandas as pd

    db_path = "System/marketplace_base.db"
    conn = sqlite3.connect(db_path, timeout=10)
    df = pd.read_sql_query("SELECT * FROM marketplace", conn)
    conn.close()

    df["Маркетплейс"] = df["Маркетплейс"].str.capitalize()

    errors = []
    # 👇 теперь проверяем и Sklad как поле
    fields_to_check = ["Sklad", "Invask", "Okno", "United", "Модель"]

    for art_mc, group in df.groupby("Sklad"):
        if len(group) <= 1:
            continue

        values_by_field = {field: set(group[field].astype(str).fillna("")) for field in fields_to_check}

        has_diff = any(len(values) > 1 for values in values_by_field.values())
        if not has_diff:
            continue

        for _, row in group.iterrows():
            row_dict = row.to_dict()
            diff = {field: len(values_by_field[field]) > 1 for field in fields_to_check}
            diff["Нал"] = False
            diff["Опт"] = False

            errors.append({
                "Маркетплейс": row_dict.get("Маркетплейс", ""),
                "Sklad": row_dict.get("Sklad", ""),
                "Invask": row_dict.get("Invask", ""),
                "Okno": row_dict.get("Okno", ""),
                "United": row_dict.get("United", ""),
                "Модель": row_dict.get("Модель", ""),
                "Статус": row_dict.get("Статус", ""),
                "Нал": row_dict.get("Нал", ""),
                "Опт": row_dict.get("Опт", ""),
                "diff": diff
            })

    return errors


@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, HTTPException):
        return e
    logger.exception(f"💥 Ошибка: {str(e)}")
    return "Произошла ошибка на сервере", 500

@app.route('/download_unlisted')
@requires_auth
def download_unlisted():
    try:
        df = generate_unlisted()
        if df.empty:
            return Response("Нет новых товаров", status=404)

        # Сохраняем в Excel в памяти
        output = BytesIO()
        df.to_excel(output, index=False)
        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name="new_products.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        logger.exception("❌ Ошибка при формировании списка новых товаров")
        return Response("Ошибка при формировании файла", status=500)

def recompute_marketplace_core(market: str) -> int:
    """Пересчёт остатков и цен одного маркетплейса (общая реализация)."""
    return _recompute_marketplace_core_impl(market, load_stock_flags())


@app.route('/recompute/<market>', methods=['POST'])
@requires_auth
def recompute_marketplace(market):
    updated = recompute_marketplace_core(market)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return '', 204
    return redirect(url_for('show_table', table_name=market))


def _format_automation_time(value) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def _get_cycle_freshness(state: dict) -> dict:
    """Возвращает свежесть данных по последнему завершённому полному циклу."""
    if state.get("running"):
        return {
            "freshness_status": "running",
            "freshness_label": "Обновляется",
            "freshness_title": "Сейчас выполняется полный цикл синхронизации",
        }

    result = state.get("last_result")
    result_states = {
        "partial": ("warning", "Есть ошибки", "Последний полный цикл завершён с ошибками"),
        "error": ("error", "Ошибка цикла", "Последний полный цикл не выполнен"),
        "interrupted": ("error", "Цикл прерван", "Последний полный цикл был прерван"),
    }
    if result in result_states:
        status, label, title = result_states[result]
        return {
            "freshness_status": status,
            "freshness_label": label,
            "freshness_title": title,
        }

    finished_value = state.get("last_finished")
    if result != "success" or not finished_value:
        return {
            "freshness_status": "unknown",
            "freshness_label": "Нет данных",
            "freshness_title": "Успешный полный цикл ещё не завершался",
        }

    try:
        finished_at = datetime.fromisoformat(str(finished_value))
        now = (
            datetime.now(finished_at.tzinfo)
            if finished_at.tzinfo is not None
            else datetime.now()
        )
        age_seconds = max(0, (now - finished_at).total_seconds())
    except (TypeError, ValueError):
        return {
            "freshness_status": "unknown",
            "freshness_label": "Нет данных",
            "freshness_title": "Не удалось определить время последнего полного цикла",
        }

    age_minutes = int(age_seconds // 60)
    interval_seconds = int(state.get("interval_minutes", 5)) * 60
    fresh_threshold = max(10 * 60, interval_seconds * 1.25)
    warning_threshold = max(30 * 60, interval_seconds * 2)
    if age_seconds < fresh_threshold:
        status, label = "fresh", "Актуальны"
    elif age_seconds < warning_threshold:
        status, label = "warning", "Устаревают"
    else:
        status, label = "error", "Устарели"

    return {
        "freshness_status": status,
        "freshness_label": label,
        "freshness_title": (
            f"Последний успешный полный цикл завершён {age_minutes} мин. назад"
        ),
    }


def get_automation_status() -> dict:
    state = load_automation_state()
    next_run = None

    if scheduler_instance is not None and scheduler_instance.running:
        job = scheduler_instance.get_job("automation-cycle")
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.isoformat()

    if not state["enabled"]:
        label = "Выключено"
    elif state["running"]:
        label = "Выполняется"
    else:
        label = {
            "success": "Готово",
            "partial": "Есть ошибки",
            "error": "Ошибка",
            "interrupted": "Прервано",
            "skipped_locked": "Уже запущено",
            "never": "Ожидание",
        }.get(state.get("last_result"), "Ожидание")

    state.update(
        {
            "label": label,
            "next_run": next_run,
            "next_run_display": (
                _format_automation_time(next_run)
                if state["enabled"]
                else "автозапуск выключен"
            ),
            "last_started_display": _format_automation_time(
                state.get("last_started")
            ),
            "last_finished_display": _format_automation_time(
                state.get("last_finished")
            ),
        }
    )
    state.update(_get_cycle_freshness(state))
    return state


def configure_automation_cycle_job(scheduler: BackgroundScheduler) -> None:
    """Применяет сохранённый интервал и состояние автоматического цикла."""
    state = load_automation_state()
    interval_minutes = state["interval_minutes"]
    job = scheduler.get_job("automation-cycle")

    if job is None:
        scheduler.add_job(
            run_automation_cycle,
            "interval",
            minutes=interval_minutes,
            args=["scheduler"],
            id="automation-cycle",
            replace_existing=True,
        )
    else:
        scheduler.reschedule_job(
            "automation-cycle",
            trigger="interval",
            minutes=interval_minutes,
        )

    if state["enabled"]:
        scheduler.resume_job("automation-cycle")
    else:
        scheduler.pause_job("automation-cycle")


def run_automation_cycle(trigger: str = "scheduler") -> dict:
    """Единый цикл: делегирует в services.cycle_runner (общая реализация)."""
    from services.cycle_runner import run_cycle

    if trigger == "scheduler" and not load_automation_state()["enabled"]:
        logger.info("⏸ Автоматический цикл выключен — плановый запуск пропущен")
        return {"status": "disabled", "message": "Автоматический запуск выключен"}

    return run_cycle(trigger, check_schedule=False)


@app.route("/automation/status")
@requires_auth
def automation_status():
    return jsonify(get_automation_status())


@app.route("/orders")
@requires_auth
def control_orders_page():
    return render_template(
        "orders.html",
        active_page="orders",
        automation_status=get_automation_status(),
    )


@app.route("/processes")
@requires_auth
def control_processes_page():
    return render_template(
        "processes.html",
        active_page="processes",
        automation_status=get_automation_status(),
    )


@app.route("/logs")
@requires_auth
def control_logs_page():
    return render_template(
        "logs.html",
        active_page="logs",
        automation_status=get_automation_status(),
    )


_LOG_SECRET_PATTERNS = (
    re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"(?i)(token|secret|password|api[_-]?key)([\"'=:\s]+)([A-Za-z0-9_.:-]{10,})"
    ),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._-]+"),
)


def _mask_log_line(line: str) -> str:
    masked = _LOG_SECRET_PATTERNS[0].sub("bot***:***", line)
    masked = _LOG_SECRET_PATTERNS[1].sub(r"\1\2***", masked)
    return _LOG_SECRET_PATTERNS[2].sub(r"\1***", masked)


def read_control_log_tail(lines: int = 150) -> list[str]:
    safe_lines = max(20, min(int(lines), 1000))
    path = LOG_DIR / "app.log"
    if not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 1024 * 1024))
        content = stream.read().decode("utf-8", errors="replace")
    return [_mask_log_line(line) for line in content.splitlines()[-safe_lines:]]


def execute_control_process(process_code: str, trigger: str = "manual") -> None:
    if not automation_cycle_lock.acquire(blocking=False):
        logger.warning(f"⏭ {process_code}: запуск пропущен, полный цикл уже выполняется")
        return

    runners = {
        "refresh_sklad": update_sklad_task,
        "stocks": lambda: __import__(
            "services.marketplace_cycle", fromlist=["run_stock_updates"]
        ).run_stock_updates(),
        "orders": lambda: __import__(
            "order_notifications", fromlist=["check_for_new_orders"]
        ).check_for_new_orders(),
        "prices": lambda: __import__(
            "price_updater_master", fromlist=["update_all_prices"]
        ).update_all_prices(),
        "ozon_actions": run_ozon_actions_if_enabled,
        "database_backup": backup_database,
    }
    runner = runners.get(process_code)
    if runner is None:
        automation_cycle_lock.release()
        logger.error(f"Неизвестный процесс панели управления: {process_code}")
        return

    run_id = start_process_run(process_code, trigger)
    started_at = datetime.now()
    try:
        result = runner()
        successful = result is not False
        message = "Выполнено" if successful else "Операция завершилась с ошибками"
        finish_process_run(
            run_id,
            process_code,
            status="success" if successful else "error",
            message=message,
            duration_seconds=round((datetime.now() - started_at).total_seconds(), 1),
            details={"result": result} if isinstance(result, (str, int, float, bool)) else None,
        )
    except Exception as error:
        logger.exception(f"❌ Ручной процесс {process_code} завершился ошибкой")
        finish_process_run(
            run_id,
            process_code,
            status="error",
            message=f"{type(error).__name__}: {error}",
            duration_seconds=round((datetime.now() - started_at).total_seconds(), 1),
        )
    finally:
        automation_cycle_lock.release()


@app.route("/api/control/state")
@requires_auth
def control_state_api():
    init_control_store()
    return jsonify({
        "automation": get_automation_status(),
        "processes": list_processes(),
        "runs": list_process_runs(80),
        "market_states": {
            market: bool(global_stock_flags.get(market, True))
            for market in ("yandex", "ozon", "wildberries")
        },
        "market_schedule": get_market_schedule_status(),
    })


@app.route("/api/control/processes/<process_code>/run", methods=["POST"])
@requires_auth
def control_run_process_api(process_code):
    allowed = {
        "marketplace_cycle", "refresh_sklad", "stocks", "orders",
        "prices", "ozon_actions", "database_backup",
    }
    if process_code not in allowed:
        return jsonify({"status": "error", "message": "Неизвестный процесс"}), 404
    if load_automation_state().get("running") or automation_cycle_lock.locked():
        return jsonify({"status": "busy", "message": "Другой процесс уже выполняется"}), 409

    scheduler = ensure_scheduler_started()
    if process_code == "marketplace_cycle":
        callback = run_automation_cycle
        args = ["manual"]
    else:
        callback = execute_control_process
        args = [process_code, "manual"]
    scheduler.add_job(
        callback,
        "date",
        run_date=datetime.now(),
        args=args,
        id=f"control-{process_code}-{secrets.token_hex(4)}",
        misfire_grace_time=60,
    )
    record_operator_action(
        USERNAME,
        "process.manual_run",
        f"Вручную запущен процесс {process_code}",
        target_type="process",
        target_code=process_code,
    )
    return jsonify({"status": "queued", "message": "Процесс поставлен в очередь"}), 202


@app.route("/api/control/orders")
@requires_auth
def control_orders_api():
    init_control_store()
    return jsonify({"orders": list_orders(
        query=request.args.get("query", ""),
        marketplace=request.args.get("marketplace", ""),
        status=request.args.get("status", ""),
        limit=request.args.get("limit", 100),
    )})


@app.route("/api/control/orders/<marketplace>/<path:order_id>")
@requires_auth
def control_order_detail_api(marketplace, order_id):
    order = get_order(marketplace, order_id)
    if order is None:
        return jsonify({"status": "error", "message": "Заказ не найден"}), 404
    return jsonify({"order": order})


@app.route("/api/control/orders/<marketplace>/<path:order_id>", methods=["DELETE"])
@requires_auth
def control_delete_order_api(marketplace, order_id):
    order = get_order(marketplace, order_id)
    if order is None:
        return jsonify({"status": "error", "message": "Заказ не найден"}), 404

    # Локальный импорт исключает циклическую загрузку: order_notifications
    # использует функции выбора поставщика из web_app.
    from order_notifications import file_path, unmark_order_processed

    marker_removed = unmark_order_processed(order_id, file_path)
    if not delete_order(marketplace, order_id):
        return jsonify({"status": "error", "message": "Заказ не найден"}), 404

    record_operator_action(
        USERNAME,
        "order.delete",
        f"Полностью сброшена история заказа {marketplace} {order_id}",
        target_type="order",
        target_code=f"{marketplace}:{order_id}",
        details={
            "external_data_changed": False,
            "processed_marker_changed": marker_removed,
            "reprocessing_allowed": True,
        },
    )
    logger.warning(
        "♻️ Заказ {} {} удалён из истории; защита от повторной обработки снята: {}.",
        marketplace,
        order_id,
        "да" if marker_removed else "отметки не было",
    )
    return jsonify({
        "status": "ok",
        "message": "Заказ удалён; повторная обработка разрешена",
        "processed_marker_removed": marker_removed,
    })


@app.route("/api/control/logs")
@requires_auth
def control_logs_api():
    try:
        lines = int(request.args.get("lines", 150))
    except ValueError:
        return jsonify({"status": "error", "message": "Некорректное число строк"}), 400
    return jsonify({
        "lines": read_control_log_tail(lines),
        "actions": list_operator_actions(150),
    })


@app.route("/market-schedule", methods=["GET", "POST"])
@requires_auth
def market_schedule_settings():
    def response_payload() -> dict:
        return {
            **get_market_schedule_status(),
            "market_states": {
                market: bool(global_stock_flags.get(market, True))
                for market in ("yandex", "ozon", "wildberries")
            },
        }

    if request.method == "GET":
        return jsonify(response_payload())

    try:
        schedule = save_market_schedule(request.get_json(silent=True) or {})
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 400

    if scheduler_instance is not None and scheduler_instance.running:
        configure_market_schedule_jobs(scheduler_instance)

    logger.info(
        "📆 Недельное расписание Wildberries сохранено: "
        f"{'включено' if schedule['enabled'] else 'выключено'}, "
        f"OFF {schedule['off_day']} {schedule['off_time']}, "
        f"ON {schedule['on_day']} {schedule['on_time']}"
    )
    return jsonify({"status": "ok", **response_payload()})


@app.route("/automation/toggle", methods=["POST"])
@requires_auth
def toggle_automation():
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(
            {"status": "error", "message": "enabled must be boolean"}
        ), 400

    set_automation_enabled(enabled)
    if scheduler_instance is not None and scheduler_instance.running:
        configure_automation_cycle_job(scheduler_instance)
    logger.info(
        f"{'▶' if enabled else '⏸'} Автоматический цикл "
        f"{'включён' if enabled else 'выключен'} пользователем"
    )
    record_operator_action(
        USERNAME,
        "automation.toggle",
        f"Автосинхронизация {'включена' if enabled else 'выключена'}",
        target_type="automation",
        target_code="marketplace_cycle",
        details={"enabled": enabled},
    )
    return jsonify(get_automation_status())


@app.route("/automation/interval", methods=["POST"])
@requires_auth
def set_automation_interval_minutes():
    payload = request.get_json(silent=True) or {}
    try:
        state = set_automation_interval(payload.get("interval_minutes"))
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 400

    if scheduler_instance is not None and scheduler_instance.running:
        configure_automation_cycle_job(scheduler_instance)

    logger.info(
        "⏱ Интервал автоматического цикла изменён: "
        f"{state['interval_minutes']} мин."
    )
    record_operator_action(
        USERNAME,
        "automation.interval",
        f"Интервал автосинхронизации: {state['interval_minutes']} мин.",
        target_type="automation",
        target_code="marketplace_cycle",
        details={"interval_minutes": state["interval_minutes"]},
    )
    return jsonify(get_automation_status())


@app.route("/automation/run", methods=["POST"])
@requires_auth
def run_automation_now():
    if scheduler_instance is None or not scheduler_instance.running:
        return jsonify(
            {"status": "error", "message": "Планировщик не запущен"}
        ), 503

    if load_automation_state()["running"] or automation_cycle_lock.locked():
        return jsonify(
            {"status": "busy", "message": "Цикл уже выполняется"}
        ), 409

    scheduler_instance.add_job(
        run_automation_cycle,
        "date",
        run_date=datetime.now(),
        args=["manual"],
        id=f"automation-manual-{secrets.token_hex(4)}",
        misfire_grace_time=60,
    )
    return jsonify(
        {"status": "queued", "message": "Ручной запуск поставлен в очередь"}
    ), 202


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        job_defaults={
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": 120,
        },
    )
    configure_automation_cycle_job(scheduler)
    scheduler.add_job(
        run_ozon_actions_if_enabled,
        "interval",
        minutes=10,
        id="ozon-actions",
        replace_existing=True,
    )
    scheduler.add_job(backup_database, "cron", hour=2, id="database-backup", replace_existing=True)
    for supplier in CRON_SUPPLIERS:
        scheduler.add_job(
            set_supplier_state_if_needed,
            "cron",
            day_of_week="fri",
            hour=1,
            minute=0,
            args=[supplier, False],
            id=f"supplier-off-{supplier}",
            replace_existing=True,
        )
        scheduler.add_job(
            set_supplier_state_if_needed,
            "cron",
            day_of_week="sun",
            hour=15,
            minute=0,
            args=[supplier, True],
            id=f"supplier-on-{supplier}",
            replace_existing=True,
        )
    configure_market_schedule_jobs(scheduler)
    return scheduler


def shutdown_scheduler() -> None:
    if scheduler_instance is not None and scheduler_instance.running:
        scheduler_instance.shutdown(wait=False)


def ensure_scheduler_started() -> BackgroundScheduler:
    global scheduler_instance, scheduler_shutdown_registered

    if scheduler_instance is not None and scheduler_instance.running:
        return scheduler_instance

    # Единоразовая инициализация планировщика внутри текущего процесса.
    with scheduler_start_lock:
        if scheduler_instance is not None and scheduler_instance.running:
            return scheduler_instance

        ensure_indexes(DB_PATH, _SUP_DB_PATH)
        init_control_store()
        reset_interrupted_run()
        scheduler_instance = create_scheduler()
        scheduler_instance.start()

        if not scheduler_shutdown_registered:
            atexit.register(shutdown_scheduler)
            scheduler_shutdown_registered = True

        interval_minutes = load_automation_state()["interval_minutes"]
        logger.info(
            "📅 Запущен единый цикл фоновой синхронизации "
            "(склад, остатки, заказы и цены — "
            f"каждые {interval_minutes} мин.)"
        )

    return scheduler_instance


def run_server() -> None:
    # Запуск фоновых задач до старта HTTP-сервера при прямом запуске web_app.py.
    ensure_scheduler_started()

    logger.info("🚀 Приложение доступно: http://127.0.0.1:5050")

    if os.getenv("YMWB_DEV_SERVER", "").lower() in {"1", "true", "yes"}:
        app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
        return

    try:
        from waitress import serve
    except ModuleNotFoundError:
        logger.warning(
            "Waitress не установлен в текущем Python. "
            "Запускается встроенный сервер Flask; установите зависимости "
            "командой: python -m pip install -r requirements.txt"
        )
        app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
        return

    serve(app, host="127.0.0.1", port=5050, threads=4)


if __name__ == '__main__':
    run_server()

