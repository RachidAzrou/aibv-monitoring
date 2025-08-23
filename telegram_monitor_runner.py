import logging
import asyncio
import time
import threading
from typing import Optional, Dict, List

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    AIORateLimiter,
    MessageHandler,
    filters,
)

from config import Config
from selenium_monitor import AIBVMonitorBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("TG-MON")

HELP = (
    "Monitor-bot (maakt géén reservatie)\n\n"
    "Gebruik:\n"
    "/monitor <chassis> | <merk model> | <dd/mm/jjjj>\n"
    "  ▶︎ Volgt de flow t/m het overzicht en monitort 24u lang.\n\n"
    "/status  ▶︎ tussentijdse lijst met gevonden slots\n"
    "/stop    ▶︎ stoppen en eindreport sturen\n\n"
    "Voorbeeld:\n"
    "/monitor ABC12345678901234 | Toyota Corolla | 01/01/2020"
)

# Per chat state
running_task: Dict[int, asyncio.Task] = {}
stop_flags: Dict[int, bool] = {}
buffers: Dict[int, List[dict]] = {}  # realtime events
locks: Dict[int, threading.Lock] = {}


def allowed_chat(update: Update) -> bool:
    if not Config.TELEGRAM_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(Config.TELEGRAM_CHAT_ID)
    except Exception:
        return False


def fmt_report(events: List[dict]) -> str:
    if not events:
        return "Geen slots gedetecteerd."
    lines = []
    for ev in events:
        lines.append(f"• {ev['slot']}  (gevonden: {ev['detected_at']})")
    return "\n".join(lines[:1000])  # hard cap, Telegram limit safeguard


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return await update.message.reply_text("⛔ Niet toegestaan voor deze chat.")
    await update.message.reply_text("Monitor-bot klaar ✅\n\n" + HELP)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    await update.message.reply_text(HELP)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    buf = buffers.get(chat_id, [])
    count = len(buf)
    if not count:
        return await update.message.reply_text("ℹ️ Nog geen slots gedetecteerd.")
    # Kopie onder lock
    lock = locks.setdefault(chat_id, threading.Lock())
    with lock:
        snapshot = list(buf)
    await update.message.reply_text(
        f"📊 Tussentijdse status — {len(snapshot)} slot(s) gevonden:\n\n" + fmt_report(snapshot[:50])
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    stop_flags[chat_id] = True
    task = running_task.get(chat_id)
    if task and not task.done():
        await update.message.reply_text("⏹️ Stopverzoek ontvangen. Ik rond netjes af…")
    else:
        await update.message.reply_text("ℹ️ Er draait momenteel geen monitor-proces.")


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    await update.message.reply_text(
        "❓ Onbekende input.\nGebruik alstublieft:\n\n" + HELP
    )


async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return
    chat_id = update.effective_chat.id

    # Als er al iets draait voor deze chat
    if chat_id in running_task and running_task[chat_id] and not running_task[chat_id].done():
        return await update.message.reply_text("⏳ Er draait al een monitor-run. Gebruik /stop om te stoppen.")

    if not update.message or not update.message.text:
        return

    # Parse parameters
    try:
        parts = update.message.text.split(" ", 1)
        if len(parts) != 2:
            return await update.message.reply_text("❌ Ongeldig formaat.\n\n" + HELP)
        fields = [p.strip() for p in parts[1].split("|")]
        if len(fields) != 3:
            return await update.message.reply_text("❌ Ongeldig formaat.\n\n" + HELP)

        chassis, merkmodel, datum = fields
        await update.message.reply_text(
            "🚀 Monitor start…\n"
            f"• Chassis: {chassis}\n"
            f"• Merk/Model: {merkmodel}\n"
            f"• Inschrijvingsdatum: {datum}\n\n"
            "Ik volg alle stappen en hou 24u lang alle vrijgekomen slots bij."
        )
    except Exception as e:
        log.exception("parse error")
        return await update.message.reply_text(f"❌ Fout: {e}")

    # Reset state voor deze chat
    stop_flags[chat_id] = False
    buffers[chat_id] = []
    locks.setdefault(chat_id, threading.Lock())

    async def runner():
        bot = AIBVMonitorBot()
        try:
            # 0) driver
            try:
                bot.setup_driver()
            except Exception as e:
                return await update.message.reply_text(f"❌ Browser startte niet: {e}")

            # 1) login
            await update.message.reply_text("🔐 Inloggen…")
            bot.login()
            if stop_flags.get(chat_id):
                return await update.message.reply_text("⏹️ Gestopt na login.")

            # 2) voertuig
            await update.message.reply_text("🚗 Voertuig toevoegen…")
            bot.add_vehicle(chassis, merkmodel, datum)
            if stop_flags.get(chat_id):
                return await update.message.reply_text("⏹️ Gestopt na voertuig toevoegen.")

            # 3) EU-voertuig
            await update.message.reply_text("🌍 EU-voertuig selecteren…")
            bot.select_eu_vehicle()
            if stop_flags.get(chat_id):
                return await update.message.reply_text("⏹️ Gestopt na EU-selectie.")

            # 4) station
            await update.message.reply_text("📍 Station selecteren…")
            bot.select_station()
            if stop_flags.get(chat_id):
                return await update.message.reply_text("⏹️ Gestopt na stationselectie.")

            await update.message.reply_text("👀 Monitoren gestart (max 24u)… Gebruik /status voor tussentijdse stand, /stop om te stoppen.")

            # thread-safe event buffer aanvullen
            lock = locks[chat_id]

            def on_new_event(ev: dict):
                with lock:
                    buffers[chat_id].append(ev)

            # stop-check closure
            def stop_check():
                return stop_flags.get(chat_id, False)

            # Draai monitor blokkerend in threadpool
            events = await asyncio.to_thread(
                bot.monitor_slots,
                24 * 3600,       # 24u
                stop_check,
                on_new_event
            )

            # Eindrapport
            with locks[chat_id]:
                snapshot = list(buffers[chat_id])  # alles wat wij live zagen
            # Voeg events (retour) toe indien nodig (zou identiek moeten zijn)
            if len(snapshot) < len(events):
                snapshot = events

            lines = [
                "🧾 EINDRAPPORT (monitor):",
                f"• Totale gevonden slots: {len(snapshot)}",
                "",
                fmt_report(snapshot[:200])
            ]
            await update.message.reply_text("\n".join(lines) or "🧾 Geen slots gedetecteerd.")
        except Exception as e:
            log.exception("monitor run error")
            await update.message.reply_text(f"❌ Fout: {e}")
        finally:
            bot.close()
            # opruimen
            stop_flags[chat_id] = False
            running_task.pop(chat_id, None)
            log.info(f"[chat {chat_id}] monitor afgerond")

    # Start async task
    t = asyncio.create_task(runner())
    running_task[chat_id] = t


def main():
    app = (
        ApplicationBuilder()
        .token(Config.TELEGRAM_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Andere tekst → help
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    log.info("Monitor-bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
