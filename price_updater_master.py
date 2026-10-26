"""
Модуль price_updater_master предназначен для автоматического обновления цен
на маркетплейсах Yandex.Market, Ozon и Wildberries через их официальные API.

Функции модуля:

- update_yandex:
    Загружает цены из базы данных и отправляет обновления на Yandex.Market.
    Использует API с расчётом discountBase.

- update_ozon:
    Загружает цены из базы данных и отправляет обновления на Ozon.
    Формирует old_price с наценкой 15%.

- update_wildberries:
    Загружает артикулы и цены и отправляет цены с наценкой 25% и скидкой 16% на Wildberries.
    Обрабатывает специфические коды ответа, включая 208 и дублирующие ошибки.

- update_all_prices:
    Вызывает все три функции обновления по очереди.

Также поддерживается логирование всех операций через loguru
и уведомления об ошибках в Telegram через библиотеку notifiers.
"""

import os
import sqlite3
import math
import json
from clients import http_client as requests
from logger_config import logger
from dotenv import load_dotenv
from notifiers import get_notifier

# Загрузка переменных окружения
load_dotenv(dotenv_path=os.path.join("System", ".env"))

# Telegram уведомления
telegram = get_notifier('telegram')
telegram_got_token_error = os.getenv('telegram_got_token_error')
telegram_chat_id_error = os.getenv('telegram_chat_id_error')

def notify_error(marketplace, error):
    message = f"😨 Ошибка при обновлении цен на {marketplace}:\n{error}"
    telegram.notify(token=telegram_got_token_error, chat_id=telegram_chat_id_error, message=message)

# ----------- YANDEX -----------
def update_yandex():
    logger.info("🚀 Начато обновление цен на Yandex Market")
    try:
        conn = sqlite3.connect('System/marketplace_base.db', timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT `Sklad`, `Цена` FROM marketplace
            WHERE `Маркетплейс` = 'yandex' AND `Sklad` IS NOT NULL AND `Цена` IS NOT NULL
        """)
        rows = cursor.fetchall()
        logger.debug(f"📥 Загружено {len(rows)} строк из базы для Yandex")
        conn.close()

        offers = []
        for offer_id, price in rows:
            price = int(price)
            discount_base = int(math.ceil(price * 1.18 / 100.0)) * 100
            offers.append({
                "offerId": str(offer_id).strip(),
                "price": {
                    "value": price,
                    "currencyId": "RUR",
                    "discountBase": discount_base
                }
            })
        ym_token = os.getenv('ym_token')
        businessId = os.getenv('businessId')
        url = f'https://api.partner.market.yandex.ru/businesses/{businessId}/offer-prices/updates'
        headers = {
            "Authorization": f"Bearer {ym_token}",
            "Content-Type": "application/json"
        }
        logger.info(f"⏳ Отправка {len(offers)} цен в Yandex Market...")
        response = requests.post(url, headers=headers, json={"offers": offers}, timeout=10)
        logger.info(f"📡 Yandex API: {response.status_code}")
        if response.status_code != 200:
            logger.warning(f"⚠ Ответ от Yandex API: {response.text}")
            raise Exception(f"YM: Статус-код {response.status_code}, Ответ: {response.text}")
        logger.success("✅ Цены Yandex успешно обновлены")

    except Exception as e:
        notify_error("Yandex Market", e)
        raise

# ----------- OZON -----------
def update_ozon():
    logger.info("🚀 Начато обновление цен на Ozon")
    try:
        conn = sqlite3.connect('System/marketplace_base.db')
        cursor = conn.cursor()
        cursor.execute("""
            SELECT `Sklad`, `Цена` FROM marketplace
            WHERE `Маркетплейс` = 'ozon' AND `Sklad` IS NOT NULL AND `Цена` IS NOT NULL
        """)
        rows = cursor.fetchall()
        logger.debug(f"📥 Загружено {len(rows)} строк из базы для Ozon")
        conn.close()

        prices = []
        for offer_id, price in rows:
            price = float(price)
            old_price = int(round(price * 1.15))
            prices.append({
                "offer_id": str(offer_id).strip(),
                "price": str(int(price)),
                "old_price": str(old_price),
                "isDeleted": False
            })

        ozon_client_id = os.getenv('ozon_client_ID')
        ozon_api_key = os.getenv('ozon_API_key')
        url = 'https://api-seller.ozon.ru/v1/product/import/prices'
        headers = {
            'Client-Id': ozon_client_id,
            'Api-Key': ozon_api_key,
            'Content-Type': 'application/json'
        }
        logger.info(f"⏳ Отправка {len(prices)} цен в Ozon...")
        response = requests.post(url, headers=headers, data=json.dumps({"prices": prices}), timeout=10)
        logger.info(f"📡 Ozon API: {response.status_code}")
        if response.status_code != 200:
            logger.warning(f"⚠ Ответ от Ozon API: {response.text}")

            # Исключение для ошибки 429 в 50 или 55 минут
            if response.status_code == 429 and (
                    "limit exceeded" in response.text or "ResourceExhausted" in response.text):
                from datetime import datetime
                now_minute = datetime.now().minute
                if now_minute in (50, 55):
                    logger.info("⏳ Ошибка 429 пропущена для Ozon в разрешённое время")
                    return
            raise Exception(f"Ozon: Статус-код {response.status_code}, Ответ: {response.text}")
        logger.success("✅ Цены Ozon успешно обновлены")

    except Exception as e:
        notify_error("Ozon", e)
        raise

# ----------- WILDBERRIES -----------
def update_wildberries():
    logger.info("🚀 Начато обновление цен на Wildberries")
    try:
        conn = sqlite3.connect('System/marketplace_base.db')
        cursor = conn.cursor()
        cursor.execute("""
            SELECT `WB Артикул`, `Цена` FROM marketplace
            WHERE `Маркетплейс` = 'wildberries' AND `WB Артикул` IS NOT NULL AND `Цена` IS NOT NULL
        """)
        rows = cursor.fetchall()
        logger.debug(f"📥 Загружено {len(rows)} строк из базы для Wildberries")
        conn.close()

        data = []
        for wb_id, price in rows:
            base_price = int(price)
            final_price = int(round((base_price * 1.20) / 100.0)) * 100  # +25%, округляем
            data.append({
                "nmID": int(wb_id),
                "price": final_price,
                "discount": 16
            })

        wb_token = os.getenv('wb_token')
        url = 'https://discounts-prices-api.wildberries.ru/api/v2/upload/task'
        headers = {
            'Authorization': wb_token,
            'Content-Type': 'application/json'
        }

        response = requests.post(url, headers=headers, json={"data": data}, timeout=10)
        logger.info(f"⏳ Отправка {len(data)} цен в Wildberries...")
        # if response.status_code != 200:
        #     logger.warning(f"⚠ Ответ от Wildberries API: {response.text}")
        #     error_text = response.json().get('errorText', '')
        #     if response.status_code == 208 or \
        #        (response.status_code == 400 and ("No goods" in error_text or "already set" in error_text.lower())):
        #         logger.success("✅ Цены Wildberries успешно обновлены")
        #         return
        #     raise Exception(f"WB: Статус-код {response.status_code}, Ответ: {response.text}")
        if response.status_code != 200:
            try:
                error_text = response.json().get('errorText', '').strip().lower()
            except Exception:
                error_text = ''

            # Повторная установка уже действующих значений — штатный результат без изменений.
            if "specified prices and discounts are already set" in error_text:
                logger.success("✅ WB: цены и скидки уже установлены — изменений нет")
                return

            # Повторная задача и отсутствие товаров также не требуют аварийного повтора.
            if response.status_code in (208, 400) and any(x in error_text for x in [
                "no goods",
                "already set",
                "already exists",
            ]):
                logger.info("ℹ️ WB: обновление цен не требуется — данные уже актуальны")
                return

            logger.warning(f"⚠ Ответ от Wildberries API: {response.text}")
            raise Exception(f"WB: Статус-код {response.status_code}, Ответ: {response.text}")
        logger.success("✅ Цены Wildberries успешно обновлены")


    except Exception as e:
        notify_error("Wildberries", e)
        raise

# ----------- ГЛАВНЫЙ ВЫЗОВ -----------
def update_all_prices():
    from services.marketplace_cycle import load_stock_flags

    flags = load_stock_flags()
    failures = []
    updated = 0
    for market_key, marketplace, updater in (
        ("yandex", "Yandex", update_yandex),
        ("ozon", "Ozon", update_ozon),
        ("wildberries", "Wildberries", update_wildberries),
    ):
        if not bool(flags.get(market_key, True)):
            logger.info(
                f"⏭ {marketplace} выключен → обновление цен пропущено"
            )
            continue
        try:
            updater()
            updated += 1
        except Exception as exc:
            failures.append(f"{marketplace}: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))
    logger.info(f"🎯 Обновление цен завершено: площадок обновлено {updated}")
