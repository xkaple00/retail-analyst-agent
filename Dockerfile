FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml .
COPY src ./src
COPY prompts ./prompts
COPY config ./config
COPY golden ./golden
COPY scripts ./scripts
COPY evals ./evals
RUN pip install --no-cache-dir --no-deps -e .

# Data (sqlite, traces, candidate trios) is kept on a volume, see docker-compose.yml
ENV ANALYST_DATA_DIR=/app/data
VOLUME ["/app/data"]

ENTRYPOINT ["analyst"]
CMD ["chat", "--user", "alice"]
