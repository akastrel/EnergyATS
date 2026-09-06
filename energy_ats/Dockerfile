FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

# aiohttp используется для WebSocket Home Assistant и небольшой REST-публикации
# диагностического sensor.energy_ats_status. Всё остальное в ATS core — только
# стандартная библиотека Python.
RUN apk add --no-cache \
    python3 \
    py3-aiohttp

WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
