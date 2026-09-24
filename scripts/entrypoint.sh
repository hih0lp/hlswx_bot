#!/usr/bin/env bash
set -e
echo "Waiting for PostgreSQL..."
python -m app.core.seed
echo "Starting HWLS app..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${WEBHOOK_PORT:-8082}"
