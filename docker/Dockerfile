FROM python:3.12-slim

# iproute2 provides tc, used by the netem entrypoint to inject the remote arm's delay.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bench ./bench
COPY docker/netem-entrypoint.sh /usr/local/bin/netem-entrypoint.sh
RUN chmod +x /usr/local/bin/netem-entrypoint.sh

ENV PYTHONUNBUFFERED=1
