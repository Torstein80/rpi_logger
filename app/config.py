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
DEFAULT_FALLBACK_TEMPERATURE_C = float(os.getenv("DEFAULT_FALLBACK_TEMPERATURE_C", "85.0"))
MAX_SENSOR_SLOTS = int(os.getenv("MAX_SENSOR_SLOTS", "4"))
ALLOWED_EXPORTS = {"csv", "txt", "xlsx"}

NETWORK_MANAGER_ENABLED = os.getenv("NETWORK_MANAGER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
NETWORK_AP_INTERFACE = os.getenv("NETWORK_AP_INTERFACE", "wlan0")
NETWORK_AP_CONNECTION_NAME = os.getenv("NETWORK_AP_CONNECTION_NAME", "haga-fallback-ap")
NETWORK_WIRED_INTERFACE = os.getenv("NETWORK_WIRED_INTERFACE", "eth0")
NETWORK_WIRED_CONNECTION_NAME = os.getenv("NETWORK_WIRED_CONNECTION_NAME", "haga-eth0")

STORAGE_HEARTBEAT_PATH = os.getenv("STORAGE_HEARTBEAT_PATH", "/data/heartbeat.json")
STORAGE_PROBE_INTERVAL_SECONDS = int(os.getenv("STORAGE_PROBE_INTERVAL_SECONDS", "15"))
STORAGE_LOW_FREE_GB_WARNING = float(os.getenv("STORAGE_LOW_FREE_GB_WARNING", "2.0"))
STORAGE_WATCHDOG_ENABLED = os.getenv("STORAGE_WATCHDOG_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
STORAGE_WATCHDOG_REBOOT_AFTER_SECONDS = int(os.getenv("STORAGE_WATCHDOG_REBOOT_AFTER_SECONDS", "600"))
