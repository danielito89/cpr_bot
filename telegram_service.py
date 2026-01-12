import telebot
import os
import sys
import time
from dotenv import load_dotenv

# Importamos nuestras herramientas compartidas
from shared.ccxt_handler import BinanceHandler
import config

# --- CONFIGURACIÓN ---
# En Docker, la ruta raíz es /app directamente
BASE_PATH = "/app"
load_dotenv() # Docker ya carga las variables, pero esto asegura compatibilidad local

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Inicializamos el bot
try:
    bot = telebot.TeleBot(TOKEN)
    exchange_handler = BinanceHandler()
    print("✅ Telegram Service: Modulos cargados correctamente.")
except Exception as e:
    print(f"🔥 Error cargando dependencias de Telegram: {e}")

# Restringir acceso solo a TI (Seguridad)
def is_authorized(message):
    # Convertimos a string por seguridad
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
        "☁️ _Ejecutando en Contenedor (Alemania)_\n\n"
        "📊 *ESTADO*\n"
        "/status - Ver estado y posiciones\n"
        "/balance - Ver saldo USDT en Binance\n\n"
        "⚙️ *SISTEMA*\n"
        "Para ver logs o reiniciar, usa la terminal:\n"
        "`docker compose logs -f`\n"
        "`docker compose restart`\n\n"
        "💀 *EMERGENCIA*\n"
        "/panic - ⚠️ CERRAR TODO A MERCADO"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# --- COMANDO: /status ---
@bot.message_handler(commands=['status'])
def status_command(message):
    if not is_authorized(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    
    # 1. Estado del Servicio
    # En Docker, si este mensaje responde, el contenedor 'hydra_bot' debería estar corriendo
    # porque comparten el mismo docker-compose.
    service_status = "🟢 ONLINE (Docker)"
    
    # 2. Leer Posiciones Abiertas
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
        
        # Intentamos calcular PnL flotante si hay info
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

# --- COMANDOS OBSOLETOS EN DOCKER ---
@bot.message_handler(commands=['logs', 'start_bot', 'stop_bot', 'restart'])
def docker_notice(message):
    if not is_authorized(message): return
    bot.reply_to(message, 
        "⚠️ *Comando no disponible en Docker*\n\n"
        "Para gestionar el bot, usa la terminal SSH:\n"
        "🔹 Logs: `docker compose logs -f --tail=50`\n"
        "🔹 Reiniciar: `docker compose restart`",
        parse_mode="Markdown")

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
            
            # Cerrar posición (Invertir lado)
            try:
                # Nota: En producción real, binance tiene endpoints específicos para cerrar,
                # pero lanzar orden de mercado contraria funciona igual.
                if side == 'long':
                    exchange_handler.exchange.create_market_sell_order(symbol, amount, params={'reduceOnly': True})
                else:
                    exchange_handler.exchange.create_market_buy_order(symbol, amount, params={'reduceOnly': True})
                log += f"✅ Closed {symbol}\n"
            except Exception as e:
                log += f"❌ Error {symbol}: {e}\n"
        
        bot.reply_to(message, f"📝 *REPORTE PÁNICO:*\n{log}")
        bot.reply_to(message, "⚠️ Recuerda detener el contenedor manualmente si es necesario.")

    except Exception as e:
        bot.reply_to(message, f"❌ Error crítico: {e}")

# Bucle infinito
if __name__ == "__main__":
    print("🤖 Telegram Service Iniciado... (Modo Docker)")
    try:
        bot.infinity_polling()
    except Exception as e:
        print(f"Error polling: {e}")