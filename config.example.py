# Copy this file to config.py and fill in your values:
#   cp config.example.py config.py

# ─── Telegram ─────────────────────────────────────────────────
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"       # get from @BotFather

# ─── Timezone (Kyiv: winter=2, summer=3) ──────────────────────
TIMEZONE_OFFSET = 2

# ─── Detection sources (enable one or both) ──────────────────

# ESP32 heartbeat
ESP_ENABLED = True
HEARTBEAT_PORT = 8080
HEARTBEAT_TIMEOUT = 90                   # seconds without heartbeat = power off
HEARTBEAT_SECRET = "change_me"           # must match SECRET_KEY in ESP32 firmware

# Tuya Smart Plug (enable when you have one)
TUYA_ENABLED = False
TUYA_ACCESS_ID = ""                      # from iot.tuya.com
TUYA_ACCESS_SECRET = ""
TUYA_DEVICE_ID = ""                      # device ID from Tuya app
TUYA_REGION = "eu"                       # eu / us / cn / in
TUYA_POLL_INTERVAL = 30                  # seconds between checks

# ─── YASNO API (DTEK outage schedule) ─────────────────────────
YASNO_ENABLED = True
YASNO_API_URL = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages"
YASNO_REGION = "kiev"                    # kiev or dnipro
SCHEDULE_CHECK_INTERVAL = 900            # seconds (15 min)
NOTIFY_BEFORE_MINUTES = 30               # warn N minutes before outage
