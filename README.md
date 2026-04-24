<div align="center">

# ⚔️ Artificial Gladiator League

**The AI arena where machine-learning models battle in Chess and Breakthrough.**

Upload your Hugging Face model, watch it fight in real-time tournaments, climb the Elo leaderboard, and chat with other builders — all inside a secure, sandboxed platform.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-5.x-092E20?logo=django&logoColor=white)
![Channels](https://img.shields.io/badge/Django_Channels-4.x-092E20?logo=django&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-5.x-37814A?logo=celery&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8.0+-4479A1?logo=mysql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7.0+-DC382D?logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Sandbox-2496ED?logo=docker&logoColor=white)
![Hugging Face](https://img.shields.io/badge/🤗_Hugging_Face-Model_Hub-FFD21E)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## Highlights

- **Two games** — Classical Chess (via `python-chess`) and Breakthrough (custom engine)
- **Hugging Face integration** — Link your HF repo; models are downloaded, scanned, and verified automatically
- **Secure Docker sandbox** — Every inference call runs in a disposable, network-less container
- **Real-time WebSockets** — Live game boards, tournament brackets, and chat powered by Django Channels
- **Celery task queue** — Automated tournament scheduling, stale-game cleanup, and background jobs
- **Elo rating system** — Per-game-type ratings with rating-lock and model-integrity checks
- **Community features** — Forum, direct messaging, in-tournament chat
- **GDPR-ready** — Data export, account deletion, and formal request workflow
- **Anti-cheat** — IP logging, country restrictions, duplicate-account detection

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Project Structure](#project-structure)
3. [AI Model Upload & Sandbox Security](#ai-model-upload--sandbox-security)
4. [Tournament Management](#tournament-management)
5. [Celery Workers](#celery-workers)
6. [GDPR Compliance](#gdpr-compliance)
7. [Anti-Cheat / Multi-Account Detection](#anti-cheat--multi-account-detection)
8. [Deployment](#deployment)
9. [Contributing](#contributing)
10. [License](#license)

---

## Quick Start

### 1. Prerequisites

| Tool    | Version |
|---------|---------|
| Python  | 3.11+   |
| MySQL   | 8.0+    |
| Redis   | 7.0+    |
| Docker  | 24.0+ (for AI sandboxing) |

> **Note:** No Node.js required — Tailwind CSS is loaded via CDN.

### 2. Clone & Install

```bash
git clone <repo-url>
cd agladiator/agladiator          # directory containing manage.py
python -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. MySQL Setup

```sql
CREATE DATABASE aigladiator
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'gladiator'@'localhost' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON aigladiator.* TO 'gladiator'@'localhost';
FLUSH PRIVILEGES;
```

<details>
<summary><strong>Optional: MySQL tuning (my.cnf / my.ini)</strong></summary>

```ini
[mysqld]
innodb_buffer_pool_size = 512M
innodb_log_file_size    = 128M
innodb_flush_log_at_trx_commit = 2
```
</details>

### 4. Environment Variables

Create a `.env` file in the project root (next to `manage.py`) or export these variables:

```bash
DJANGO_SECRET_KEY="your-production-secret-key"
DJANGO_DEBUG="False"
DJANGO_ALLOWED_HOSTS="yourdomain.com,www.yourdomain.com"
DB_NAME="aigladiator"
DB_USER="gladiator"
DB_PASSWORD="your-password"
DB_HOST="127.0.0.1"
DB_PORT="3306"
REDIS_URL="redis://127.0.0.1:6379/0"
HF_PLATFORM_TOKEN=""              # Hugging Face read-repos token
HF_WEBHOOK_SECRET=""              # HMAC secret for HF webhook payloads
HF_OAUTH_CLIENT_ID=""             # HF OAuth app credentials
HF_OAUTH_CLIENT_SECRET=""
```

### Disable startup pre-warm in development

The application pre-warms verified user models at startup by default. To avoid downloading models when running the local development server, set `PREWARM_MODELS=false`.

- Persistently (add to `.env` next to `manage.py`):

```bash
PREWARM_MODELS=false
```

- Temporarily (PowerShell):

```powershell
$env:PREWARM_MODELS = "false"
python manage.py runserver
```

- Temporarily (bash / zsh):

```bash
PREWARM_MODELS=false python manage.py runserver
```

- Temporarily (Windows CMD):

```cmd
set PREWARM_MODELS=false
python manage.py runserver
```

Note: `manage.py` now defaults to skipping the pre-warm when you run `python manage.py runserver`. To force pre-warming in development set `PREWARM_MODELS=true`.

### 5. Migrations & Static Files

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

### 6. Run the Development Server

```bash
# Daphne (ASGI — full WebSocket support)
daphne -b 0.0.0.0 -p 8000 agladiator.asgi:application

# Or Django dev server (limited — no WebSockets)
python manage.py runserver
```

> In development, if Redis is not running the app automatically falls back to in-memory channel layers and eager Celery execution.

---

## Project Structure

```
agladiator/                        # ← repository root
├── manage.py
├── requirements.txt
├── Procfile
├── Dockerfile.sandbox             # Docker image for sandboxed AI inference
│
├── agladiator/                    # Django project package
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py                    # Channels ASGI entry point
│   ├── wsgi.py
│   └── celery.py                  # Celery app bootstrap
│
├── apps/
│   ├── core/                      # Home, leaderboard, about, static pages
│   ├── users/                     # Auth, profiles, GDPR, HF OAuth, model lifecycle
│   ├── games/                     # Chess & Breakthrough engines, lobbies, WebSocket consumers
│   ├── tournaments/               # Brackets, scheduling, Celery tasks, management commands
│   ├── forum/                     # Community discussion board
│   └── chat/                      # Real-time direct messaging & notifications
│
├── templates/                     # Django templates (Tailwind dark / light themes)
│   ├── base.html
│   ├── games/
│   ├── tournaments/
│   ├── users/
│   ├── forum/
│   ├── chat/
│   └── registration/
│
├── static/
│   ├── css/                       # custom.css, gladiator.css
│   ├── img/
│   └── flags/                     # Country flag SVGs
│
├── media/                         # User uploads (gitignored)
└── my-chessbot/                   # Reference chess-bot model (SafeTensors)
```

---

## AI Model Upload & Sandbox Security

> ⚠️ **CRITICAL:** User-submitted AI code is **never** executed inside the main Django process.

### How It Works

1. Users link a **Hugging Face model repository** to their profile.
2. The platform downloads the model using `huggingface_hub`, then scans it with **Bandit**, **ModelScan**, **Fickling**, and **PickleScan**.
3. Every move-inference call runs inside a **disposable Docker container** built from `Dockerfile.sandbox`.

### Sandbox Guarantees

| Control              | Implementation                                               |
|----------------------|--------------------------------------------------------------|
| **Isolation**        | Each inference runs in a fresh, disposable container         |
| **No network**       | `--network none` — container cannot reach the internet       |
| **Resource limits**  | CPU, RAM, and disk quotas enforced via Docker flags          |
| **Read-only FS**     | Only `/tmp` is writable (size-limited tmpfs)                 |
| **Time limits**      | Hard kill after `SANDBOX_MOVE_TIMEOUT` (default 30 s)        |
| **No host mounts**   | Model files are copied in, never bind-mounted                |

```bash
# Example sandbox invocation
docker run --rm \
  --network none \
  --memory 512m \
  --cpus 1.0 \
  --read-only \
  --tmpfs /tmp:size=64m \
  -v /path/to/model:/workspace/model:ro \
  python:3.11-slim \
  python /workspace/predict.py
```

### Upload Validation

- Allowed extensions: `.py`, `.pth` (configurable via `AI_UPLOAD_ALLOWED_EXTENSIONS`)
- Max file size: **50 MB** (configurable via `AI_UPLOAD_MAX_SIZE_MB`)
- Model format: **SafeTensors** required
- Full verification pipeline timeout: `SANDBOX_VERIFY_TIMEOUT` (default 300 s)

---

## Tournament Management

### Management Commands

```bash
# Schedule the weekly slate of tournaments
python manage.py schedule_tournaments

# Create a QA/test tournament
python manage.py create_qa_tournament

# Run a gauntlet (round-robin stress test)
python manage.py run_gauntlet
```

### Weekly Schedule (5 Tournaments)

| Day | Size  | Category      | Prizes              |
|-----|-------|---------------|----------------------|
| Mon | Small | Beginner      | $10 (1st)            |
| Tue | Small | Intermediate  | $10 (1st)            |
| Wed | Small | Advanced      | $10 (1st)            |
| Thu | Small | Expert        | $10 (1st)            |
| Sat | Large | Rotating      | $30 / $20 / $10      |

### Cron / Scheduler

```bash
# crontab — daily at midnight UTC
0 0 * * * cd /path/to/agladiator && python manage.py schedule_tournaments
```

Or use **Celery Beat** (see below) for fully automated scheduling.

---

## Celery Workers

The project uses Celery for background task processing (tournament scheduling, stale-game cleanup, etc.).

```bash
# Start the worker
celery -A agladiator worker -l info

# Start the beat scheduler (periodic tasks)
celery -A agladiator beat -l info
```

**Broker:** Redis (same `REDIS_URL` used by channel layers).

> In `DEBUG` mode without Redis, Celery falls back to eager (synchronous) execution automatically.

---

## GDPR Compliance

Users can:

- **Export data** → `GET /users/gdpr/export/` — full JSON download of personal data
- **Delete account** → `POST /users/gdpr/delete/` — permanent, irreversible erasure
- **Formal requests** → `GET /users/gdpr/` — submit data-access or deletion requests

Admin dashboard: `/admin/users/gdprrequest/`

---

## Anti-Cheat / Multi-Account Detection

- Registration IP logged for admin review
- **Country restrictions** enforced at registration (blocked: `KP`, `IR`, `SY`, `CU`, `SD`)
- Duplicate-account detection via IP heuristics (expandable with device fingerprinting)
- Model-integrity middleware verifies that deployed model weights haven't changed between rated games
- All violations logged — admin review via Django admin

---

## Deployment

### Heroku

```bash
heroku create artificial-gladiator
heroku addons:create jawsdb:kitefin       # MySQL
heroku addons:create heroku-redis         # Redis
heroku config:set DJANGO_SECRET_KEY="..." DJANGO_DEBUG="False"
git push heroku main
heroku run python manage.py migrate
heroku run python manage.py collectstatic --noinput
```

### AWS / VPS

1. Use the provided `Procfile` with a process manager (systemd, supervisor).
2. **Daphne** handles both HTTP and WebSocket traffic.
3. **Nginx** as reverse proxy for static/media files and TLS termination.
4. **Redis** for channel layers + Celery broker.
5. **MySQL** via RDS or self-hosted.
6. **Docker** must be available for sandbox inference.

### Daphne (recommended)

```bash
daphne -b 0.0.0.0 -p 8000 agladiator.asgi:application
```

### Gunicorn (HTTP only — no WebSockets)

```bash
gunicorn agladiator.wsgi:application --bind 0.0.0.0:8000 --workers 4
```

> Use **Daphne** for full functionality. Gunicorn does not support WebSockets.

---

## Contributing

Contributions are welcome! Here's how to get started:

1. **Fork** the repository and clone your fork.
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes and add tests where appropriate.
4. Run the test suite: `python manage.py test`
5. Commit with a clear message: `git commit -m "Add my feature"`
6. Push and open a **Pull Request** against `main`.

Please follow the existing code style and keep PRs focused on a single change. For larger features, open an issue first to discuss the approach.

---

## License

This project is licensed under the [MIT License](LICENSE).

© Artificial Gladiator League
