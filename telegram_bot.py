import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import json
import os
import aiofiles

# Cargar config del bot principal
CONFIG_FILE = "config.json"

async def load_config():
    async with aiofiles.open(CONFIG_FILE, "r") as f:
        return json.loads(await f.read())

async def save_config(cfg):
    async with aiofiles.open(CONFIG_FILE, "w") as f:
        await f.write(json.dumps(cfg, indent=4))

# --- COMANDOS TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *CPR Trading Bot v55*\nComandos disponibles:\n"
        "/status - Estado del bot\n"
        "/pivots - Ver pivotes actuales\n"
        "/toggle - Activar/desactivar trading\n"
        "/setlimit X - Cambiar límite diario (%)\n"
        "/restart - Reiniciar bot principal",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await load_config()
    msg = (
        f"📊 *Estado CPR Bot*\n"
        f"Modo Testnet: `{cfg['testnet_mode']}`\n"
        f"Límite diario: `{cfg['daily_loss_limit_percent']}%`\n"
        f"Trading activo: `{cfg.get('enabled', True)}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def pivots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists("latest_pivots.txt"):
        async with aiofiles.open("latest_pivots.txt", "r") as f:
            piv = await f.read()
        await update.message.reply_text(f"📐 *Pivotes actuales:*\n{piv}", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Aún no se calcularon pivotes.")

async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = await load_config()
    cfg["enabled"] = not cfg.get("enabled", True)
    await save_config(cfg)
    estado = "🟢 Activado" if cfg["enabled"] else "🔴 Pausado"
    await update.message.reply_text(f"Trading ahora: {estado}")

async def setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Uso: /setlimit 5")
    try:
        pct = float(context.args[0])
        cfg = await load_config()
        cfg["daily_loss_limit_percent"] = pct
        await save_config(cfg)
        await update.message.reply_text(f"✅ Límite diario actualizado a {pct}%")
    except ValueError:
        await update.message.reply_text("Valor inválido. Ejemplo: /setlimit 5")

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("♻️ Reiniciando servicio principal...")
    os.system("sudo systemctl restart cpr_bot.service")

async def main():
    cfg = await load_config()
    bot_token = cfg["telegram_token"]
    app = ApplicationBuilder().token(bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pivots", pivots))
    app.add_handler(CommandHandler("toggle", toggle))
    app.add_handler(CommandHandler("setlimit", setlimit))
    app.add_handler(CommandHandler("restart", restart))

    print("✅ Telegram bot en ejecución...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
