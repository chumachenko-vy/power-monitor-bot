"""
🔌 Telegram бот моніторингу електроенергії v2.1
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
    "schedule_raw": None,
    "schedule_updated": None,
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


# ─── Schedule helpers ────────────────────────────────────────

def get_group_slots(group: str, day: str = "today") -> list:
    if not state["schedule_raw"]:
        return []
    group_data = state["schedule_raw"].get(group)
    if not group_data:
        return []
    return group_data.get(day, {}).get("slots", [])

def get_group_status(group: str, day: str = "today") -> str:
    if not state["schedule_raw"]:
        return "unknown"
    group_data = state["schedule_raw"].get(group)
    if not group_data:
        return "unknown"
    return group_data.get(day, {}).get("status", "unknown")

def get_any_group() -> str | None:
    """Повертає першу групу з підписників."""
    for info in state["subscribers"].values():
        g = info.get("group")
        if g:
            return g
    return None

def find_next_outage_time(group: str) -> str | None:
    """Знаходить час наступного відключення за графіком."""
    if not group or get_group_status(group) != "ScheduleApplies":
        return None
    cur_mins = now_minutes()
    for sl in sorted(get_group_slots(group), key=lambda x: x.get("start", 0)):
        s, e, t = sl.get("start", 0), sl.get("end", 0), sl.get("type", "")
        if t not in ("Definite", "Possible"):
            continue
        if s > cur_mins:
            return mins_to_hm(s)
    return None

def find_next_on_time(group: str) -> str | None:
    """Знаходить час увімкнення (кінець поточного відключення) за графіком."""
    if not group or get_group_status(group) != "ScheduleApplies":
        return None
    cur_mins = now_minutes()
    for sl in sorted(get_group_slots(group), key=lambda x: x.get("start", 0)):
        s, e, t = sl.get("start", 0), sl.get("end", 0), sl.get("type", "")
        if t not in ("Definite", "Possible"):
            continue
        if s <= cur_mins < e:
            return mins_to_hm(e)
        if s > cur_mins:
            # Ще не дійшли до цього слоту — поточне відключення позапланове
            return None
    return None


# ─── Events ──────────────────────────────────────────────────

def check_schedule_match(power_on: bool) -> str:
    if not state["schedule_raw"]:
        return "unknown"
    cur_mins = now_minutes()
    groups = {i.get("group") for i in state["subscribers"].values() if i.get("group")}
    if not groups:
        return "unknown"
    for g in groups:
        if get_group_status(g) != "ScheduleApplies":
            continue
        for slot in get_group_slots(g):
            if slot.get("type") in ("Definite", "Possible"):
                s, e = slot.get("start", 0), slot.get("end", 0)
                if s - 30 <= cur_mins <= e + 30:
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

    # Наступне відключення за графіком
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

    # Коли буде світло за графіком
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
        if (now() - hb).total_seconds() > config.HEARTBEAT_TIMEOUT:
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
#  YASNO API
# ═══════════════════════════════════════════════════════════════

async def fetch_yasno():
    try:
        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as s:
            async with s.get(config.YASNO_API_URL) as r:
                if r.status != 200:
                    logger.warning(f"YASNO: HTTP {r.status}")
                    return
                data = await r.json()
        state["schedule_raw"] = data
        state["schedule_updated"] = now().isoformat()
        logger.info("📅 YASNO графік оновлено")
    except Exception as e:
        logger.error(f"YASNO: {e}")

def format_schedule(group: str, day: str = "today") -> str:
    if not state["schedule_raw"]:
        return "📅 Графік ще не завантажено."
    group_data = state["schedule_raw"].get(group)
    if not group_data:
        return f"📅 Група <b>{group}</b> не знайдена в графіку."
    day_data = group_data.get(day, {})
    status = day_data.get("status", "")
    slots = day_data.get("slots", [])
    day_label = "сьогодні" if day == "today" else "завтра"

    if status == "WaitingForSchedule":
        return f"📅 Графік на <b>{day_label}</b> | Група <b>{group}</b>\n\n⏳ Графік ще не сформовано"
    if not slots:
        return f"📅 Група <b>{group}</b> на {day_label}: немає даних"

    lines = [f"📅 Графік на <b>{day_label}</b> | Група <b>{group}</b>\n"]
    cur_mins = now_minutes()
    has_outage = False

    for sl in sorted(slots, key=lambda x: x.get("start", 0)):
        s = sl.get("start", 0)
        e = sl.get("end", 0)
        t = sl.get("type", "")
        time_str = f"{mins_to_hm(s)} — {mins_to_hm(e)}"
        if t == "Definite":
            icon, lb = "🔴", "відключення"
            has_outage = True
        elif t == "Possible":
            icon, lb = "🟡", "можливе відкл."
            has_outage = True
        else:
            icon, lb = "🟢", "є світло"
        cur_mark = ""
        if day == "today" and s <= cur_mins < e:
            cur_mark = " ◄ зараз"
        lines.append(f"  {icon} {time_str}  {lb}{cur_mark}")

    if not has_outage:
        lines.append("\n✅ Відключень не заплановано!")
    if state["schedule_updated"]:
        upd = datetime.fromisoformat(state["schedule_updated"])
        lines.append(f"\n🔄 Оновлено: {fmt_time(upd)}")
    return "\n".join(lines)

async def schedule_loop():
    while True:
        await fetch_yasno()
        await check_upcoming()
        await asyncio.sleep(config.SCHEDULE_CHECK_INTERVAL)

async def check_upcoming():
    if not state["schedule_raw"]:
        return
    cur = now()
    cur_mins = now_minutes()
    ahead_mins = config.NOTIFY_BEFORE_MINUTES
    groups = {i.get("group") for i in state["subscribers"].values() if i.get("group")}

    for g in groups:
        if get_group_status(g) != "ScheduleApplies":
            continue
        for sl in get_group_slots(g):
            if sl.get("type") not in ("Definite", "Possible"):
                continue
            s = sl.get("start", 0)
            e = sl.get("end", 0)
            key = f"{cur.date()}_{g}_{s}"
            if key in state["notified_slots"]:
                continue
            diff = s - cur_mins
            if 0 < diff <= ahead_mins:
                dur = e - s
                msg = (
                    f"⏰ <b>Увага!</b> Через ~{diff} хв планове відключення\n"
                    f"🕐 {mins_to_hm(s)} — {mins_to_hm(e)} ({dur} хв)"
                )
                await notify_group(msg, g)
                state["notified_slots"].add(key)
                logger.info(f"⏰ Попередження: {g} о {mins_to_hm(s)}")

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
    if not g or not state["schedule_raw"]: return ""
    if get_group_status(g) != "ScheduleApplies": return ""

    cur_mins = now_minutes()
    for sl in sorted(get_group_slots(g), key=lambda x: x.get("start", 0)):
        s, e, t = sl.get("start", 0), sl.get("end", 0), sl.get("type", "")
        if t not in ("Definite", "Possible"):
            continue
        if s > cur_mins:
            diff = s - cur_mins
            return f"\n\n⏰ Наступне відключення: <b>{mins_to_hm(s)}—{mins_to_hm(e)}</b> (через {fmt_dur(diff*60)})"
        if s <= cur_mins < e:
            left = e - cur_mins
            return f"\n\n🔴 Зараз відключення: <b>{mins_to_hm(s)}—{mins_to_hm(e)}</b> (ще {fmt_dur(left*60)})"

    return "\n\n✅ Більше відключень на сьогодні немає"

def calc_stats(hours: int = None, label: str = "Загальна") -> str:
    cur = now()
    cutoff = cur - timedelta(hours=hours) if hours else None
    on = off = ev_on = ev_off = sched_off = unsched_off = 0
    for ev in state["history"]:
        st = datetime.fromisoformat(ev["start"])
        if cutoff and st < cutoff: continue
        d = ev.get("duration_sec") or int((cur - st).total_seconds())
        if ev["type"] == "on": on += d; ev_on += 1
        else:
            off += d; ev_off += 1
            if ev.get("scheduled") == "scheduled": sched_off += 1
            elif ev.get("scheduled") == "unscheduled": unsched_off += 1

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
            "• @DTEK_KEM_bot в Telegram\n"
            "• dtek-kem.com.ua/ua/shutdowns", parse_mode="HTML")
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
    await fetch_yasno()
    await update.message.reply_text(format_schedule(g, "today"), parse_mode="HTML")

async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    g = state["subscribers"].get(cid, {}).get("group")
    if not g:
        await update.message.reply_text("⚠️ /setgroup 27.1"); return
    await fetch_yasno()
    await update.message.reply_text(format_schedule(g, "tomorrow"), parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(full_status(str(update.effective_chat.id)), parse_mode="HTML")

async def cmd_power(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.TUYA_ENABLED:
        await update.message.reply_text("🔌 Tuya розетка не підключена.\nНалаштуйте TUYA_* в config.py")
        return
    if tuya:
        st = await tuya.get_device_status()
        if st and st["online"]:
            pw = st.get("power_w") or 0
            v = st.get("voltage") or 0
            ma = st.get("current_ma") or 0
            await update.message.reply_text(
                f"🔌 <b>Споживання зараз</b>\n\n"
                f"⚡ Потужність: <b>{pw:.1f} Вт</b>\n"
                f"🔋 Напруга: {v:.0f} В\n"
                f"💧 Струм: {ma} мА", parse_mode="HTML")
        else:
            await update.message.reply_text("🔴 Розетка офлайн")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(calc_stats(), parse_mode="HTML")

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(calc_stats(24, "За 24 години"), parse_mode="HTML")

async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(calc_stats(168, "За тиждень"), parse_mode="HTML")

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
            else: await fetch_yasno(); await q.edit_message_text(format_schedule(g), parse_mode="HTML")
        elif d == "sched_tom":
            g = state["subscribers"].get(cid, {}).get("group")
            if not g: await q.edit_message_text("⚠️ /setgroup 27.1")
            else: await fetch_yasno(); await q.edit_message_text(format_schedule(g, "tomorrow"), parse_mode="HTML")
        elif d == "stats": await q.edit_message_text(calc_stats(), parse_mode="HTML")
        elif d == "today": await q.edit_message_text(calc_stats(24, "За 24 години"), parse_mode="HTML")
        elif d == "week": await q.edit_message_text(calc_stats(168, "За тиждень"), parse_mode="HTML")
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

    if config.YASNO_ENABLED:
        asyncio.create_task(schedule_loop())
        logger.info("✅ YASNO графіки увімкнено")

    logger.info("✅ Бот v2.1 запущено!")

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

    print("🔌 Бот v2.1")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
