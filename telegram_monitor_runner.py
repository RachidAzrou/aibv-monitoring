#!/usr/bin/env python3
import asyncio
import logging
import time
from typing import Optional, Dict, Any, List, Tuple

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    AIORateLimiter,
)

from config import Config
from selenium_monitor import AIBVMonitorBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("TG_MON")

HELP = (
    "🔎 Monitor-bot (geen boekingen)\n\n"
    "Commando’s:\n"
    "• /monitor — start 24u monitoring van de **week van morgen** (geen weekends, max 3 werkdagen)\n"
    "• /status — toon tussentijdse resultaten\n"
    "• /stop — stop de monitoring en toon eindrapport\n"
    "• /help — toon dit overzicht\n"
)

# ---------- Per-chat state ----------
class MonitorState:
    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.stop_flag: bool = False
        # Lijst met tuples (timestamp_iso, "dd/mm/YYYY HH:MM")
        self.found: List[Tuple[str, str]] = []
        self.last_status: Dict[str, Any] = {}
        self.started_at: float = time.time()

# chat_id -> MonitorState
SESSIONS: Dict[int, MonitorState] = {}

# ---------- Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("AIBV Monitor-bot klaar ✅\n" + HELP)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Ik herken dit niet.\n"
        "Gebruik een van deze commando’s:\n\n" + HELP
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = SESSIONS.get(chat_id)
    if not st or not st.task:
        return await update.message.reply_text("ℹ️ Er draait momenteel geen monitor-sessie.\nGebruik /monitor om te starten.")

    elapsed = int(time.time() - st.started_at)
    mins = elapsed // 60
    secs = elapsed % 60

    total = len(st.found)
    tail = "\n".join([f"• {ts} — {slot}" for ts, slot in st.found[-10:]]) if total else "• (nog niets gevonden)"

    await update.message.reply_text(
        "📊 Tussentijdse status\n"
        f"• Verstreken tijd: {mins:02d}:{secs:02d}\n"
        f"• Totaal gevonden: {total}\n"
        f"• Laatste 10:\n{tail}"
    )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = SESSIONS.get(chat_id)
    if not st or not st.task:
        return await update.message.reply_text("ℹ️ Er draait momenteel geen monitor-sessie.")

    st.stop_flag = True
    await update.message.reply_text("⏹️ Stopverzoek ontvangen. Ik rond netjes af en stuur het rapport…")

async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    log.info(f"[chat {chat_id}] monitor start aangevraagd door @{user.username or user.id}")

    st = SESSIONS.get(chat_id)
    if st and st.task and not st.task.done():
        return await update.message.reply_text("⚠️ Er draait al een monitor-sessie. Gebruik /status of /stop.")

    # (re)start state
    st = MonitorState()
    SESSIONS[chat_id] = st

    await update.message.reply_text(
        "🚀 Monitor gestart voor **week van morgen**.\n"
        "• Weekends worden overgeslagen\n"
        "• Alleen slots binnen 3 werkdagen\n"
        "• Ik stuur elke 5 min een status\n"
        "• Max duur: 24u of tot /stop\n\n"
        "Ik ga inloggen en de flow openen…"
    )

    async def periodic_status():
        start_ts = time.time()
        while True:
            await asyncio.sleep(300)  # 5 min
            # Als task weg is: stoppen
            if st.task is None or st.task.done():
                break
            elapsed = int(time.time() - start_ts)
            mins = elapsed // 60
            secs = elapsed % 60
            total = len(st.found)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⏳ Nog bezig met monitoren…\n"
                        f"• Verstreken tijd: {mins:02d}:{secs:02d}\n"
                        f"• Gevonden slots tot nu: {total}\n"
                        f"• Refresh-interval: {Config.REFRESH_DELAY}s"
                    ),
                )
            except Exception as e:
                log.warning(f"[chat {chat_id}] status push faalde: {e}")

    async def runner():
        bot = AIBVMonitorBot()
        try:
            # Start browser (in aparte thread i.v.m. CPU-bound init)
            try:
                await asyncio.to_thread(bot.setup_driver)
            except Exception as e:
                log.exception(f"[chat {chat_id}] browser start faalde")
                return await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Fout bij starten van de browser: {e}",
                )

            async def push(msg: str):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                except Exception as e:
                    log.warning(f"[chat {chat_id}] push mislukte: {e}")

            await push("🔐 Inloggen en flow openen…")

            # status task
            status_task: Optional[asyncio.Task] = asyncio.create_task(periodic_status())

            def stop_checker() -> bool:
                return st.stop_flag

            def status_hook(snap: dict):
                st.last_status = snap

            # monitoren: 24u of tot stop
            result = await asyncio.to_thread(bot.monitor_24h_collect, stop_checker, status_hook)

            # status task opkuisen
            if status_task and not status_task.done():
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass

            # resultaten opslaan
            if isinstance(result, dict) and "found" in result:
                st.found.extend(result["found"])

            # rapport opstellen
            duration = int(time.time() - st.started_at)
            mins = duration // 60
            secs = duration % 60
            total = len(st.found)

            header = "✅ Monitoring gestopt op verzoek." if result.get("ended") == "stopped" else "⏱️ 24u monitoring afgelopen."
            lines = "\n".join([f"• {ts} — {slot}" for ts, slot in st.found]) if total else "• (geen slots gevonden)"
            await push(
                f"{header}\n"
                f"• Totale duur: {mins:02d}:{secs:02d}\n"
                f"• Totaal gevonden: {total}\n\n"
                f"📜 Overzicht:\n{lines}"
            )

        except Exception as e:
            log.exception(f"[chat {chat_id}] monitor crashte")
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Fout tijdens monitoring: {e}")
        finally:
            bot.close()
            log.info(f"[chat {chat_id}] monitor beëindigd")

    # start runner
    st.task = asyncio.create_task(runner())

# ---------- App ----------
def main():
    app = (
        ApplicationBuilder()
        .token(Config.TELEGRAM_TOKEN)           # Zet je MONITOR-bot token als env var TELEGRAM_TOKEN
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    log.info("Monitor-bot starting…")
    app.run_polling()

if __name__ == "__main__":
    main()
