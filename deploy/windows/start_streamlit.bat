@echo off
rem Запуск панели парсера на Windows двойным щелчком.
rem Панель откроется на http://localhost:8501
cd /d "%~dp0..\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m streamlit run streamlit_app.py
) else (
    python -m streamlit run streamlit_app.py
)
pause
