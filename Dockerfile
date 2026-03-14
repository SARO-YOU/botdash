FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl gnupg unzip git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p uploads bots

EXPOSE 5000
CMD gunicorn app:app --worker-class gevent --workers 1 --bind 0.0.0.0:$PORT --timeout 120
