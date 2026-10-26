from pathlib import Path
from loguru import logger
import sys

# Папка для логов
LOG_DIR = Path("System/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Удаляем все предыдущие обработчики
logger.remove()

# Формат лога
log_format = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}"

# Поток в консоль. Не меняем кодировку самого stdout: IDE и службы Windows
# могут ожидать CP1251 или UTF-8. Если текущая кодировка не поддерживает эмодзи,
# заменяем только такие символы, сохраняя читаемый русский текст.
def _write_console(message):
    text = str(message)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding)
        sys.stdout.write(safe_text)
    sys.stdout.flush()


logger.add(
    _write_console,
    format=log_format,
    level="DEBUG",
    colorize=False,
    backtrace=False,
    diagnose=False,
)

# Запись в файл
logger.add(
    LOG_DIR / "app.log",
    format=log_format,
    level="DEBUG",
    rotation="1 day",
    retention="7 days",
    encoding="utf-8",
    backtrace=False,
    diagnose=False,
)

# Экспорт
__all__ = ["logger", "LOG_DIR"]
