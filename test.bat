@echo off
cd /d "%~dp0"
IF EXIST venv\Scripts\activate.bat call venv\Scripts\activate.bat
pytest tests -v --tb=short
