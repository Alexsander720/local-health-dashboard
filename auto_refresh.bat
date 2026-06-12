@echo off
REM Авто-обновление health dashboard. Запускается по расписанию.
REM Windows Task Scheduler тикает каждые 15 минут.

cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM 1) Sync свежие данные с телефона (timeout 90s встроен в скрипт)
python health_sync.py --days 14 >> auto_refresh.log 2>&1

REM 2) Обновить AI кэш — respects TTL (day=30мин, week=3ч, month=12ч, категории=4-6ч)
REM    вызовы дешёвые: если кэш свежий, Gemini API не дёргается
REM    9 периодов: 3 общих (day/week/month) + 6 категорий (sleep/body/nutrition/foodprofile/activity/health)
python gemini_analyzer.py --period all >> auto_refresh.log 2>&1

REM 3) Пересобрать статический dashboard.html
python build_dashboard.py >> auto_refresh.log 2>&1

echo [%date% %time%] refresh done >> auto_refresh.log
