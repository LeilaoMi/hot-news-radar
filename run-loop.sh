#!/usr/bin/env bash
# TrendRadar hourly loop — runs the crawler every hour, forever
set -u
cd "$(dirname "$0")"
source .env

LOG=/dev/shm/trendradar-crawler.log
echo "[$(date -Iseconds)] trendradar-crawler loop started" >> $LOG

while true; do
  echo "[$(date -Iseconds)] === running trendradar ===" >> $LOG
  uv run python -m trendradar >> $LOG 2>&1 || echo "[$(date -Iseconds)] run failed (rc=$?)" >> $LOG
  echo "[$(date -Iseconds)] === sleeping 3600s ===" >> $LOG
  sleep 3600
done
