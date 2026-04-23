import os

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/data/logger.db")
EXPORT_DIR = os.getenv("EXPORT_DIR", "/data/exports")
W1_BASE_PATH = os.getenv("W1_BASE_PATH", "/sys/bus/w1/devices")
SENSOR_LABELS = os.getenv("SENSOR_LABELS", "")
HTTPS_CERTFILE = os.getenv("HTTPS_CERTFILE", "").strip()
HTTPS_KEYFILE = os.getenv("HTTPS_KEYFILE", "").strip()
DEFAULT_POLL_SECONDS = int(os.getenv("DEFAULT_POLL_SECONDS", "10"))
DEFAULT_INTERVAL_SECONDS = int(os.getenv("DEFAULT_INTERVAL_SECONDS", "10"))
DEFAULT_TIMEZONE_OFFSET = os.getenv("DEFAULT_TIMEZONE_OFFSET", "+02:00")
ALLOWED_EXPORTS = {"csv", "txt", "xlsx"}
