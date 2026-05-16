# syntax=docker/dockerfile:1.7
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.22

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

ENV UV_LINK_MODE=copy

WORKDIR /usr/src/app

RUN apk add --no-cache \
  git \
  ca-certificates \
  build-base \
  python3-dev

RUN pip3 install --no-cache-dir uv

COPY app/pyproject.toml app/uv.lock /usr/src/app/
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --frozen --no-dev || uv sync --no-dev

COPY app /usr/src/app
COPY rootfs /

RUN chmod a+x /etc/services.d/ups2mqtt/run /etc/cont-init.d/10-ups2mqtt-options
