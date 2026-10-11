"""Настройки gunicorn на хостинге (Render). Gunicorn читает этот файл сам, если запущен из папки сайта.

Работает и со стандартной командой Render `gunicorn app:app`.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))  # на бесплатном плане мало памяти — один процесс
threads = 8
timeout = 120  # ответы ИИ на зеркале и у критика идут 15–40 секунд; по умолчанию gunicorn рвёт через 30
