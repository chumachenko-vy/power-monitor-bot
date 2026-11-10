"""
🔌 Telegram бот моніторингу електроенергії v2.2
=================================================
Джерела: ESP32 heartbeat | Tuya Smart Plug
Графік: Baskerville42/outage-data-ua (primary) + YASNO API (fallback)
"""

import asyncio
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

import config
if config.TUYA_ENABLED:
    from tuya_client import TuyaCloud

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_FILE = Path("power_history.json")

state = {
    "power_on": None,
    "last_change": None,
    "last_heartbeat": None,
    "last_tuya_check": None,
    "tuya_power_w": None,
    "tuya_voltage": None,
    "history": [],
    "subscribers": {},
    # Schedule: parsed into unified format
    # {group: {day_ts: [{start_min, end_min, type}]}}
    "schedule": {},
    "schedule_source": None,
    "schedule_updated": None,
    "schedule_today_ts": None,
    "notified_slots": set(),
}

telegram_app: Application = None
tuya: "TuyaCloud | None" = None

# ═══════════════════════════════════════════════════════════════
#  Утиліти
# ═══════════════════════════════════════════════════════════════

def now() -> datetime:
    return datetime.utcnow() + timedelta(hours=config.TIMEZONE_OFFSET)

def fmt_dur(s: int) -> str:
    if s < 60: return f"{s} сек"
    m = s // 60
    if m < 60: return f"{m} хв {s%60} сек"
    h, m2 = m // 60, m % 60
    if h < 24: return f"{h} год {m2} хв"
    return f"{h//24} д {h%24} год {m2} хв"

def fmt_time(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M:%S")

def mins_to_hm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"

def now_minutes() -> int:
    n = now()
    return n.hour * 60 + n.minute


# ─── Persistence ─────────────────────────────────────────────

def save_data():
    d = {
        "history": state["history"][-1000:],
        "subscribers": state["subscribers"],
        "power_on": state["power_on"],
        "last_change": state["last_change"],
        "last_heartbeat": state["last_heartbeat"],
    }
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

def load_data():
    if not DATA_FILE.exists():
        return
    try:
        d = json.loads(DATA_FILE.read_text())
        state["history"] = d.get("history", [])
        state["subscribers"] = d.get("subscribers", {})
        if isinstance(state["subscribers"], list):
            state["subscribers"] = {str(c): {"group": None} for c in state["subscribers"]}
        state["power_on"] = d.get("power_on")
        state["last_change"] = d.get("last_change")
        state["last_heartbeat"] = d.get("last_heartbeat")
        logger.info(f"Завантажено: {len(state['history'])} подій, {len(state['subscribers'])} підписників")
    except Exception as e:
        logger.error(f"load error: {e}")


# ═══════════════════════════════════════════════════════════════
#  Schedule: Baskerville42 parser (GPV format)
# ═══════════════════════════════════════════════════════════════

def parse_gpv_hours(gpv_data: dict) -> list:
    """
    Парсить годинні слоти GPV формату в список інтервалів відключень.
    Вхід: {"1": "yes", "2": "no", "3": "first", "4": "second", ...}
    Вихід: [{"start": 60, "end": 120, "type": "off"}, ...]

    Години 1-24: година N = (N-1):00 до N:00
    yes = світло є, no = немає всю годину
    first = немає перші 30 хв, second = немає другі 30 хв
    """
    off_intervals = []

    for hour_str in sorted(gpv_data.keys(), key=lambda x: int(x)):
        hour_num = int(hour_str)
        val = gpv_data[hour_str]
        base_min = (hour_num - 1) * 60  # година 1 = 00:00

        if val == "no":
            off_intervals.append({"start": base_min, "end": base_min + 60, "type": "off"})
        elif val == "first":
            off_intervals.append({"start": base_min, "end": base_min + 30, "type": "off"})
        elif val == "second":
            off_intervals.append({"start": base_min + 30, "end": base_min + 60, "type": "off"})
        # "yes" = немає відключення, пропускаємо

    # Зливаємо суміжні інтервали
    merged = []
    for iv in off_intervals:
        if merged and merged[-1]["end"] >= iv["start"]:
            merged[-1]["end"] = max(merged[-1]["end"], iv["end"])
        else:
            merged.append(dict(iv))

    return merged


def build_full_day_slots(off_intervals: list) -> list:
    """
    Будує повний розклад дня (ON + OFF слоти) з інтервалів відключень.
    """
    slots = []
    prev_end = 0

    for iv in off_intervals:
        if iv["start"] > prev_end:
            slots.append({"start": prev_end, "end": iv["start"], "type": "on"})
        slots.append({"start": iv["start"], "end": iv["end"], "type": "off"})
        prev_end = iv["end"]

    if prev_end < 1440:
        slots.append({"start": prev_end, "end": 1440, "type": "on"})

    return slots


async def fetch_baskerville():
    """Завантажує графік з Baskerville42/outage-data-ua."""
    try:
        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as s:
            async with s.get(config.BASKERVILLE_URL) as r:
                if r.status != 200:
                    logger.warning(f"Baskerville: HTTP {r.status}")
                    return False
                data = await r.json(content_type=None)

        fact = data.get("fact", {})
        fact_data = fact.get("data", {})
        today_ts = fact.get("today")

        schedule = {}

        for day_ts_str, day_groups in fact_data.items():
            if not isinstance(day_groups, dict):
                continue
            for gpv_key, hours in day_groups.items():
                if not gpv_key.startswith("GPV"):
                    continue
                group = gpv_key[3:]  # "GPV27.1" -> "27.1"
                if group not in schedule:
                    schedule[group] = {}
                off_intervals = parse_gpv_hours(hours)
                schedule[group][day_ts_str] = {
                    "off_intervals": off_intervals,
                    "full_slots": build_full_day_slots(off_intervals),
                }

        state["schedule"] = schedule
        state["schedule_source"] = "Baskerville42"
        state["schedule_updated"] = now().isoformat()
        state["schedule_today_ts"] = str(today_ts) if today_ts else None

        update_info = fact.get("update", "")
        logger.info(f"📅 Графік оновлено (Baskerville42), {len(schedule)} груп, update: {update_info}")
        return True

    except Exception as e:
        logger.error(f"Baskerville: {e}")
        return False


async def fetch_yasno_fallback():
    """Fallback: YASNO API."""
    try:
        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as s:
            async with s.get(config.YASNO_API_URL) as r:
                if r.status != 200:
                    logger.warning(f"YASNO fallback: HTTP {r.status}")
                    return False
                data = await r.json(content_type=None)

        schedule = {}
        for group, group_data in data.items():
            if not isinstance(group_data, dict):
                continue
            schedule[group] = {}
            for day_key in ("today", "tomorrow"):
                day_data = group_data.get(day_key, {})
                slots = day_data.get("slots", [])
                off_intervals = []
                for sl in slots:
                    if sl.get("type") in ("Definite", "Possible"):
                        off_intervals.append({
                            "start": sl["start"],
                            "end": sl["end"],
                            "type": "off",
                        })
                schedule[group][day_key] = {
                    "off_intervals": off_intervals,
                    "full_slots": slots,  # YASNO вже дає повні слоти
                }

        state["schedule"] = schedule
        state["schedule_source"] = "YASNO"
        state["schedule_updated"] = now().isoformat()
        state["schedule_today_ts"] = "today"
        logger.info(f"📅 Графік оновлено (YASNO fallback)")
        return True

    except Exception as e:
        logger.error(f"YASNO fallback: {e}")
        return False


async def fetch_schedule():
    """Завантажує графік: спочатку Baskerville42, потім YASNO як fallback."""
    ok = await fetch_baskerville()
    if not ok:
        await fetch_yasno_fallback()


# ─── Schedule helpers ────────────────────────────────────────

def get_today_key(group: str) -> str | None:
    """Повертає ключ сьогоднішнього дня для групи."""
    if group not in state["schedule"]:
        return None
    days = state["schedule"][group]

    # Baskerville: ключі — timestamps
    if state["schedule_today_ts"] and state["schedule_today_ts"] in days:
        return state["schedule_today_ts"]

    # YASNO fallback: ключі "today"/"tomorrow"
    if "today" in days:
        return "today"

    # Спробувати перший ключ
    keys = sorted(days.keys())
    return keys[0] if keys else None


def get_tomorrow_key(group: str) -> str | None:
    """Повертає ключ завтрашнього дня для групи."""
    if group not in state["schedule"]:
        return None
    days = state["schedule"][group]

    if "tomorrow" in days:
        return "tomorrow"

    # Baskerville: другий timestamp
    if state["schedule_today_ts"]:
        keys = sorted(days.keys())
        today_idx = keys.index(state["schedule_today_ts"]) if state["schedule_today_ts"] in keys else -1
        if today_idx >= 0 and today_idx + 1 < len(keys):
            return keys[today_idx + 1]

    return None


def get_off_intervals(group: str, day: str = "today") -> list:
    """Повертає інтервали відключень для групи."""
    key = get_today_key(group) if day == "today" else get_tomorrow_key(group)
    if not key or group not in state["schedule"]:
        return []
    return state["schedule"][group].get(key, {}).get("off_intervals", [])


def get_full_slots(group: str, day: str = "today") -> list:
    """Повертає повний розклад (ON+OFF) для групи."""
    key = get_today_key(group) if day == "today" else get_tomorrow_key(group)
    if not key or group not in state["schedule"]:
        return []
    return state["schedule"][group].get(key, {}).get("full_slots", [])


def get_any_group() -> str | None:
    for info in state["subscribers"].values():
        g = info.get("group")
        if g:
            return g
    return None


def find_next_outage_time(group: str) -> str | None:
    """Час наступного відключення (сьогодні або завтра)."""
    if not group:
        return None
    cur_mins = now_minutes()

    for iv in get_off_intervals(group, "today"):
        if iv["start"] > cur_mins:
            return mins_to_hm(iv["start"])

    for iv in get_off_intervals(group, "tomorrow"):
        return "завтра о " + mins_to_hm(iv["start"])

    return None


def find_next_on_time(group: str) -> str | None:
    """Час увімкнення (кінець поточного відключення)."""
    if not group:
        return None
    cur_mins = now_minutes()

    for iv in get_off_intervals(group, "today"):
        if iv["start"] <= cur_mins < iv["end"]:
            return mins_to_hm(iv["end"])
        if iv["start"] <= cur_mins + 30 and cur_mins < iv["end"]:
            return mins_to_hm(iv["end"])

    return None


# ─── Events ──────────────────────────────────────────────────

def check_schedule_match(power_on: bool) -> str:
    if not state["schedule"]:
        return "unknown"
    cur_mins = now_minutes()
    groups = {i.get("group") for i in state["subscribers"].values() if i.get("group")}
    if not groups:
        return "unknown"

    for g in groups:
        for iv in get_off_intervals(g, "today"):
            if iv["start"] - 30 <= cur_mins <= iv["end"] + 30:
                return "scheduled"

    return "unscheduled" if not power_on else "scheduled"


def record_event(power_on: bool, source: str = ""):
    cur = now()
    if state["history"]:
        last = state["history"][-1]
        if last.get("end") is None:
            start = datetime.fromisoformat(last["start"])
            last["end"] = cur.isoformat()
            last["duration_sec"] = int((cur - start).total_seconds())

    sched = check_schedule_match(power_on)
    state["history"].append({
        "type": "on" if power_on else "off",
        "start": cur.isoformat(),
        "end": None, "duration_sec": None,
        "scheduled": sched,
        "source": source,
    })
    state["power_on"] = power_on
    state["last_change"] = cur.isoformat()
    save_data()


def sched_tag(s: str) -> str:
    if s == "scheduled": return "\n📋 Планове відключення"
    if s == "unscheduled": return "\n⚠️ Позапланове відключення!"
    return ""

def prev_dur_text() -> str:
    if len(state["history"]) >= 2:
        p = state["history"][-2]
        if p.get("duration_sec"):
            lbl = "Світло було" if p["type"] == "on" else "Світла не було"
            return f"\n⏱ {lbl}: <b>{fmt_dur(p['duration_sec'])}</b>"
    return ""


# ─── Notify ──────────────────────────────────────────────────

async def notify_all(text: str):
    if not telegram_app: return
    for cid in list(state["subscribers"]):
        try:
            await telegram_app.bot.send_message(chat_id=int(cid), text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"send {cid}: {e}")

async def notify_group(text: str, group: str):
    if not telegram_app: return
    for cid, info in state["subscribers"].items():
        if info.get("group") == group:
            try:
                await telegram_app.bot.send_message(chat_id=int(cid), text=text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"send {cid}: {e}")

async def power_on_event(source: str):
    if state["power_on"] is True:
        return
    record_event(True, source)
    s = state["history"][-1].get("scheduled", "unknown")
    group = get_any_group()
    next_off = find_next_outage_time(group) if group else None
    next_off_text = f"\n🔴 Наступне відключення за графіком: {next_off}" if next_off else ""
    msg = (
        f"💡 <b>Світло УВІМКНЕНО!</b>\n"
        f"🕐 {fmt_time(now())}"
        f"{prev_dur_text()}"
        f"{sched_tag(s)}"
        f"{next_off_text}"
    )
    await notify_all(msg)
    logger.info(f"💡 ON ({source})")

async def power_off_event(source: str, extra: str = ""):
    if state["power_on"] is False:
        return
    record_event(False, source)
    s = state["history"][-1].get("scheduled", "unknown")
    group = get_any_group()
    next_on = find_next_on_time(group) if group else None
    next_on_text = f"\n💡 За графіком увімкнення о {next_on}" if next_on else ""
    msg = (
        f"🔴 <b>Світло ВИМКНЕНО!</b>\n"
        f"🕐 {fmt_time(now())}"
        f"{prev_dur_text()}"
        f"{sched_tag(s)}"
        f"{next_on_text}"
    )
    await notify_all(msg)
    logger.info(f"🔴 OFF ({source})")


# ═══════════════════════════════════════════════════════════════
#  ESP32 Heartbeat
# ═══════════════════════════════════════════════════════════════

async def handle_heartbeat(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Secret", "")
    if config.HEARTBEAT_SECRET and secret != config.HEARTBEAT_SECRET:
        return web.Response(status=403, text="forbidden")
    state["last_heartbeat"] = now().isoformat()
    await power_on_event("ESP32")
    return web.Response(text="ok")

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "power_on": state["power_on"],
        "last_heartbeat": state["last_heartbeat"],
        "schedule_source": state["schedule_source"],
        "schedule_updated": state["schedule_updated"],
        "tuya_power_w": state["tuya_power_w"],
        "subscribers": len(state["subscribers"]),
    })

async def start_http_server():
    app = web.Application()
    app.router.add_post("/heartbeat", handle_heartbeat)
    app.router.add_get("/heartbeat", handle_heartbeat)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.HEARTBEAT_PORT).start()
    logger.info(f"📡 HTTP: порт {config.HEARTBEAT_PORT}")

async def esp_watchdog():
    while True:
        await asyncio.sleep(10)
        if not state["last_heartbeat"]:
            continue
        hb = datetime.fromisoformat(state["last_heartbeat"])
        elapsed = (now() - hb).total_seconds()

        # Розумний таймаут: короткий під час планового, довгий поза графіком
        is_scheduled = check_schedule_match(False) == "scheduled"
        timeout = config.HEARTBEAT_TIMEOUT if is_scheduled else getattr(config, "HEARTBEAT_TIMEOUT_UNSCHEDULED", config.HEARTBEAT_TIMEOUT)

        if elapsed > timeout:
            await power_off_event("ESP32")


# ═══════════════════════════════════════════════════════════════
#  Tuya Smart Plug
# ═══════════════════════════════════════════════════════════════

async def tuya_poller():
    consecutive_offline = 0
    THRESHOLD = 3
    while True:
        await asyncio.sleep(config.TUYA_POLL_INTERVAL)
        if not tuya:
            continue
        status = await tuya.get_device_status()
        if status is None:
            continue
        state["last_tuya_check"] = now().isoformat()
        if status["online"]:
            consecutive_offline = 0
            state["tuya_power_w"] = status.get("power_w")
            state["tuya_voltage"] = status.get("voltage")
            await power_on_event("Tuya")
        else:
            consecutive_offline += 1
            if consecutive_offline >= THRESHOLD:
                await power_off_event("Tuya")


# ═══════════════════════════════════════════════════════════════
#  Schedule display & warnings
# ═══════════════════════════════════════════════════════════════

def format_schedule(group: str, day: str = "today") -> str:
    if not state["schedule"]:
        return "📅 Графік ще не завантажено."
    if group not in state["schedule"]:
        return f"📅 Група <b>{group}</b> не знайдена. Перевірте номер."

    slots = get_full_slots(group, day)
    if not slots:
        day_label = "сьогодні" if day == "today" else "завтра"
        return f"📅 Група <b>{group}</b> на {day_label}: графік відсутній"

    day_label = "сьогодні" if day == "today" else "завтра"
    lines = [f"📅 Графік на <b>{day_label}</b> | Група <b>{group}</b>\n"]

    cur_mins = now_minutes()
    has_outage = False

    for sl in slots:
        s, e = sl["start"], sl["end"]
        t = sl["type"]
        time_str = f"{mins_to_hm(s)} — {mins_to_hm(e)}"

        if t == "off":
            icon, lb = "🔴", "відключення"
            has_outage = True
        else:
            icon, lb = "🟢", "є світло"

        cur_mark = ""
        if day == "today" and s <= cur_mins < e:
            cur_mark = " ◄ зараз"

        lines.append(f"  {icon} {time_str}  {lb}{cur_mark}")

    if not has_outage:
        lines.append("\n✅ Відключень не заплановано!")

    src = state.get("schedule_source", "")
    upd = ""
    if state["schedule_updated"]:
        upd = fmt_time(datetime.fromisoformat(state["schedule_updated"]))
    lines.append(f"\n🔄 {src} | {upd}")

    return "\n".join(lines)


async def schedule_loop():
    while True:
        await fetch_schedule()
        await check_upcoming()
        await asyncio.sleep(config.SCHEDULE_CHECK_INTERVAL)


async def check_upcoming():
    if not state["schedule"]:
        return
    cur = now()
    cur_mins = now_minutes()
    ahead = config.NOTIFY_BEFORE_MINUTES
    groups = {i.get("group") for i in state["subscribers"].values() if i.get("group")}

    for g in groups:
        for iv in get_off_intervals(g, "today"):
            key = f"{cur.date()}_{g}_{iv['start']}"
            if key in state["notified_slots"]:
                continue
            diff = iv["start"] - cur_mins
            if 0 < diff <= ahead:
                dur = iv["end"] - iv["start"]
                msg = (
                    f"⏰ <b>Увага!</b> Через ~{diff} хв планове відключення\n"
                    f"🕐 {mins_to_hm(iv['start'])} — {mins_to_hm(iv['end'])} ({dur} хв)"
                )
                await notify_group(msg, g)
                state["notified_slots"].add(key)

    today_pfx = str(cur.date())
    state["notified_slots"] = {k for k in state["notified_slots"] if k.startswith(today_pfx)}


# ═══════════════════════════════════════════════════════════════
#  Статистика
# ═══════════════════════════════════════════════════════════════

def status_text() -> str:
    if state["power_on"] is True: return "💡 Є світло"
    if state["power_on"] is False: return "🔴 Немає світла"
    return "❓ Очікую перший сигнал"

def full_status(cid: str = None) -> str:
    cur = now()
    dur = ""
    if state["last_change"]:
        d = int((cur - datetime.fromisoformat(state["last_change"])).total_seconds())
        w = "увімкнено" if state["power_on"] else "вимкнено"
        dur = f"\n⏱ {w.capitalize()} вже: <b>{fmt_dur(d)}</b>"

    tuya_txt = ""
    if config.TUYA_ENABLED and state["tuya_power_w"] is not None:
        tuya_txt = f"\n🔌 Споживання: <b>{state['tuya_power_w']:.1f} Вт</b>"
        if state["tuya_voltage"]:
            tuya_txt += f" | {state['tuya_voltage']:.0f} В"

    grp = ""
    if cid and cid in state["subscribers"]:
        g = state["subscribers"][cid].get("group")
        grp = f"\n📋 Група: <b>{g}</b>" if g else "\n📋 Група: /setgroup"

    nxt = next_outage_text(cid) if cid else ""

    return (f"🔌 <b>Статус електроенергії</b>\n\n"
            f"Стан: <b>{status_text()}</b>{dur}\n"
            f"🕐 {fmt_time(cur)}{tuya_txt}{grp}{nxt}")

def next_outage_text(cid: str = None) -> str:
    if not cid or cid not in state["subscribers"]: return ""
    g = state["subscribers"][cid].get("group")
    if not g or not state["schedule"]: return ""
    cur_mins = now_minutes()

    for iv in get_off_intervals(g, "today"):
        if iv["start"] > cur_mins:
            diff = iv["start"] - cur_mins
            return f"\n\n⏰ Наступне: <b>{mins_to_hm(iv['start'])}—{mins_to_hm(iv['end'])}</b> (через {fmt_dur(diff*60)})"
        if iv["start"] <= cur_mins < iv["end"]:
            left = iv["end"] - cur_mins
            return f"\n\n🔴 Зараз відключення: <b>{mins_to_hm(iv['start'])}—{mins_to_hm(iv['end'])}</b> (ще {fmt_dur(left*60)})"

    return "\n\n✅ Більше відключень на сьогодні немає"

def calc_stats(cutoff_dt: datetime = None, label: str = "Загальна") -> str:
    cur = now()
    on = off = ev_on = ev_off = sched_off = unsched_off = 0

    for ev in state["history"]:
        ev_start = datetime.fromisoformat(ev["start"])
        ev_end = datetime.fromisoformat(ev["end"]) if ev.get("end") else cur
        if cutoff_dt:
            if ev_end <= cutoff_dt:
                continue
            if ev_start < cutoff_dt:
                ev_start = cutoff_dt
        d = int((ev_end - ev_start).total_seconds())
        if d <= 0:
            continue
        if ev["type"] == "on":
            on += d; ev_on += 1
        else:
            off += d; ev_off += 1
            if ev.get("scheduled") == "scheduled": sched_off += 1
            elif ev.get("scheduled") == "unscheduled": unsched_off += 1

    # Якщо є cutoff — рахуємо загальний період від cutoff до зараз
    if cutoff_dt:
        total = int((cur - cutoff_dt).total_seconds())
        # Якщо є нерозподілений час — додаємо до "on" (якщо світло є) або "off"
        tracked = on + off
        gap = total - tracked
        if gap > 0:
            if state.get("power_on") is True or state.get("power_on") is None:
                on += gap
            else:
                off += gap
    else:
        total = on + off

    if not total: return f"📊 <b>{label}</b>\n\nНемає даних."
    po, pf = on/total*100, off/total*100
    bl = 20; ob = round(po/100*bl)
    bar = "🟢"*ob + "🔴"*(bl-ob)
    sc = ""
    if sched_off or unsched_off:
        sc = f"\n📋 Планових: {sched_off}\n⚠️ Позапланових: {unsched_off}"
    return (f"📊 <b>{label}</b>\n\n{bar}\n\n"
            f"💡 Було: <b>{fmt_dur(on)}</b> ({po:.1f}%)\n"
            f"🔴 Не було: <b>{fmt_dur(off)}</b> ({pf:.1f}%)\n\n"
            f"📈 Увімкнень: {ev_on}\n📉 Вимкнень: {ev_off}{sc}\n"
            f"⏱ Період: {fmt_dur(total)}")


# ═══════════════════════════════════════════════════════════════
#  Команди
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    if cid not in state["subscribers"]:
        state["subscribers"][cid] = {"group": None}
        save_data()
    g = state["subscribers"][cid].get("group")
    gt = f"Група: <b>{g}</b>" if g else "⚠️ Група не встановлена"
    kb = [
        [InlineKeyboardButton("📊 Статус", callback_data="status"),
         InlineKeyboardButton("📅 Графік", callback_data="sched")],
        [InlineKeyboardButton("📅 Завтра", callback_data="sched_tom"),
         InlineKeyboardButton("📋 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📅 Сьогодні", callback_data="today"),
         InlineKeyboardButton("📅 Тиждень", callback_data="week")],
        [InlineKeyboardButton("📜 Історія", callback_data="hist"),
         InlineKeyboardButton("🔌 Споживання", callback_data="power")],
        [InlineKeyboardButton("🔕 Відписатись", callback_data="unsub")],
    ]
    await update.message.reply_text(
        f"🔌 <b>Бот моніторингу електроенергії</b>\n\n"
        f"{status_text()}\n{gt}\n\nВи підписані ✅\n"
        f"Встановити групу: /setgroup 27.1\n",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def cmd_setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    if cid not in state["subscribers"]:
        state["subscribers"][cid] = {"group": None}
    if not ctx.args:
        await update.message.reply_text(
            "📋 Використання: /setgroup 27.1\n\n"
            "Дізнатись групу:\n"
            "• Сайт ДТЕК → F12 → Network → ajax\n"
            "• @DTEK_KEM_bot в Telegram", parse_mode="HTML")
        return
    g = ctx.args[0].strip()
    state["subscribers"][cid]["group"] = g
    save_data()
    await update.message.reply_text(
        f"✅ Група: <b>{g}</b>\n/schedule — графік", parse_mode="HTML")

async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    g = state["subscribers"].get(cid, {}).get("group")
    if not g:
        await update.message.reply_text("⚠️ /setgroup 27.1"); return
    await fetch_schedule()
    await update.message.reply_text(format_schedule(g, "today"), parse_mode="HTML")

async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    g = state["subscribers"].get(cid, {}).get("group")
    if not g:
        await update.message.reply_text("⚠️ /setgroup 27.1"); return
    await fetch_schedule()
    await update.message.reply_text(format_schedule(g, "tomorrow"), parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(full_status(str(update.effective_chat.id)), parse_mode="HTML")

async def cmd_power(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.TUYA_ENABLED:
        await update.message.reply_text("🔌 Tuya не підключена. Налаштуйте в config.py")
        return
    if tuya:
        st = await tuya.get_device_status()
        if st and st["online"]:
            await update.message.reply_text(
                f"🔌 <b>Споживання</b>\n⚡ {st.get('power_w',0):.1f} Вт | "
                f"{st.get('voltage',0):.0f} В", parse_mode="HTML")
        else:
            await update.message.reply_text("🔴 Розетка офлайн")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(calc_stats(None, "Загальна"), parse_mode="HTML")

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today_start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    await update.message.reply_text(calc_stats(today_start, "За сьогодні"), parse_mode="HTML")

async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = now()
    monday = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    await update.message.reply_text(calc_stats(monday, "За тиждень"), parse_mode="HTML")

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    evts = state["history"][-15:]
    if not evts:
        await update.message.reply_text("📋 Порожньо."); return
    lines = ["📋 <b>Останні події:</b>\n"]
    for ev in reversed(evts):
        i = "💡" if ev["type"] == "on" else "🔴"
        t = datetime.fromisoformat(ev["start"]).strftime("%d.%m %H:%M")
        d = f" ({fmt_dur(ev['duration_sec'])})" if ev.get("duration_sec") else ""
        a = "увімк" if ev["type"] == "on" else "вимк"
        sc = " 📋" if ev.get("scheduled") == "scheduled" else (" ⚠️" if ev.get("scheduled") == "unscheduled" else "")
        lines.append(f"{i} {t} — {a}{d}{sc}")
    lines.append("\n📋 = планове | ⚠️ = позапланове")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔌 <b>Команди:</b>\n\n"
        "/start — меню\n/setgroup 27.1 — група\n"
        "/schedule — графік сьогодні\n/tomorrow — завтра\n"
        "/status — стан\n/power — споживання (Tuya)\n"
        "/stats — статистика\n/today — за 24г\n/week — тиждень\n"
        "/history — події\n/help — довідка", parse_mode="HTML")

async def btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    d, cid = q.data, str(q.message.chat_id)
    try:
        if d == "status": await q.edit_message_text(full_status(cid), parse_mode="HTML")
        elif d == "sched":
            g = state["subscribers"].get(cid, {}).get("group")
            if not g: await q.edit_message_text("⚠️ /setgroup 27.1")
            else: await fetch_schedule(); await q.edit_message_text(format_schedule(g), parse_mode="HTML")
        elif d == "sched_tom":
            g = state["subscribers"].get(cid, {}).get("group")
            if not g: await q.edit_message_text("⚠️ /setgroup 27.1")
            else: await fetch_schedule(); await q.edit_message_text(format_schedule(g, "tomorrow"), parse_mode="HTML")
        elif d == "stats": await q.edit_message_text(calc_stats(None, "Загальна"), parse_mode="HTML")
        elif d == "today":
            today_start = now().replace(hour=0, minute=0, second=0, microsecond=0)
            await q.edit_message_text(calc_stats(today_start, "За сьогодні"), parse_mode="HTML")
        elif d == "week":
            n = now()
            monday = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            await q.edit_message_text(calc_stats(monday, "За тиждень"), parse_mode="HTML")
        elif d == "hist":
            evts = state["history"][-10:]
            if not evts: await q.edit_message_text("📋 Порожньо."); return
            lines = ["📋 <b>Останні:</b>\n"]
            for ev in reversed(evts):
                i = "💡" if ev["type"] == "on" else "🔴"
                t = datetime.fromisoformat(ev["start"]).strftime("%d.%m %H:%M")
                d2 = f" ({fmt_dur(ev['duration_sec'])})" if ev.get("duration_sec") else ""
                a = "увімк" if ev["type"] == "on" else "вимк"
                sc = " 📋" if ev.get("scheduled") == "scheduled" else (" ⚠️" if ev.get("scheduled") == "unscheduled" else "")
                lines.append(f"{i} {t} — {a}{d2}{sc}")
            lines.append("\n📋 = планове | ⚠️ = позапланове")
            await q.edit_message_text("\n".join(lines), parse_mode="HTML")
        elif d == "power":
            if not config.TUYA_ENABLED:
                await q.edit_message_text("🔌 Tuya не підключена")
            elif tuya:
                st = await tuya.get_device_status()
                if st and st["online"]:
                    await q.edit_message_text(
                        f"🔌 <b>Споживання</b>\n⚡ {st.get('power_w',0):.1f} Вт | "
                        f"{st.get('voltage',0):.0f} В", parse_mode="HTML")
                else:
                    await q.edit_message_text("🔴 Розетка офлайн")
        elif d == "unsub":
            state["subscribers"].pop(cid, None); save_data()
            await q.edit_message_text("🔕 Відписано. /start")
    except Exception as e:
        logger.error(f"btn error: {e}")


# ═══════════════════════════════════════════════════════════════
#  Запуск
# ═══════════════════════════════════════════════════════════════

async def post_init(app: Application):
    global telegram_app, tuya
    telegram_app = app
    load_data()

    if config.ESP_ENABLED:
        await start_http_server()
        asyncio.create_task(esp_watchdog())
        logger.info("✅ ESP32 heartbeat увімкнено")

    if config.TUYA_ENABLED:
        tuya = TuyaCloud(
            config.TUYA_ACCESS_ID, config.TUYA_ACCESS_SECRET,
            config.TUYA_DEVICE_ID, config.TUYA_REGION,
        )
        asyncio.create_task(tuya_poller())
        logger.info("✅ Tuya polling увімкнено")

    if config.SCHEDULE_ENABLED:
        asyncio.create_task(schedule_loop())
        logger.info("✅ Графіки увімкнено")

    logger.info("✅ Бот v2.2 запущено!")

def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ BOT_TOKEN!"); return
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    for cmd, fn in [
        ("start", cmd_start), ("setgroup", cmd_setgroup),
        ("schedule", cmd_schedule), ("tomorrow", cmd_tomorrow),
        ("status", cmd_status), ("power", cmd_power),
        ("stats", cmd_stats), ("today", cmd_today),
        ("week", cmd_week), ("history", cmd_history), ("help", cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(btn))
    print("🔌 Бот v2.2")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
