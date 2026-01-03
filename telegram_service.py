import telebot
import subprocess
import os
import sys
import time
from dotenv import load_dotenv

# Importamos nuestras herramientas compartidas para no reinventar la rueda
from shared.ccxt_handler import BinanceHandler
import config

# --- CONFIGURACIÓN ---
BASE_PATH = "/home/ubuntu/bot_cpr"
load_dotenv(os.path.join(BASE_PATH, ".env"))

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Inicializamos el bot
bot = telebot.TeleBot(TOKEN)
exchange_handler = BinanceHandler() # Usamos nuestro handler optimizado

# Restringir acceso solo a TI (Seguridad)
def is_authorized(message):
    if str(message.chat.id) != str(CHAT_ID):
        bot.reply_to(message, "⛔ Acceso denegado. Este bot es privado.")
        return False
    return True

# --- COMANDO: /start (Bienvenida) ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_authorized(message): return
    help_text = (
        "🐉 *HYDRA REMOTE CONTROL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 *COMANDOS DISPONIBLES:*\n\n"
        "📊 *ESTADO*\n"
        "/status - Estado del servicio y posiciones\n"
        "/balance - Ver saldo y PnL en Binance\n"
        "/logs - Ver últimos logs del sistema\n\n"
        "⚙️ *CONTROL*\n"
        "/start_bot - Arrancar Hydra\n"
        "/stop_bot - Detener Hydra\n"
        "/restart - Reiniciar servicio\n\n"
        "💀 *EMERGENCIA*\n"
        "/panic - ⚠️ CERRAR TODO A MERCADO"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# --- COMANDO: /status ---
@bot.message_handler(commands=['status'])
def status_command(message):
    if not is_authorized(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    
    # 1. Chequear Systemd (El servicio se llama cpr_bot)
    def check_service(name):
        try:
            res = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
            status = res.stdout.strip()
            if status == "active": return "🟢 ONLINE"
            elif status == "inactive": return "🔴 OFFLINE"
            else: return f"🟡 {status.upper()}"
        except: return "❓ ERROR"

    service_status = check_service("cpr_bot")
    
    # 2. Leer Posiciones Abiertas (Directo de Binance para mayor precisión)
    try:
        positions = exchange_handler.get_open_positions()
        active_count = len(positions)
        positions_txt = ""
        
        if active_count > 0:
            for pos in positions:
                pnl = float(pos['pnl'])
                icon = "🟢" if pnl >= 0 else "🔴"
                positions_txt += (
                    f"{icon} *{pos['symbol']}*\n"
                    f"   Entry: `{pos['entry_price']}` | Size: `{pos['amount']}`\n"
                    f"   PnL: `${pnl:.2f}`\n"
                )
        else:
            positions_txt = "_Sin posiciones activas._"
    except Exception as e:
        positions_txt = f"⚠️ Error leyendo exchange: {str(e)}"
        active_count = "?"

    msg = (
        f"📊 *ESTADO DEL SISTEMA*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Servicio Hydra:* {service_status}\n\n"
        f"💼 *Posiciones Abiertas ({active_count}):*\n"
        f"{positions_txt}"
    )
    bot.reply_to(message, msg, parse_mode="Markdown")

# --- COMANDO: /balance ---
@bot.message_handler(commands=['balance'])
def balance_command(message):
    if not is_authorized(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    
    try:
        # Usamos métodos de ccxt raw a través de nuestro handler para info detallada
        balance = exchange_handler.exchange.fetch_balance()
        total_usdt = balance['total']['USDT']
        free_usdt = balance['free']['USDT']
        
        # PnL no realizado
        positions = balance['info']['positions']
        unrealized_pnl = sum([float(p['unrealizedProfit']) for p in positions])
        
        msg = (
            f"💰 *BALANCE WALLET*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 *Total Equity:* `${total_usdt:.2f}`\n"
            f"🔓 *Disponible:* `${free_usdt:.2f}`\n"
            f"📈 *PnL Flotante:* `${unrealized_pnl:.2f}`"
        )
        bot.reply_to(message, msg, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error leyendo Binance: {e}")

# --- COMANDOS DE CONTROL ---
def run_system_cmd(message, cmd):
    if not is_authorized(message): return
    bot.reply_to(message, f"⚙️ Ejecutando: `{cmd}`...", parse_mode="Markdown")
    try:
        subprocess.run(cmd.split(), check=True)
        bot.reply_to(message, "✅ Comando ejecutado con éxito.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['start_bot'])
def start_bot(m): run_system_cmd(m, "sudo systemctl start cpr_bot")

@bot.message_handler(commands=['stop_bot'])
def stop_bot(m): run_system_cmd(m, "sudo systemctl stop cpr_bot")

@bot.message_handler(commands=['restart'])
def restart_bot(m): run_system_cmd(m, "sudo systemctl restart cpr_bot")

# --- COMANDO: /logs ---
@bot.message_handler(commands=['logs'])
def logs_command(message):
    if not is_authorized(message): return
    try:
        # Últimas 15 líneas del servicio cpr_bot
        out = subprocess.check_output("journalctl -u cpr_bot -n 15 --no-pager", shell=True).decode()
        if len(out) > 4000: out = out[-4000:]
        bot.reply_to(message, f"📜 *LOGS (cpr_bot):*\n```\n{out}\n```", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error obteniendo logs: {e}")

# --- COMANDO: /panic (EMERGENCIA) ---
@bot.message_handler(commands=['panic'])
def panic_command(message):
    if not is_authorized(message): return
    
    # Confirmación simple
    msg = bot.reply_to(message, "💀 *ALERTA DE PÁNICO* 💀\nEstás a punto de cerrar TODAS las posiciones a mercado.\n\nEscribe 'CONFIRMAR' para ejecutar.")
    bot.register_next_step_handler(msg, process_panic)

def process_panic(message):
    if message.text.upper() != "CONFIRMAR":
        bot.reply_to(message, "🚫 Cancelado.")
        return

    bot.reply_to(message, "🔥 *EJECUTANDO CIERRE DE EMERGENCIA...*")
    
    try:
        positions = exchange_handler.get_open_positions()
        if not positions:
            bot.reply_to(message, "🤷‍♂️ No hay posiciones abiertas para cerrar.")
            return

        log = ""
        for pos in positions:
            symbol = pos['symbol']
            amount = abs(float(pos['amount']))
            side = pos['side']
            
            # Invertir lado para cerrar
            try:
                if side == 'long':
                    exchange_handler.exchange.create_market_sell_order(symbol, amount)
                else:
                    exchange_handler.exchange.create_market_buy_order(symbol, amount)
                log += f"✅ Closed {symbol}\n"
            except Exception as e:
                log += f"❌ Error {symbol}: {e}\n"
        
        bot.reply_to(message, f"📝 *REPORTE PÁNICO:*\n{log}")
        
        # Opcional: Detener el bot para que no vuelva a abrir
        subprocess.run(["sudo", "systemctl", "stop", "cpr_bot"])
        bot.reply_to(message, "🛑 Bot detenido por seguridad.")

    except Exception as e:
        bot.reply_to(message, f"❌ Error crítico: {e}")

# Bucle infinito
if __name__ == "__main__":
    print("🤖 Telegram Service Iniciado...")
    try:
        bot.infinity_polling()
    except Exception as e:
        print(f"Error polling: {e}")