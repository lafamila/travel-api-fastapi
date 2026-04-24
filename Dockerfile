FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul \
    PYTHONPATH=/app/src \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System deps for Playwright Chromium + fonts
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl tzdata \
       libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
       libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
       libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
       libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
       fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

RUN playwright install chromium

COPY src/ ./src/
COPY tests/ ./tests/

EXPOSE 8010

CMD ["uvicorn", "src.__main__:app", "--host", "0.0.0.0", "--port", "8010"]
