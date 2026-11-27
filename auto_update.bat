@echo off
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
 git pull --ff-only || exit /b 1
 "%PY%" update_lotto.py --years 5 || exit /b 1
 git add -- data/lotto_history.csv || exit /b 1
 git diff --cached --quiet -- data/lotto_history.csv
 if errorlevel 1 git commit --only -m "Update lotto history" -- data/lotto_history.csv || exit /b 1
 git push || exit /b 1
