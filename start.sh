#!/bin/bash
set -e

echo "Running database migrations..."
flask db upgrade

echo "Starting gunicorn on port $PORT..."
exec gunicorn wsgi:application \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --timeout 300 \
    --access-logfile - \
    --error-logfile -
