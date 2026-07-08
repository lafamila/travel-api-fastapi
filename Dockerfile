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

# 전용 health 라우트가 없어 FastAPI 기본 /docs 로 liveness 확인 (라우트 추가 시 /api/health 로 교체)
# start-period 는 Playwright Chromium 기동 시간을 고려해 여유 있게 둔다
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/docs', timeout=4)" || exit 1

CMD ["uvicorn", "src.__main__:app", "--host", "0.0.0.0", "--port", "8010"]
