ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

WORKDIR /usr/src/app

RUN apk add --no-cache \
  git \
  ca-certificates \
  build-base \
  python3-dev

RUN pip3 install --no-cache-dir uv

COPY app /usr/src/app
COPY rootfs /

RUN chmod a+x /etc/services.d/ups2mqtt/run /etc/cont-init.d/10-ups2mqtt-options

CMD ["/init"]
