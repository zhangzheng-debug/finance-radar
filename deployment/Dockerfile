FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 radar && useradd --uid 10001 --gid radar --create-home radar

COPY requirements.lock /app/requirements.lock
RUN pip install --upgrade pip && pip install --require-hashes -r /app/requirements.lock

COPY app /app/app
COPY scripts /app/scripts
COPY replay /app/replay
COPY config /app/config

RUN mkdir -p /app/data /app/artifacts /app/reports && chown -R radar:radar /app
USER radar

EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
