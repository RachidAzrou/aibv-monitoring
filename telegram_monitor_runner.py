# telegram_monitor_runner.py
import logging
import asyncio
import time
from typing import Optional, Tuple, List

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    AIORateLimiter, MessageHandler, filters
)

# ⬇️ Belangrijk: deze import voor strakkere except-blokken
from selenium.common.exceptions import TimeoutException

from config import Config
from selenium_monitor import AIBVMonitorBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("TG_MON")

HELP = (
    "Monitor bot (géén boeking)\n\n"
    "Commands:\n"
    "/monitor <chassis> | <merk model> | <dd/mm/jjjj>\n"
    "   ➜ Logt in, opent flow, kiest station + week van morgen,\n"
    "     en monitort continu tot /stop of 24u.\n\n"
    "/status  ➜ Tussentijdse status (aantal nieuwe slots).\n"
    "/stop    ➜ Stop monitoren & geef rapport.\n"
    "/report  ➜ Toon huidig rapport (tot nu toe).\n"
)

# Eén lopende monitoring per chat
running_task: Optional[asyncio.Task] = None
stop_flag = False
results: List[Tuple[str, str]] = []  # (timestamp_seen, label)
start_ts: Optional[float] = None


def stop_requested() -> bool:
    return stop_flag


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Monitor bot klaar ✅\n" + HELP)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if start_ts is None:
        return await update.message.reply_text("ℹ️ Er is geen actieve monitor.")
    elapsed = int(time.time() - start_ts)
    mins = elapsed // 60
    await update.message.reply_text(
        f"⏳ Monitor actief.\n"
        f"• Verstreken tijd: {mins} min\n"
        f"• Nieuwe slots gedetecteerd: {len(results)}"
    )


def format_report() -> str:
    if not results:
        return "📊 Rapport: (geen nieuwe slots gedetecteerd)"
    lines = ["📊 Rapport – nieuw verschenen slots:"]
    for ts, label in results:
        lines.append(f"• [{ts}] {label}")
    return "\n".join(lines)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_report())


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stop_flag, running_task
    stop_flag = True
    if running_task and not running_task.done():
        await update.message.reply_text("⏹️ Stopverzoek ontvangen. Ik rond netjes af…")
    else:
        await update.message.reply_text("ℹ️ Er draait momenteel geen actieve monitor.")


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Onbekende tekst.\nGebruik het juiste formaat:\n\n" + HELP
    )


async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running_task, stop_flag, results, start_ts
    stop_flag = False
    results = []
    start_ts = None

    if not update.message or not update.message.text:
        return

    # Parse args
    try:
        parts = update.message.text.split(" ", 1)
        if len(parts) != 2:
            return await update.message.reply_text("❌ Ongeldig formaat.\n\n" + HELP)
        rest = parts[1]
        fields = [p.strip() for p in rest.split("|")]
        if len(fields) != 3:
            return await update.message.reply_text("❌ Ongeldig formaat.\n\n" + HELP)
        chassis, merkmodel, datum = fields
    except Exception:
        return await update.message.reply_text("❌ Kon argumenten niet parsen.\n\n" + HELP)

    await update.message.reply_text(
        "🚀 Monitor gestart voor **week van morgen**.\n"
        "• Weekends worden overgeslagen\n"
        "• Alleen slots binnen 3 werkdagen\n"
        "• Max duur: 24u of tot /stop\n\n"
        "Ik ga inloggen en de flow openen…"
    )

    async def runner():
        global results, start_ts

        bot = AIBVMonitorBot()

        try:
            # DRIVER
            try:
                bot.setup_driver()
            except Exception as e:
                await update.message.reply_text(f"❌ Fout bij starten van de browser: {e}")
                return

            await update.message.reply_text("🔐 Inloggen en flow openen…")
            try:
                bot.login()
                bot.add_vehicle(chassis, merkmodel, datum)
                bot.select_eu_vehicle()
                bot.select_station()
            except TimeoutException as e:
                # ⬇️ Voeg context toe (URL + TITLE) voor duidelijke diagnose
                await update.message.reply_text(
                    "❌ Timeout tijdens inloggen/flow:\n"
                    f"{e}\n"
                    f"{bot._dbg_context()}"
                )
                return
            except Exception as e:
                await update.message.reply_text(
                    "❌ Fout tijdens inloggen/flow:\n"
                    f"{e}\n"
                    f"{bot._dbg_context()}"
                )
                return

            # Week van morgen zetten
            ok = bot.select_week_of_tomorrow()
            if not ok:
                await update.message.reply_text(
                    "❌ Kon 'week van morgen' niet selecteren in dropdown.\n"
                    f"{bot._dbg_context()}"
                )
                return

            await update.message.reply_text("🔎 Monitoren gestart… (ik meld alleen als er iets nieuws is) ")
            start_ts = time.time()

            # Run monitoring in thread (blokkerend Selenium)
            result = await asyncio.to_thread(
                bot.monitor_slots,
                stop_requested,
                24 * 3600,
                None  # geen 5-min status push
            )

            # Klaar -> bundel rapport
            if result.get("success"):
                results = result.get("new_slots", [])
                if result.get("stopped"):
                    await update.message.reply_text("🛑 Gestopt op jouw verzoek.\n\n" + format_report())
                elif result.get("timeout"):
                    await update.message.reply_text("⏲️ 24u afgelopen.\n\n" + format_report())
                else:
                    await update.message.reply_text("✅ Monitor klaar.\n\n" + format_report())
            else:
                await update.message.reply_text(f"❌ Monitor fout: {result.get('error','Onbekend')}")

        except Exception as e:
            log.exception("monitor runner error")
            # ⬇️ Context ook hier, voor safety
            try:
                ctx = bot._dbg_context()
            except Exception:
                ctx = "(geen context beschikbaar)"
            await update.message.reply_text(f"❌ Onverwachte fout: {e}\n{ctx}")

        finally:
            bot.close()

    # Start de taak
    running_task = asyncio.create_task(runner())


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
    app.add_handler(CommandHandler("report", report_cmd))

    # Onbekende tekst -> help
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    log.info("Monitor bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
