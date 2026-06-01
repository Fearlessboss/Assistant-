FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libssl-dev \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# session + settings files will be created at runtime; mount a volume to persist them
VOLUME ["/app/data"]

# default command
CMD ["python", "main.py"]
