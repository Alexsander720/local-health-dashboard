import sqlite3
import json
from pathlib import Path

db_path = Path("sleep-data/health_common.db")
c = sqlite3.connect(db_path).cursor()
try:
    c.execute('SELECT start_time, end_time, type, duration FROM sport_record WHERE start_time >= "2026-04-28"')
    rows = c.fetchall()
    print("Mobvoi Health workouts:")
    for row in rows:
        print(row)
except Exception as e:
    print(e)
