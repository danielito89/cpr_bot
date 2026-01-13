import telebot
import subprocess
import os
import sys
import time
from dotenv import load_dotenv

# Importamos nuestras herramientas compartidas
# Ajusta la ruta si es necesario, pero en Docker con PYTHONPATH=. suele funcionar directo
from shared.ccxt_handler import BinanceHandler
import config

# --- CONFIGURACIÓN ---
BASE_PATH = "/app" # Ruta estándar en Docker
load_dotenv()

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Inicializamos dependencias
try:
    bot = telebot.TeleBot(TOKEN)
    exchange_handler = BinanceHandler()
    print("✅ Telegram Service: Modulos cargados correctamente.")
except Exception as e:
    print(f"🔥 Error cargando dependencias de Telegram: {e}")

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
        "🐉 *HYDRA DOCKER CONTROL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "☁️ _Ejecutando en Contenedor (Producción)_\n\n"
        "📊 *ESTADO*\n"
        "/status - Ver estado y posiciones\n"
        "/balance - Ver saldo USDT en Binance\n\n"
        "⚙️ *CONTROL*\n"
        "/stop_bot - 🛑 Detener Hydra (Soft Stop)\n\n"
        "💀 *EMERGENCIA*\n"
        "/panic - ⚠️ CERRAR TODO A MERCADO"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# --- COMANDO: /status ---
@bot.message_handler(commands=['status'])
def status_command(message):
    if not is_authorized(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    
    # En Docker, asumimos que si este bot responde, el sistema está vivo.
    # Podríamos chequear si existe el proceso python main_breakout.py, pero simplificamos.
    service_status = "🟢 ONLINE (Docker)"
    
    # Leer Posiciones Abiertas
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
        positions_txt = f"⚠️ Error API Binance: {str(e)}"
        active_count = "?"

    msg = (
        f"📊 *ESTADO DEL SISTEMA*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🐳 *Contenedor:* {service_status}\n\n"
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
        balance = exchange_handler.exchange.fetch_balance()
        total_usdt = balance['total']['USDT']
        free_usdt = balance['free']['USDT']
        
        unrealized_pnl = 0.0
        if 'positions' in balance['info']:
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

# --- COMANDO: /stop_bot (SOFT STOP EN DOCKER) ---
@bot.message_handler(commands=['stop_bot'])
def stop_bot(m):
    if not is_authorized(m): return
    try:
        # Creamos un archivo bandera para que el bot principal se detenga solo
        with open("STOP_SIGNAL", "w") as f:
            f.write("STOP")
        bot.reply_to(m, "🛑 SEÑAL DE PARADA ENVIADA. El bot se detendrá en el próximo ciclo (máx 5 min).")
    except Exception as e:
        bot.reply_to(m, f"❌ Error creando señal de parada: {e}")

# --- COMANDO: /panic (EMERGENCIA) ---
@bot.message_handler(commands=['panic'])
def panic_command(message):
    if not is_authorized(message): return
    
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
            bot.reply_to(message, "🤷‍♂️ No hay posiciones abiertas.")
            return

        log = ""
        for pos in positions:
            symbol = pos['symbol']
            amount = abs(float(pos['amount']))
            side = pos['side']
            
            try:
                # Invertir lado para cerrar
                if side == 'long':
                    exchange_handler.exchange.create_market_sell_order(symbol, amount, params={'reduceOnly': True})
                else:
                    exchange_handler.exchange.create_market_buy_order(symbol, amount, params={'reduceOnly': True})
                log += f"✅ Closed {symbol}\n"
            except Exception as e:
                log += f"❌ Error {symbol}: {e}\n"
        
        bot.reply_to(message, f"📝 *REPORTE PÁNICO:*\n{log}")
        bot.reply_to(message, "⚠️ Recuerda que el bot sigue corriendo. Usa /stop_bot si quieres detenerlo.")

    except Exception as e:
        bot.reply_to(message, f"❌ Error crítico: {e}")

if __name__ == "__main__":
    print("🤖 Telegram Service Iniciado... (Modo Docker)")
    try:
        bot.infinity_polling()
    except Exception as e:
        print(f"Error polling: {e}")