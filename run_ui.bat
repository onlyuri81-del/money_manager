@echo off
cd /d "%~dp0"
python -m pip install -e ".[ui]"
if errorlevel 1 (
    echo Installation failed. Check Python 3.11+ and your internet connection.
    pause
    exit /b 1
)
python -m streamlit run streamlit_app.py --server.address 127.0.0.1
if errorlevel 1 pause
