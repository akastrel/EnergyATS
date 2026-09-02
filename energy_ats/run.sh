#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Запуск Energy ATS..."
exec python3 -u /app/main_v124.py
