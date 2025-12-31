import requests
import threading
import time
from datetime import datetime

class TelegramBot:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.last_heartbeat_time = 0

    def _send_request(self, message):
        """
        Método interno (privado) que ejecuta el envío.
        Se ejecuta en un hilo aparte para no frenar al bot.
        """
        def _target():
            try:
                payload = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "Markdown" # Permite usar negritas y monospaced
                }
                requests.post(self.base_url, json=payload, timeout=10)
            except Exception as e:
                print(f"⚠️ Error enviando a Telegram: {e}")

        # Lanzar en hilo separado (Fire & Forget)
        threading.Thread(target=_target).start()

    def send_msg(self, message):
        """Envío genérico (para errores o avisos simples)"""
        self._send_request(message)

    # --- 🟢 NUEVA ORDEN (Formato Bonito) ---
    def send_trade_entry(self, symbol, strategy, side, entry, sl, tp):
        emoji = "🚀" if side == 'LONG' else "📉"
        msg = (
            f"{emoji} *NUEVA ENTRADA: {symbol}*\n"
            f"🤖 Bot: `{strategy}`\n"
            f"🔹 Lado: *{side}*\n"
            f"💵 Precio: `{entry}`\n"
            f"🛑 Stop Loss: `{sl}`\n"
            f"🎯 Take Profit: `{tp}`\n"
            f"⏳ `Esperando desarrollo...`"
        )
        self._send_request(msg)

    # --- 🔄 ACTUALIZACIÓN (Trailing / Parciales) ---
    def send_trade_update(self, symbol, event, details):
        """
        event: 'PARTIAL', 'TRAILING', 'CLOSE'
        details: Texto libre con precios o PnL
        """
        if event == 'PARTIAL':
            icon = "💰"
            title = "TAKE PROFIT PARCIAL"
        elif event == 'TRAILING':
            icon = "🛡️"
            title = "TRAILING STOP SUBIDO"
        elif event == 'CLOSE':
            icon = "🏁"
            title = "POSICIÓN CERRADA"
        else:
            icon = "ℹ️"
            title = "UPDATE"

        msg = (
            f"{icon} *{title}: {symbol}*\n"
            f"{details}"
        )
        self._send_request(msg)

    # --- 💓 HEARTBEAT (Anti-Zombies) ---
    def send_daily_report(self, active_bot_name, scanned_pairs, open_positions_count):
        """
        Envía un mensaje para confirmar que el VPS no se colgó.
        """
        now = datetime.now().strftime("%d/%m %H:%M")
        msg = (
            f"💓 *REPORTE DE VIDA: {active_bot_name}*\n"
            f"📅 Hora: `{now}`\n"
            f"👀 Escaneando: `{len(scanned_pairs)}` pares\n"
            f"💼 Posiciones Abiertas: `{open_positions_count}`\n"
            f"✅ *Sistema Operativo y Escuchando*"
        )
        self._send_request(msg)