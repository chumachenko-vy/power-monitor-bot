# 🔌 Power Monitor Bot

Telegram бот для моніторингу електроенергії вдома. Сповіщає про відключення та увімкнення світла, показує графіки ДТЕК, веде статистику.

## Як працює

```
ESP32 (вдома)                GCE VM (хмара)              Telegram
┌────────────┐  heartbeat   ┌──────────────┐            ┌──────────┐
│ живиться   │──кожні 30с──▶│ HTTP сервер  │──сповіщ──▶ │  ваш чат │
│ від мережі │              │ + Telegram   │            │          │
└────────────┘              │ + YASNO API  │            └──────────┘
                            └──────────────┘
```

- Світло зникло → ESP32 вимкнувся → heartbeat пропав → бот: **🔴 Світло ВИМКНЕНО!**
- Світло повернулось → ESP32 стартує → heartbeat прийшов → бот: **💡 Світло УВІМКНЕНО!**
- Бот порівнює з графіком ДТЕК і повідомляє: планове чи позапланове відключення
- За 30 хвилин до планового відключення бот попереджає

## Можливості

- 🔔 Сповіщення про вимкнення/увімкнення світла
- 📅 Графік відключень ДТЕК (YASNO API) на сьогодні та завтра
- ⏰ Попередження за 30 хв до планового відключення
- 📋 Мітка "планове" / "позапланове" на кожному відключенні
- 💡 При вимкненні — коли буде світло за графіком
- 🔴 При увімкненні — коли наступне відключення
- 📊 Статистика: скільки було/не було світла (години, відсотки)
- 🔌 Підтримка Tuya Smart Plug (опціонально, замість або разом з ESP32)

## Швидкий старт

### 1. Створіть Telegram бота

Напишіть [@BotFather](https://t.me/BotFather) → `/newbot` → збережіть токен.

### 2. Підніміть сервер

Підійде будь-який Linux-сервер із зовнішнім IP та доступом до інтернету:

| Варіант | Примітка |
|---------|----------|
| Google Cloud (GCE) | `e2-micro` — безкоштовний free tier |
| AWS (EC2) | `t2.micro` / `t3.micro` — free tier 12 міс |
| Azure | `B1s` — free tier 12 міс |
| Hetzner | від €3.79/міс, найдешевший VPS |
| DigitalOcean | від $4/міс |
| Oracle Cloud | `VM.Standard.E2.1.Micro` — безкоштовно назавжди |
| Домашній сервер | Raspberry Pi, старий ПК, віртуалка |

**Вимоги:** Ubuntu/Debian, Python 3.10+, відкритий порт `8080` (для ESP32 heartbeat).

> ⚠️ Бот має працювати **НЕ вдома** (або на ДБЖ), бо повинен бути доступний коли вдома немає світла.

### 3. Розгорніть бота на сервері

Підключіться до сервера по SSH та виконайте:

```bash
# Встановіть залежності
sudo apt update && sudo apt install -y python3-pip git
sudo pip3 install python-telegram-bot aiohttp --break-system-packages

# Клонуйте репозиторій
sudo git clone https://github.com/chumachenko-vy/power-monitor-bot.git /opt/power_monitor_bot

# Створіть конфіг
cd /opt/power_monitor_bot
sudo cp config.example.py config.py
sudo nano config.py    # впишіть BOT_TOKEN та HEARTBEAT_SECRET

# Створіть користувача та запустіть
sudo useradd -m -s /bin/bash powerbot
sudo chown -R powerbot:powerbot /opt/power_monitor_bot
sudo cp powerbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable powerbot
sudo systemctl start powerbot
```

**Відкрийте порт 8080** у фаєрволі вашого хостингу (Security Groups в AWS, Firewall Rules в GCE, тощо) для вхідного TCP трафіку.

### 4. Прошийте ESP32

1. Встановіть [Arduino IDE](https://www.arduino.cc/en/software)
2. Додайте підтримку ESP32: **Файл → Параметри** → Additional boards URL:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. **Інструменти → Плата → Менеджер плат** → встановіть **ESP32 by Espressif**
4. Відкрийте `esp32/esp32_heartbeat.ino`
5. Заповніть Wi-Fi та IP сервера
6. **Інструменти → Плата** → ESP32 Dev Module
7. **Інструменти → Порт** → ваш COM-порт
8. **Завантажити (→)**
9. Підключіть ESP32 через USB-зарядку в розетку **без ДБЖ**

### 5. Налаштуйте бота

Напишіть боту в Telegram:
- `/start` — підписатись на сповіщення
- `/setgroup 27.1` — встановити групу ДТЕК (дізнатись: [@DTEK_KEM_bot](https://t.me/DTEK_KEM_bot))
- `/schedule` — перевірити графік

## Команди бота

| Команда | Опис |
|---------|------|
| `/start` | Меню та підписка |
| `/setgroup <група>` | Встановити групу ДТЕК |
| `/schedule` | Графік на сьогодні |
| `/tomorrow` | Графік на завтра |
| `/status` | Поточний стан |
| `/stats` | Загальна статистика |
| `/today` | Статистика за 24 години |
| `/week` | Статистика за тиждень |
| `/history` | Останні 15 подій |
| `/power` | Споживання (Tuya) |

## Приклади повідомлень

```
🔴 Світло ВИМКНЕНО!
🕐 26.10.2026 18:00:12
⏱ Світло було: 3 год 45 хв
📋 Планове відключення
💡 За графіком увімкнення о 21:30

💡 Світло УВІМКНЕНО!
🕐 26.10.2026 21:31:05
⏱ Світла не було: 3 год 31 хв
📋 Планове відключення
🔴 Наступне відключення за графіком: завтра о 11:30
```

## Структура

```
├── bot.py                  # Telegram бот + HTTP сервер + YASNO API
├── config.example.py       # Приклад конфігурації (скопіювати в config.py)
├── tuya_client.py          # Tuya Cloud API клієнт (опціонально)
├── requirements.txt        # Python залежності
├── powerbot.service        # Systemd сервіс
└── esp32/
    └── esp32_heartbeat.ino # Прошивка ESP32
```

## Tuya Smart Plug (опціонально)

Замість або разом з ESP32 можна використовувати розумну Wi-Fi розетку Tuya з ватметром. Бот перевіряє онлайн/офлайн статус розетки через Tuya Cloud API, а також показує поточне споживання у ватах.

Для налаштування:
1. Зареєструйтесь на [iot.tuya.com](https://iot.tuya.com)
2. Створіть Cloud Project, отримайте Access ID та Secret
3. Прив'яжіть розетку через додаток SmartLife/Tuya
4. Заповніть `TUYA_*` параметри в `config.py`, встановіть `TUYA_ENABLED = True`

## Ліцензія

MIT
