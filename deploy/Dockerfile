# Read-only research/PAPER API. The SPA is served by the edge nginx, not here.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY trader.py config.yaml config.tse.yaml universe.json universe_tse.json ./
RUN mkdir -p state

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "cat.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
