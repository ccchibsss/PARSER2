# 📦 Парсер маркетплейсов — Streamlit + ежедневный автозапуск в 07:00

Проект переведён на **Streamlit** (вместо Flask-интерфейса) и получил
**ежедневный автоматический парсинг в 07:00** по московскому времени.

Полный цикл, как и раньше: **склад → остатки → заказы → цены**
(Wildberries, Ozon, Яндекс.Маркет).

---

## 1. Быстрый старт

```bash
git clone https://github.com/ccchibsss/PARSER.git
cd PARSER

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp System/.env.example System/.env   # и заполните ключи API
streamlit run streamlit_app.py
```

Панель: **http://localhost:8501**

Проверить, что всё работает без ключей API:

```bash
python tools/create_demo_db.py       # демо-базы, реальные данные не затрагиваются
streamlit run streamlit_app.py
```

---

## 2. Автозапуск каждый день в 07:00

Способов три — можно совмещать.

### Вариант A. Планировщик внутри Streamlit (по умолчанию, ничего настраивать не нужно)

При старте приложения поднимается `APScheduler` с одной cron-задачей
`daily-parse`. Пока панель запущена, цикл выполняется **каждый день в 07:00**.

Настройки: раздел **🗓 Расписание** в панели или файл
`System/parse_schedule.json`:

```json
{
  "enabled": true,
  "time": "07:00",
  "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
  "timezone": "Europe/Moscow",
  "run_if_missed": true
}
```

* `time` — время запуска (ЧЧ:ММ) в поясе `timezone`;
* `weekdays` — можно оставить только рабочие дни;
* `run_if_missed` — «догнать» пропущенный запуск, если приложение было выключено.

### Вариант B. systemd + timer (Linux, работает без открытой панели)

```bash
sudo cp deploy/systemd/parser-streamlit.service /etc/systemd/system/
sudo cp deploy/systemd/parser-cycle.service deploy/systemd/parser-cycle.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now parser-streamlit     # панель 24/7
sudo systemctl enable --now parser-cycle.timer   # парсинг в 07:00
systemctl list-timers parser-cycle.timer
```

`Persistent=true` в таймере значит: если сервер был выключен в 07:00,
запуск произойдёт сразу после включения.
Проверьте часовой пояс сервера: `timedatectl` → должно быть `Europe/Moscow`,
иначе поправьте `OnCalendar` в `parser-cycle.timer`.

Перед установкой отредактируйте пути (`/opt/parser`) и пользователя (`parser`).

### Вариант C. cron

```cron
CRON_TZ=Europe/Moscow
0 7 * * * cd /opt/parser && .venv/bin/python run_cycle_once.py >> System/logs/cron.log 2>&1
```

Готовый пример: `deploy/cron/parser.cron`.

### Вариант D. Windows (Планировщик задач)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1 -Time "07:00"
```

Создаёт задачу `MarketplaceParser`, которая вызывает
`deploy\windows\run_cycle.bat` ежедневно в 07:00 (в том числе если панель закрыта).
Подробнее — `deploy/windows/README.txt`.

### Вариант E. Docker

```bash
docker compose up -d --build     # http://localhost:8501
docker compose logs -f
```

Каталог `./System` монтируется томом — там ключи, базы и логи.
Планировщик работает внутри контейнера; `restart: unless-stopped` поднимает
панель после перезагрузки хоста.

---

## 3. Ручной запуск цикла

```bash
python run_cycle_once.py                     # полный цикл
python run_cycle_once.py --step orders       # только заказы
python run_cycle_once.py --step stocks       # только остатки
python run_cycle_once.py --check             # показать расписание и следующий запуск
python run_cycle_once.py --force             # игнорировать выключенный автозапуск
python run_cycle_once.py --json              # машинный вывод результата
```

Коды возврата: `0` — успех/частичный успех, `1` — ошибка, `2` — цикл уже идёт.
В панели то же самое — кнопка **🚀 Запустить цикл сейчас**.

---

## 4. Что где лежит

| Файл | Назначение |
|---|---|
| `streamlit_app.py` | интерфейс панели (7 разделов) |
| `streamlit_scheduler.py` | планировщик ежедневного запуска внутри Streamlit |
| `services/parse_schedule.py` | чтение/проверка расписания, расчёт ближайших запусков |
| `services/cycle_runner.py` | сам цикл (склад → остатки → заказы → цены) |
| `services/market_controls.py` | остатки/цены/флаги: переключение МП и поставщиков, пересчёт |
| `run_cycle_once.py` | CLI-запуск цикла для cron/systemd/Windows |
| `clients/http_client.py` | HTTP-клиент с таймаутами и повторами (пакет отсутствовал в репозитории) |
| `deploy/` | готовые unit-файлы, cron, bat/ps1 для Windows |
| `System/.env.example` | шаблон ключей |
| `System/parse_schedule.json` | расписание парсинга (создаётся автоматически) |
| `System/logs/app.log` | общий лог (Loguru, ротация 1 день, хранение 7 дней) |

Панель: **📊 Обзор** (состояние, флаги, журнал циклов, лог),
**🗓 Расписание**, **📦 Товары** (таблицы с редактированием), **🧾 Заказы**,
**📜 Логи**, **⚙️ Диагностика** (проверка ключей, файлов, ручной запуск шагов),
**❓ Помощь**.

---

## 5. Ключи (`System/.env`)

```ini
PASSWORD_HASH=            # хеш пароля для входа в панель (рекомендуется)
PASSWORD=
SECRET_KEY=

wb_token=
warehouseId=

ozon_client_ID=
ozon_API_key=

ym_token=
campaign_id=
businessId=

telegram_got_token_error=
telegram_chat_id_error=

GOOGLE_SERVICE_ACCOUNT_FILE=System/my-python-397519-3688db4697d6.json
```

Хеш пароля:

```bash
python -c "from werkzeug.security import generate_password_hash as g; print(g('ваш_пароль'))"
```

Если ни `PASSWORD_HASH`, ни `PASSWORD` не заданы, панель открывается без входа —
задайте пароль, если панель доступна из сети.

> ⚠️ Панель слушает `0.0.0.0:8501` (чтобы работать за прокси/в Docker).
> Не выставляйте порт в интернет без пароля и HTTPS.

---

## 6. Как гарантируется, что цикл не запустится дважды

* `System/automation_cycle.lock` — межпроцессная блокировка (Streamlit, cron
  и `main.py` не пересекутся); «зависшая» блокировка снимается по возрасту/PID.
* Внутри процесса — `threading.Lock`, планировщик настроен на
  `max_instances=1`, `coalesce=True`.
* Состояние цикла пишется в `System/automation_state.json` и видно всем
  интерфейсам: панель, CLI, Flask-наследие.

---

## 7. Что изменилось в репозитории

* **Добавлен Streamlit-интерфейс** (`streamlit_app.py`) — замена Flask-панели:
  обзор, расписание, товары, заказы, логи, диагностика.
* **Добавлен планировщик** ежедневного запуска в 07:00 с настройкой времени,
  дней недели и часового пояса (`streamlit_scheduler.py`, `services/parse_schedule.py`).
* **Бизнес-логика вынесена из Flask в сервисы** (`services/market_controls.py`,
  `services/cycle_runner.py`). `web_app.py` теперь делегирует туда же, поэтому
  логика существует в одном экземпляре, а не в двух.
* **Восстановлен отсутствовавший пакет `clients`** — без него `stock.py`,
  `price_updater_master.py` и `order_notifications.py` не импортировались.
  Новый клиент добавляет таймауты и повторы: «зависший» запрос к маркетплейсу
  больше не останавливает цикл на неопределённое время.
* **Добавлены** `run_cycle_once.py`, `deploy/` (systemd, cron, Windows),
  `Dockerfile`, `docker-compose.yml`, `.streamlit/config.toml`,
  `System/.env.example`, `tools/create_demo_db.py`.

Flask-интерфейс (`web_app.py`) не удалён — он по-прежнему запускается
(`python web_app.py`, порт 5050), но для работы нужен только Streamlit.
Запускать оба одновременно не нужно: планировщик есть в каждом, но
межпроцессная блокировка не даст выполнить два цикла параллельно.

---

## 8. Частые проблемы

| Симптом | Что делать |
|---|---|
| Панель открывается, но данных нет | `python tools/create_demo_db.py` (демо) или проверьте `System/marketplace_base.db` |
| Цикл завершается со статусом «с предупреждениями» | Смотрите **📜 Логи**: обычно это отсутствующие/просроченные ключи в `System/.env` |
| `ModuleNotFoundError: clients` | Вы на старой версии репозитория — нужен пакет `clients/` из этого коммита |
| Автозапуск не сработал | Панель должна быть запущена (вариант A) либо включите timer/cron/задачу Windows (B–D) |
| Время запуска «съехало» | Проверьте `timezone` в `System/parse_schedule.json` и `timedatectl` / `TZ` на сервере |
| Порт 8501 занят | `streamlit run streamlit_app.py --server.port 8502` |
