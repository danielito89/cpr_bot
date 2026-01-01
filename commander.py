import telebot
import subprocess
import os
import json
import glob
import time
from dotenv import load_dotenv
import ccxt

# --- CONFIGURACIÓN ---
BASE_PATH = "/home/ubuntu/bot_cpr"
load_dotenv(os.path.join(BASE_PATH, ".env"))

TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Inicializamos el bot
bot = telebot.TeleBot(TOKEN)

# Restringir acceso solo a TI (Seguridad)
def is_authorized(message):
    return str(message.chat.id) == str(CHAT_ID)

# --- COMANDO: /start (Bienvenida) ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_authorized(message): return
    help_text = (
        "🐉 *HYDRA COMMANDER V1.0*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 *CONTROLES:*\n"
        "/status - Estado de bots y posiciones\n"
        "/balance - Saldo total en Binance\n"
        "/fast_on - Iniciar Breakout FAST (1H)\n"
        "/fast_off - Detener Breakout FAST\n"
        "/slow_on - Iniciar Breakout SLOW (4H)\n"
        "/slow_off - Detener Breakout SLOW\n"
        "/logs - Ver últimos errores/logs\n"
        "/reset - ⚠️ Reinicio de Emergencia (Limpia Estados)"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# --- COMANDO: /status ---
@bot.message_handler(commands=['status'])
def status_command(message):
    if not is_authorized(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    
    # 1. Chequear Systemd
    def check_service(name):
        res = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
        return "🟢 ON" if res.stdout.strip() == "active" else "🔴 OFF"

    fast_status = check_service("breakout_fast")
    slow_status = check_service("breakout_slow")
    
    # 2. Leer Posiciones Abiertas
    positions_txt = ""
    files = glob.glob(os.path.join(BASE_PATH, "bots", "breakout", "state_*.json"))
    active_count = 0
    
    for f in files:
        try:
            with open(f, 'r') as file:
                data = json.load(file)
                if data.get('status') == 'IN_POSITION':
                    active_count += 1
                    symbol = os.path.basename(f).replace('state_', '').replace('.json', '').replace('_', '/')
                    pnl = "N/A" # Aquí podrías calcular PnL si tuvieras precio actual
                    positions_txt += f"• *{symbol}*: Entrada `{data.get('entry_price')}`\n"
        except: pass

    if not positions_txt: positions_txt = "_Sin posiciones abiertas_"

    msg = (
        f"📊 *ESTADO DEL SISTEMA*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🚀 *FAST (1H):* {fast_status}\n"
        f"🐢 *SLOW (4H):* {slow_status}\n\n"
        f"💼 *Posiciones ({active_count}):*\n"
        f"{positions_txt}"
    )
    bot.reply_to(message, msg, parse_mode="Markdown")

# --- COMANDO: /balance ---
@bot.message_handler(commands=['balance'])
def balance_command(message):
    if not is_authorized(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    
    try:
        api_key = os.getenv('BINANCE_API_KEY')
        secret = os.getenv('BINANCE_SECRET')
        exchange = ccxt.binance({
            'apiKey': api_key, 'secret': secret, 
            'options': {'defaultType': 'future'}
        })
        balance = exchange.fetch_balance()
        total_usdt = balance['total']['USDT']
        free_usdt = balance['free']['USDT']
        
        # PnL no realizado
        positions = balance['info']['positions']
        unrealized_pnl = sum([float(p['unrealizedProfit']) for p in positions])
        
        msg = (
            f"💰 *BALANCE WALLET*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 *Total:* `${total_usdt:.2f}`\n"
            f"🔓 *Libre:* `${free_usdt:.2f}`\n"
            f"📈 *PnL Flotante:* `${unrealized_pnl:.2f}`"
        )
        bot.reply_to(message, msg, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error leyendo Binance: {e}")

# --- COMANDOS DE CONTROL (ON/OFF) ---
def manage_service(message, service, action):
    if not is_authorized(message): return
    bot.reply_to(message, f"⚙️ Ejecutando: {action} {service}...")
    
    cmd = "start" if action == "on" else "stop"
    try:
        subprocess.run(["sudo", "systemctl", cmd, service], check=True)
        bot.reply_to(message, f"✅ {service} ahora está {cmd.upper()}.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['fast_on'])
def fast_on(m): manage_service(m, "breakout_fast", "on")

@bot.message_handler(commands=['fast_off'])
def fast_off(m): manage_service(m, "breakout_fast", "off")

@bot.message_handler(commands=['slow_on'])
def slow_on(m): manage_service(m, "breakout_slow", "on")

@bot.message_handler(commands=['slow_off'])
def slow_off(m): manage_service(m, "breakout_slow", "off")

# --- COMANDO: /logs (Ver qué pasa) ---
@bot.message_handler(commands=['logs'])
def logs_command(message):
    if not is_authorized(message): return
    # Obtener ultimas 15 lineas combinadas
    cmd = "journalctl -u breakout_fast -u breakout_slow -n 15 --no-pager"
    out = subprocess.check_output(cmd, shell=True).decode()
    # Cortar si es muy largo para Telegram
    if len(out) > 4000: out = out[-4000:]
    bot.reply_to(message, f"📜 *ULTIMOS LOGS:*\n```\n{out}\n```", parse_mode="Markdown")

# --- COMANDO: /reset (Panico) ---
@bot.message_handler(commands=['reset'])
def reset_command(message):
    if not is_authorized(message): return
    msg = bot.reply_to(message, "⚠️ *INICIANDO PROTOCOLO DE RESET* ⚠️\n1. Deteniendo bots...")
    
    subprocess.run(["sudo", "systemctl", "stop", "breakout_fast", "breakout_slow"])
    
    bot.edit_message_text("⚠️ *RESET EN PROCESO* ⚠️\n2. Eliminando memorias de estado...", chat_id=message.chat.id, message_id=msg.message_id, parse_mode="Markdown")
    
    # Borrar JSONs
    files = glob.glob(os.path.join(BASE_PATH, "bots", "breakout", "state_*.json"))
    for f in files: os.remove(f)
    
    bot.edit_message_text("⚠️ *RESET EN PROCESO* ⚠️\n3. Reactivando sistemas...", chat_id=message.chat.id, message_id=msg.message_id, parse_mode="Markdown")
    
    subprocess.run(["sudo", "systemctl", "start", "breakout_fast", "breakout_slow"])
    
    bot.edit_message_text("✅ *SISTEMA REINICIADO Y LIMPIO*\nLos bots comenzarán a escanear desde cero.", chat_id=message.chat.id, message_id=msg.message_id, parse_mode="Markdown")

# Bucle infinito
print("🤖 Hydra Commander Iniciado...")
bot.infinity_polling()