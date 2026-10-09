# Artificial Gladiator League — Production Deployment Guide

Target: Ubuntu 22.04 LTS · server1.agladiator.com  
Stack: Nginx → Gunicorn (WSGI) + Daphne (ASGI/WS) · MySQL · Redis · Celery · Supervisor

---

## 1. System packages

```bash
sudo apt update && sudo apt install -y \
    python3.11 python3.11-venv python3-pip \
    nginx supervisor \
    mysql-server libmysqlclient-dev \
    redis-server \
    pkg-config build-essential
```

---

## 2. MySQL — create a dedicated user (never use root)

```sql
-- Run as: sudo mysql
CREATE DATABASE agladiator CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'agladiator_user'@'localhost' IDENTIFIED BY '<strong-password>';
GRANT ALL PRIVILEGES ON agladiator.* TO 'agladiator_user'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

---

## 3. Application user & directory

```bash
sudo useradd --system --shell /bin/bash --home /opt/agladiator agladiator
sudo mkdir -p /opt/agladiator /var/lib/agladiator/user_models /var/lib/agladiator/shared_models
sudo chown -R agladiator:agladiator /opt/agladiator /var/lib/agladiator
```

---

## 4. Deploy the code

```bash
sudo -u agladiator git clone https://github.com/Artificial-Gladiator-League/Artificial-Gladiator-League.git \
    /opt/agladiator/app

cd /opt/agladiator/app
sudo -u agladiator python3.11 -m venv /opt/agladiator/venv
sudo -u agladiator /opt/agladiator/venv/bin/pip install --upgrade pip
sudo -u agladiator /opt/agladiator/venv/bin/pip install -r requirements.txt
```

---

## 5. Environment file

```bash
sudo -u agladiator cp /opt/agladiator/app/.env.example /opt/agladiator/app/.env
sudo -u agladiator nano /opt/agladiator/app/.env   # fill in real values
sudo chmod 600 /opt/agladiator/app/.env
```

Key values to set in `.env`:

| Variable | Value |
|---|---|
| `DJANGO_SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(50))"` |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | `server1.agladiator.com,agladiator.com,www.agladiator.com` |
| `DB_USER` | `agladiator_user` |
| `DB_PASSWORD` | the password set in step 2 |
| `DJANGO_LOG_DIR` | `/var/log/agladiator` |

---

## 6. Django setup

```bash
cd /opt/agladiator/app

sudo -u agladiator /opt/agladiator/venv/bin/python manage.py migrate
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py collectstatic --no-input
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py createsuperuser
```

Create the log directory:

```bash
sudo mkdir -p /var/log/agladiator
sudo chown agladiator:agladiator /var/log/agladiator
```

---

## 7. Gunicorn (WSGI — HTTP only; WebSockets handled by Daphne)

`/opt/agladiator/gunicorn.conf.py`:

```python
bind = "127.0.0.1:8000"
workers = 4                  # 2 × CPU cores + 1
worker_class = "sync"
timeout = 120
accesslog = "/var/log/agladiator/gunicorn-access.log"
errorlog  = "/var/log/agladiator/gunicorn-error.log"
loglevel  = "info"
chdir     = "/opt/agladiator/app"
```

---

## 8. Daphne (ASGI — WebSocket connections)

Daphne is already listed first in `INSTALLED_APPS` and handles the ASGI protocol.

Bind it to a separate port (or Unix socket) so Nginx can route `/ws/` separately:

```
daphne -b 127.0.0.1 -p 8001 agladiator.asgi:application
```

---

## 9. Supervisor config

`/etc/supervisor/conf.d/agladiator.conf`:

```ini
[program:agladiator-gunicorn]
command=/opt/agladiator/venv/bin/gunicorn agladiator.wsgi:application -c /opt/agladiator/gunicorn.conf.py
directory=/opt/agladiator/app
user=agladiator
autostart=true
autorestart=true
stderr_logfile=/var/log/agladiator/gunicorn-supervisor.err.log
stdout_logfile=/var/log/agladiator/gunicorn-supervisor.out.log
environment=PYTHONPATH="/opt/agladiator/app"

[program:agladiator-daphne]
command=/opt/agladiator/venv/bin/daphne -b 127.0.0.1 -p 8001 agladiator.asgi:application
directory=/opt/agladiator/app
user=agladiator
autostart=true
autorestart=true
stderr_logfile=/var/log/agladiator/daphne-supervisor.err.log
stdout_logfile=/var/log/agladiator/daphne-supervisor.out.log

[program:agladiator-celery]
command=/opt/agladiator/venv/bin/celery -A agladiator worker -l info -c 2
directory=/opt/agladiator/app
user=agladiator
autostart=true
autorestart=true
stderr_logfile=/var/log/agladiator/celery-supervisor.err.log
stdout_logfile=/var/log/agladiator/celery-supervisor.out.log

[program:agladiator-celerybeat]
command=/opt/agladiator/venv/bin/celery -A agladiator beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
directory=/opt/agladiator/app
user=agladiator
autostart=true
autorestart=true
stderr_logfile=/var/log/agladiator/celerybeat-supervisor.err.log
stdout_logfile=/var/log/agladiator/celerybeat-supervisor.out.log
```

Load and start:

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start all
```

---

## 10. Nginx config

`/etc/nginx/sites-available/agladiator`:

```nginx
upstream django_wsgi {
    server 127.0.0.1:8000;
}

upstream django_asgi {
    server 127.0.0.1:8001;
}

server {
    listen 80;
    server_name server1.agladiator.com agladiator.com www.agladiator.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name server1.agladiator.com agladiator.com www.agladiator.com;

    ssl_certificate     /etc/letsencrypt/live/agladiator.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agladiator.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 60M;

    # Static files (WhiteNoise serves these, but Nginx is faster for large files)
    location /static/ {
        alias /opt/agladiator/app/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location /media/ {
        alias /opt/agladiator/app/media/;
    }

    # WebSocket connections → Daphne
    location /ws/ {
        proxy_pass http://django_asgi;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    # All other requests → Gunicorn
    location / {
        proxy_pass http://django_wsgi;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/agladiator /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 11. TLS certificate (Let's Encrypt)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d agladiator.com -d www.agladiator.com -d server1.agladiator.com
```

Auto-renewal is configured automatically by certbot. Verify with:

```bash
sudo systemctl status certbot.timer
```

---

## 12. Model storage permissions

```bash
sudo mkdir -p /var/lib/agladiator/user_models/hf_home \
              /var/lib/agladiator/user_models/hf_hub_cache \
              /var/lib/agladiator/user_models/live
sudo chown -R agladiator:agladiator /var/lib/agladiator
sudo chmod 750 /var/lib/agladiator
```

---

## 13. Deploying updates

```bash
cd /opt/agladiator/app
sudo -u agladiator git pull origin main
sudo -u agladiator /opt/agladiator/venv/bin/pip install -r requirements.txt
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py migrate
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py collectstatic --no-input
sudo supervisorctl restart agladiator-gunicorn agladiator-daphne agladiator-celery agladiator-celerybeat
```

---

## 14. Health check

```bash
# Django check (no actual DB call)
curl -I https://server1.agladiator.com/

# Confirm DEBUG is off (must NOT show tracebacks)
curl https://server1.agladiator.com/nonexistent-path/

# MySQL connectivity
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py dbshell

# Supervisor status
sudo supervisorctl status
```

---

## 15. Guest mode (optional, off by default)

Guest mode lets a visitor sign in with Hugging Face (scopes `openid profile` only), connect their own **public** model/data repos and play **unrated** games against other guests. It is additive: with `GUEST_MODE_ENABLED=False` (the default) no guest page, link, route or background task does anything.

### 15.1 Before enabling

1. Review the guest paragraph in `templates/core/terms.html` (section S2, shown only when the flag is on) and update your Termly privacy and cookie policies yourself.
2. Make sure the Hugging Face OAuth app (`HF_OAUTH_CLIENT_ID`, `HF_OAUTH_CLIENT_SECRET`) is configured. Guest sign-in reuses the existing OAuth callback (`oauth/hf/callback/`), so no new redirect URI is needed.
3. Run the database migrations (they are additive and reversible):

```bash
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py migrate users 0042
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py migrate games 0012
```

4. The Docker sandbox must work (guest model code never runs outside it). `SANDBOX_ENABLE_LOCAL_FALLBACK` is forced to `False` while guest mode is on, and `manage.py check` fails with `users.E001` if it is not.

### 15.2 Settings (environment variables in `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GUEST_MODE_ENABLED` | `False` | Master switch. |
| `GUEST_TTL_DAYS` | `3` | A guest account is purged after this many days of inactivity (minimum 1). |
| `GUEST_SNAPSHOT_IDLE_MINUTES` | `120` | Downloaded repo snapshots are deleted after this idle time (never during an ongoing game) and always at logout. |
| `GUEST_CELERY_QUEUE` | `celery` | Queue used for guest tasks and the guest beat entries. |
| `GUEST_MAX_ACTIVE` | `200` | Maximum number of active guest accounts. |
| `GUEST_MAX_REPO_MB` / `GUEST_MAX_REPO_FILES` | `300` / `200` | Size and file-count limit, checked before anything is downloaded. |
| `GUEST_RATE_SIGNIN_PER_IP` / `_PER_SUB` | `20/h` / `10/h` | Sign-in rate limits. |
| `GUEST_RATE_CONNECT_PER_USER` | `12/h` | Repo connect limit per guest. |
| `GUEST_RATE_UPGRADE_PER_USER` | `5/h` | Upgrade e-mail limit per guest. |
| `TRUSTED_PROXY_HEADER` | empty | Header holding the real client IP, used for the per-IP limit. Leave empty if the app is reachable directly. |
| `SANDBOX_GUEST_MEMORY_LIMIT_MB` | `256` | Guest sandbox memory. |
| `SANDBOX_GUEST_CPU_LIMIT` | `0.5` | Guest sandbox CPUs. |
| `SANDBOX_GUEST_PIDS_LIMIT` | `64` | Guest sandbox processes/threads. |
| `SANDBOX_GUEST_MOVE_TIMEOUT` | `15` | Seconds per move. |
| `SANDBOX_GUEST_VERIFY_TIMEOUT` | `120` | Seconds for a model check. |
| `SANDBOX_GUEST_START_TIMEOUT` | `60` | Seconds for a cold start. |
| `SANDBOX_GUEST_MAX_WORKERS` | `2` | Maximum warm guest containers (a busy pool refuses new guests instead of evicting registered users). |
| `SANDBOX_GUEST_FSIZE_MB` | `8` | Largest file a guest process may write. |
| `SANDBOX_DOCKER_RUNTIME` | empty | Optional OCI runtime for all sandbox containers, for example `runsc` (gVisor) for stronger isolation. |

Registered-account sandbox limits are unchanged.

### 15.3 Client IP behind a proxy

The per-IP rate limit needs the real client address. Set `TRUSTED_PROXY_HEADER` only to a header that **your proxy always overwrites**:

* nginx as the only proxy (the config in section 10): `TRUSTED_PROXY_HEADER=X-Forwarded-For`
* Cloudflare in front of nginx: `TRUSTED_PROXY_HEADER=CF-Connecting-IP` and allow only Cloudflare addresses to reach the server.

If the app is reachable without the proxy, leave it empty; otherwise a client could forge the header.

### 15.4 Celery worker and beat

Guest work uses the queue named by `GUEST_CELERY_QUEUE`. The default (`celery`) needs no change. To keep guest model checks away from registered traffic, set `GUEST_CELERY_QUEUE=guest` and run a second worker, and make the main worker listen to both queues:

```ini
[program:agladiator-celery]
command=/opt/agladiator/venv/bin/celery -A agladiator worker -l info -c 2 -Q celery,guest

[program:agladiator-celery-guest]
command=/opt/agladiator/venv/bin/celery -A agladiator worker -l info -c 1 -Q guest -n guest@%%h
directory=/opt/agladiator/app
user=agladiator
autostart=true
autorestart=true
```

The beat schedule adds `purge-expired-guests` (hourly) and `cleanup-idle-guest-snapshots` (every 10 minutes). Restart `agladiator-celerybeat` after deploying so the database scheduler picks them up.

### 15.5 Enable, check, roll back

```bash
# Dry run: lists what the purge would delete, deletes nothing
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py purge_guests --dry-run

# Enable
echo 'GUEST_MODE_ENABLED=True' | sudo -u agladiator tee -a /opt/agladiator/app/.env
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py check
sudo supervisorctl restart agladiator-gunicorn agladiator-daphne agladiator-celery agladiator-celerybeat
```

To switch off, set `GUEST_MODE_ENABLED=False` and restart; the guest sign-in blocks and guest pages disappear. The purge and snapshot-cleanup tasks keep running regardless of the flag, so existing guest accounts are still deleted once they have been idle for `GUEST_TTL_DAYS`; `manage.py purge_guests` runs the same purge on demand (it only touches expired guests). To remove the schema as well, wait until no guest accounts remain (check with `purge_guests --dry-run`), then reverse the migrations (this drops the guest columns):

```bash
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py migrate games 0011
sudo -u agladiator /opt/agladiator/venv/bin/python manage.py migrate users 0041
```
