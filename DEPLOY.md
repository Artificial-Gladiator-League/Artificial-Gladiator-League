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
