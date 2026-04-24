# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

FROM python:3.13-slim

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential python3-dev && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

COPY ups2mqtt/rootfs/usr/src/app /usr/src/app
RUN uv sync --frozen --no-dev || uv sync --no-dev

CMD ["uv", "run", "python", "-m", "ups2mqtt.main"]
