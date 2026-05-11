<div align="center">

# Contributing to Artificial Gladiator League

**Thank you for helping build the platform. Every contribution — big or small — matters.**

[![GitHub](https://img.shields.io/badge/GitHub-Artificial--Gladiator--League-181717?logo=github)](https://github.com/Artificial-Gladiator-League/Artificial-Gladiator-League)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Contact](https://img.shields.io/badge/Contact-contact%40agladiator.com-blue)](mailto:contact@agladiator.com)

</div>

---

## Table of Contents

1. [Welcome](#welcome)
2. [Code of Conduct](#code-of-conduct)
3. [Getting Started](#getting-started)
4. [Development Setup](#development-setup)
5. [Project Layout](#project-layout)
6. [Making Changes](#making-changes)
7. [Code Style](#code-style)
8. [Testing](#testing)
9. [Submitting a Pull Request](#submitting-a-pull-request)
10. [Reporting Issues](#reporting-issues)
11. [Security Vulnerabilities](#security-vulnerabilities)
12. [Credits](#credits)

---

## Welcome

Whether you are fixing a typo, squashing a bug, or proposing an entirely new game engine, you are welcome here. This guide walks you through everything you need to go from zero to merged PR.

**Good first issues** are labelled [`good first issue`](https://github.com/Artificial-Gladiator-League/Artificial-Gladiator-League/issues?q=label%3A%22good+first+issue%22) on GitHub — start there if you are new to the codebase.

> For larger features or architectural changes, **open an issue first** to discuss the approach before writing code. This saves everyone time.

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). By participating you agree to uphold a respectful, inclusive environment for all contributors.

Unacceptable behaviour can be reported to **contact@agladiator.com**. All reports are reviewed promptly and kept confidential.

---

## Getting Started

### Prerequisites

| Tool    | Version     | Purpose                              |
|---------|-------------|--------------------------------------|
| Python  | 3.11+       | Runtime                              |
| MySQL   | 8.0+        | Primary database                     |
| Redis   | 7.0+        | Channel layers & Celery broker       |
| Docker  | 24.0+       | AI sandbox inference                 |
| Git     | Any recent  | Version control                      |

> **No Node.js required** — Tailwind CSS is loaded via CDN.

### Fork & Clone

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/Artificial-Gladiator-League.git
cd Artificial-Gladiator-League/agladiator   # directory containing manage.py

# 2. Add the upstream remote so you can pull future changes
git remote add upstream https://github.com/Artificial-Gladiator-League/Artificial-Gladiator-League.git
```

---

## Development Setup

### 1. Virtual Environment & Dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. MySQL Database

```sql
CREATE DATABASE agladiator
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'gladiator'@'localhost' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON agladiator.* TO 'gladiator'@'localhost';
FLUSH PRIVILEGES;
```

### 3. Environment Variables

Create a `.env` file next to `manage.py`:

```bash
DJANGO_SECRET_KEY="your-dev-secret-key"
DJANGO_DEBUG="True"
DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
DB_NAME="agladiator"
DB_USER="gladiator"
DB_PASSWORD="your-password"
DB_HOST="127.0.0.1"
DB_PORT="3306"
REDIS_URL="redis://127.0.0.1:6379/0"
HF_PLATFORM_TOKEN=""        # optional for local dev
HF_WEBHOOK_SECRET=""
HF_OAUTH_CLIENT_ID=""
HF_OAUTH_CLIENT_SECRET=""
PREWARM_MODELS=false        # skip model downloads in dev
```

> `PREWARM_MODELS=false` prevents the server from downloading Hugging Face models on startup — highly recommended for local development.

### 4. Migrations & Static Files

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

### 5. Start the Development Server

```bash
# Daphne — full WebSocket support (recommended)
daphne -b 0.0.0.0 -p 8000 agladiator.asgi:application

# Django dev server — no WebSockets, but fine for non-realtime work
python manage.py runserver
```

> If Redis is not running locally, the app automatically falls back to in-memory channel layers and eager Celery execution — no extra config needed for basic development.

### 6. Optional: Celery Workers

Required only when working on tournament scheduling, stale-game cleanup, or other background tasks:

```bash
# Worker
celery -A agladiator worker -l info

# Beat scheduler (periodic tasks)
celery -A agladiator beat -l info
```

---

## Project Layout

| App              | Responsibility                                                  |
|------------------|-----------------------------------------------------------------|
| `apps/core/`     | Home, leaderboard, about, static pages                          |
| `apps/users/`    | Auth, profiles, GDPR, Hugging Face OAuth, model lifecycle       |
| `apps/games/`    | Chess & Breakthrough engines, lobbies, WebSocket consumers      |
| `apps/tournaments/` | Brackets, scheduling, Celery tasks, management commands      |
| `apps/forum/`    | Community discussion board                                      |
| `apps/chat/`     | Real-time direct messaging & notifications                      |

---

## Making Changes

### Branch Naming

Branch off `main` using a short, descriptive name:

```bash
git checkout main
git pull upstream main
git checkout -b fix/tournament-bracket-seeding
```

| Prefix      | Use for                                      |
|-------------|----------------------------------------------|
| `feat/`     | New features                                 |
| `fix/`      | Bug fixes                                    |
| `docs/`     | Documentation only                           |
| `refactor/` | Code restructuring with no behaviour change  |
| `test/`     | Adding or updating tests                     |
| `chore/`    | Dependency bumps, CI config, build tooling   |

### Commit Style

Write commits in the **imperative mood**, present tense:

```
feat(games): add time-control support to Breakthrough engine
fix(tournaments): correct Elo delta when player forfeits
docs: update sandbox security table in README
test(users): add GDPR export endpoint coverage
```

- **Keep commits atomic** — one logical change per commit.
- **Reference issues** where relevant: `fix(chat): resolve WS disconnect loop (#42)`.

---

## Code Style

### Python

- Follow **PEP 8** — use `flake8` or `ruff` for linting.
- Maximum line length: **119 characters** (matches the project's existing convention).
- Format code with **`black`** before committing:

```bash
pip install black ruff
black .
ruff check .
```

### Django Best Practices

- Place business logic in **service layers or model methods**, not views.
- Use **class-based views** for CRUD; function-based views are fine for simple endpoints.
- Prefer **`select_related` / `prefetch_related`** to avoid N+1 queries.
- Never store secrets or credentials in source code — use environment variables.
- New models require a **migration** (`python manage.py makemigrations <app>`).

### Templates

- Use the existing **Tailwind dark/light theme** utilities — do not introduce inline styles.
- Keep template logic minimal; move complexity to views or template tags.

### Security

> ⚠️ User-submitted AI code must **never** execute inside the main Django process. All inference must go through the Docker sandbox (`apps/games/local_sandbox_inference.py`).

- Validate all user inputs at the boundary (forms, serializers, consumers).
- Use Django's built-in CSRF, XSS, and SQL-injection protections — do not bypass them.
- Follow the [OWASP Top 10](https://owasp.org/Top10/) guidelines.

---

## Testing

### Running the Test Suite

```bash
# Django test runner (all tests)
python manage.py test

# Run tests for a specific app
python manage.py test apps.tournaments

# With pytest-django (if installed)
pytest
```

### Writing Tests

- Place tests in `apps/<app>/tests/` or a `tests.py` file inside the app.
- Use `pytest` with the `pytest-django` plugin for new test files.
- Aim for **at least one test per new view, model method, or Celery task**.
- Use `django.test.TestCase` for database-dependent tests and `pytest.mark.django_db` with `pytest-django`.

```python
# Example — pytest-django style
import pytest
from django.urls import reverse

@pytest.mark.django_db
def test_leaderboard_returns_200(client):
    response = client.get(reverse("core:leaderboard"))
    assert response.status_code == 200
```

- Mock external services (Hugging Face API, Docker daemon) in tests — do not hit live endpoints.
- All existing tests must pass before your PR can be merged.

---

## Submitting a Pull Request

### Checklist

Before opening your PR, confirm:

- [ ] Branch is up to date with `upstream/main`
- [ ] `black .` and `ruff check .` pass with no errors
- [ ] All existing tests pass (`python manage.py test`)
- [ ] New behaviour is covered by tests
- [ ] Migrations are included for any model changes
- [ ] No secrets, credentials, or personal data committed
- [ ] PR description explains **what** changed and **why**

### Updating Your Branch

```bash
git fetch upstream
git rebase upstream/main
```

### PR Title Format

Use the same imperative-mood, scoped format as commits:

```
feat(games): add draw-by-repetition detection to Chess engine
fix(users): prevent duplicate HF model links on profile save
docs(contributing): add pytest examples to Testing section
```

### What to Expect

| Stage              | Typical Turnaround  |
|--------------------|---------------------|
| Initial review     | 2–5 business days   |
| Follow-up feedback | 1–3 business days   |
| Merge (approved)   | Same day            |

A maintainer will review your PR and may request changes. Please respond to review comments — PRs with no activity for 30 days may be closed.

> **Keep PRs small and focused.** A PR that changes one thing is easier to review, faster to merge, and simpler to revert if needed.

---

## Reporting Issues

### Before Opening an Issue

1. Search [existing issues](https://github.com/Artificial-Gladiator-League/Artificial-Gladiator-League/issues) to avoid duplicates.
2. Check that you are running against the latest `main`.

### Issue Template

When filing a bug, include:

```
**Describe the bug**
A clear description of what went wrong.

**Steps to reproduce**
1. Go to '...'
2. Click on '...'
3. See error

**Expected behaviour**
What you expected to happen.

**Actual behaviour**
What actually happened (include full tracebacks).

**Environment**
- OS: [e.g. Ubuntu 22.04]
- Python: [e.g. 3.11.8]
- Django: [e.g. 5.1.2]
- Browser (if UI issue): [e.g. Firefox 125]

**Additional context**
Logs, screenshots, or anything else that might help.
```

### Feature Requests

Open an issue with the `enhancement` label. Describe the problem you are trying to solve — not just the solution — so we can explore the best approach together.

---

## Security Vulnerabilities

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a vulnerability, email **contact@agladiator.com** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (if safe to share)
- Any suggested mitigations

You will receive an acknowledgement within **48 hours** and a status update within **7 days**. We follow responsible disclosure — please give us reasonable time to address the issue before publishing details publicly.

Areas of particular sensitivity:

| Area                        | Risk                                              |
|-----------------------------|---------------------------------------------------|
| Docker sandbox (`apps/games/local_sandbox_inference.py`) | Container escape, code execution |
| Hugging Face model pipeline (`apps/users/model_lifecycle.py`) | Malicious model execution |
| Anti-cheat / IP logging     | Privacy and data-protection implications          |
| Authentication & sessions   | Account takeover                                  |
| GDPR endpoints              | Unauthorised data access or deletion              |

---

## Credits

All contributors are recognised in the project. Once your first PR is merged, you will be added to the contributors list.

A huge thank you to everyone who has filed bugs, improved documentation, written tests, or shipped features. You make this project better for everyone in the community.

---

<div align="center">

Questions? Reach us at **contact@agladiator.com** or open a [GitHub Discussion](https://github.com/Artificial-Gladiator-League/Artificial-Gladiator-League/discussions).

© Artificial Gladiator League — Licensed under the [MIT License](LICENSE)

</div>
