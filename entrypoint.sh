#!/bin/sh

# Default to /app if a dedicated config volume directory isn't specified
CONFIG_DIR="${CONFIG_DIR:-/app}"

# If an external config volume is used, make sure files exist and are linked
if [ "$CONFIG_DIR" != "/app" ]; then
    mkdir -p "$CONFIG_DIR"
    
    # Seed the configuration file if the persistent volume is empty
    if [ ! -f "$CONFIG_DIR/config.yaml" ] && [ -f "/app/config.template.yaml" ]; then
        cp /app/config.template.yaml "$CONFIG_DIR/config.yaml"
    fi
    
    # Touch .env if missing to avoid link issues
    if [ ! -f "$CONFIG_DIR/.env" ]; then
        touch "$CONFIG_DIR/.env"
    fi

    # Create symlinks so the application code can read/write directly at /app/
    ln -sf "$CONFIG_DIR/config.yaml" /app/config.yaml
    ln -sf "$CONFIG_DIR/.env" /app/.env
fi


# Stop old standalone scheduler if still running (v2.4.x migration)
if docker inspect --format '{{.State.Running}}' immich-memories-scheduler 2>/dev/null | grep -q true; then
    echo "[migration] Stopping old scheduler container (now embedded in dashboard)..."
    docker stop immich-memories-scheduler 2>/dev/null || true
fi

# Generate crontab from config
python -c "from dashboard.crontab import generate_crontab; generate_crontab()"

# Start crond in background
crond -l 2

# Forward signals to uvicorn for graceful shutdown
trap 'kill $UVICORN_PID; wait $UVICORN_PID' TERM INT

# Start uvicorn in background so shell stays PID 1 (reaps zombies)
uvicorn dashboard.main:app --host 0.0.0.0 --port 5000 &
UVICORN_PID=$!

# Wait for uvicorn — if it dies, container exits
wait $UVICORN_PID
