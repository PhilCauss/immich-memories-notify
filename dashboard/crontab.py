"""Crontab generation and scheduler reload for embedded crond."""

import os
import subprocess

import yaml

ENV_FILE = "/app/.cron.env"


def dump_env():
    """Save current environment to a file that cron jobs can source."""
    with open(ENV_FILE, "w") as f:
        for key, value in os.environ.items():
            if key in ("PWD", "SHLVL", "_", "HOSTNAME"):
                continue
            value = value.replace("'", "'\\''")
            f.write(f"export {key}='{value}'\n")


def generate_crontab(config_path: str = "/app/config.yaml"):
    """Generate /etc/crontabs/root from config.yaml notification windows."""
    dump_env()
    with open(config_path) as f:
        config = yaml.safe_load(f)
    windows = config.get("settings", {}).get("notification_windows", [])
    prefix = f". {ENV_FILE} && cd /app"
    with open("/etc/crontabs/root", "w") as f:
        for i, w in enumerate(windows, 1):
            h, m = w.get("start", "08:00").split(":")
            f.write(f"{int(m)} {int(h)} * * * {prefix} && python -m notify --slot {i} >> /proc/1/fd/1 2>&1\n")
        f.write(f"0 6 * * * {prefix} && python -m notify --check-updates >> /proc/1/fd/1 2>&1\n")


def reload_scheduler(config_path: str = "/app/config.yaml"):
    """Regenerate crontab and restart crond."""
    generate_crontab(config_path)
    subprocess.run(["killall", "crond"], check=False)
    subprocess.run(["crond", "-l", "2"], check=False)
