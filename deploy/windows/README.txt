Windows: как включить ежедневный парсинг в 07:00
=================================================

1) Панель (нужна для ручного управления и логов):

   deploy\windows\start_streamlit.bat
   → откроется http://localhost:8501
   Пока это окно открыто, планировщик внутри панели сам запустит цикл в 07:00.

2) Надёжный вариант — Планировщик задач Windows (работает даже при закрытой панели):

   PowerShell от администратора:
     powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1 -Time "07:00"

   Проверка:  Get-ScheduledTask -TaskName MarketplaceParser
   Ручной запуск: Start-ScheduledTask -TaskName MarketplaceParser
   Удаление:  Unregister-ScheduledTask -TaskName MarketplaceParser

   Лог выполнения: System\logs\cron.log
