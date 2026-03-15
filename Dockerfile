FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl gnupg unzip git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Pre-install Baileys (official npm) and common WhatsApp bot packages at build time
RUN mkdir -p /preinstalled && cd /preinstalled && \
    echo '{"name":"preinstall","version":"1.0.0","dependencies":{"@whiskeysockets/baileys":"^6.7.16","@adiwajshing/keyed-db":"^0.2.4","@hapi/boom":"^10.0.0","qrcode-terminal":"^0.12.0","pino":"^8.0.0","jimp":"^0.22.12","node-fetch":"^2.7.0","axios":"^1.7.9","chalk":"^4.1.2","colors":"latest","moment-timezone":"^0.5.34","dotenv":"^16.0.0","ms":"^2.1.3","yt-search":"^2.10.4","readline":"^1.3.0","fs-extra":"^11.2.0"}}' > package.json && \
    npm install --legacy-peer-deps

ENV NODE_PATH=/preinstalled/node_modules

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p uploads bots

EXPOSE 5000
CMD gunicorn app:app --worker-class gevent --workers 1 --bind 0.0.0.0:$PORT --timeout 300
