# Railway image for HireAssist backend.
# Based on python:3.11-slim + Playwright Chromium so the daily refresh cron can
# run career-page scraping (agent_scrape.py) on Railway itself -- no laptop.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install Python deps, then the Chromium browser + its OS libraries.
# `--with-deps` apt-installs the system libraries Chromium needs.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

# Railway provides $PORT at runtime.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
