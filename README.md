# Artificial Gladiator

Skill-based AI chess competition platform built with Django, Channels, and python-chess.

## Quick Start

### 1. Prerequisites

| Tool     | Version   |
|----------|-----------|
| Python   | 3.11+     |
| MySQL    | 8.0+      |
| Redis    | 7.0+      |
| Node.js  | (none — Tailwind via CDN) |

### 2. Clone & Install

```bash
git clone <repo-url>
cd agladiator/mysite
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. MySQL Setup

```sql
CREATE DATABASE chess_ai_platform
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'chessai'@'localhost' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON chess_ai_platform.* TO 'chessai'@'localhost';
FLUSH PRIVILEGES;
```

**Optimisation (add to `my.cnf` / `my.ini`):**

```ini
[mysqld]
innodb_buffer_pool_size = 512M
innodb_log_file_size = 128M
innodb_flush_log_at_trx_commit = 2
query_cache_type = 0

# Recommended indexes (auto-created by Django migrations):
#   - users_customuser: elo, total_games, is_active (leaderboard queries)
#   - tournaments_match: tournament_id + round_num + bracket_position
#   - games_game: player1_id, player2_id, timestamp
```

### 4. Environment Variables

```bash
export DJANGO_SECRET_KEY="your-production-secret-key"
export DJANGO_DEBUG="False"
export DJANGO_ALLOWED_HOSTS="yourdomain.com,www.yourdomain.com"
export DB_NAME="chess_ai_platform"
export DB_USER="chessai"
export DB_PASSWORD="your-password"
export DB_HOST="127.0.0.1"
export DB_PORT="3306"
export REDIS_URL="redis://127.0.0.1:6379/0"
```

### 5. Migrations & Static Files

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

### 6. Run Development Server

```bash
# Daphne (ASGI — WebSocket support)
daphne -b 0.0.0.0 -p 8000 mysite.asgi:application

# Or Django dev server (no WebSockets)
python manage.py runserver
```

---

## Tournament Management

### Auto-schedule & run tournaments

```bash
# Schedule upcoming weekly tournaments
python manage.py run_tournaments --schedule-only

# Run all pending/full tournaments (simulate matches)
python manage.py run_tournaments --run-only

# Do both
python manage.py run_tournaments
```

**Weekly schedule (5 tournaments):**

| Day | Type  | Category     | Prize     |
|-----|-------|-------------|-----------|
| Mon | Small | Beginner    | $10 (1st) |
| Tue | Small | Intermediate| $10 (1st) |
| Wed | Small | Advanced    | $10 (1st) |
| Thu | Small | Expert      | $10 (1st) |
| Sat | Large | Rotating    | $30/$20/$10 (1st/2nd/3rd) |

Prizes are sponsor-funded (mock PayPal payouts — see logs). Unclaimed prizes roll over.

### Cron / Scheduler

```bash
# crontab — run daily at midnight UTC
0 0 * * * cd /path/to/mysite && python manage.py run_tournaments
```

---

## GDPR Compliance

Users can:
- **Export data** → `GET /users/gdpr/export/` — JSON download
- **Delete account** → `POST /users/gdpr/delete/` — permanent erasure
- **Formal requests** → `GET /users/gdpr/` — submit data-access or deletion requests

Admin dashboard shows all GDPR requests under `/admin/users/gdprrequest/`.

---

## AI Execution Security

> ⚠️ **CRITICAL**: Never execute uploaded AI files directly in the main Django process.

### Sandbox Requirements

1. **Docker isolation** — Each AI agent runs in a disposable container.
2. **No network access** — Container networking disabled (`--network none`).
3. **Resource limits** — CPU, RAM, and disk quotas enforced.
4. **Read-only filesystem** — Except designated output directory.
5. **Time limits** — Hard kill after time control expires + 5s buffer.
6. **No host mounts** — AI files copied in, never bind-mounted.

### Recommended Docker Setup

```bash
docker run --rm \
  --network none \
  --memory 512m \
  --cpus 1.0 \
  --read-only \
  --tmpfs /tmp:size=64m \
  -v /path/to/ai_file.py:/agent/bot.py:ro \
  ai-chess-sandbox \
  python /agent/bot.py
```

### File Validation

- Allowed extensions: `.py`, `.pth` (configurable via `AI_UPLOAD_ALLOWED_EXTENSIONS`)
- Max file size: 50 MB (configurable via `AI_UPLOAD_MAX_SIZE_MB`)
- Files stored under `media/ai_bots/<user_id>/`
- Rights declaration required on upload

---

## Anti-Cheat / Multi-Account Detection

- Registration IP is logged for admin review
- Country restrictions enforced at registration (blocked: KP, IR, SY, CU, SD)
- Duplicate account detection via IP heuristics (expandable with device fingerprinting)
- All violations logged — admin review via Django admin

---

## Deployment

### Heroku

```bash
heroku create ai-chess-arena
heroku addons:create jawsdb:kitefin   # MySQL
heroku addons:create heroku-redis     # Redis
heroku config:set DJANGO_SECRET_KEY="..."
heroku config:set DJANGO_DEBUG="False"
git push heroku main
heroku run python manage.py migrate
heroku run python manage.py collectstatic --noinput
```

### AWS / VPS

1. Use the provided `Procfile` with a process manager (systemd, supervisor).
2. Daphne handles both HTTP and WebSocket traffic.
3. Nginx as reverse proxy for static/media files.
4. Redis for channel layers.
5. MySQL RDS or self-hosted.

### Gunicorn (HTTP only, no WebSockets)

```bash
gunicorn mysite.wsgi:application --bind 0.0.0.0:8000 --workers 4
```

> Use Daphne for WebSocket support. Gunicorn is HTTP-only.

---

## Project Structure

```
mysite/
├── manage.py
├── requirements.txt
├── Procfile
├── mysite/              # Django project config
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── apps/
│   ├── core/            # Home, leaderboard, static pages
│   ├── users/           # Auth, profile, GDPR, signals
│   ├── games/           # Casual quick-pair games
│   └── tournaments/     # Tournament brackets, management commands
├── templates/           # Django templates (Tailwind dark/light)
├── static/css/          # custom.css
└── media/ai_bots/       # Uploaded AI files (gitignored)
```

---

## License

All rights reserved. © Artificial Gladiator.
