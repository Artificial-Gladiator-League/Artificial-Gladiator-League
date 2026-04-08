# ──────────────────────────────────────────────
# Procfile — Heroku / AWS Elastic Beanstalk / Dokku
# ──────────────────────────────────────────────
# HTTP via Daphne (ASGI — supports WebSockets)
web: daphne -b 0.0.0.0 -p $PORT mysite.asgi:application

# Background worker — run weekly tournaments
# Schedule via Heroku Scheduler or cron: daily at 00:00 UTC
worker: python manage.py run_tournaments
