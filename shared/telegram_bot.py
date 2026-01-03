import requests
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

class TelegramBot:
    def __init__(self):
        self.token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def _send(self, message):
        if not self.token or not self.chat_id:
            print("⚠️ Telegram no configurado en .env")
            return
        
        try:
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML' # Permite usar <b>negrita</b>
            }
            requests.post(self.base_url, data=payload, timeout=10)
        except Exception as e:
            print(f"❌ Error enviando Telegram: {e}")

    def send_entry(self, symbol, price, size, risk_tier):
        """Alerta de Entrada (Long)"""
        now = datetime.now().strftime('%H:%M')
        msg = (
            f"🚀 <b>HYDRA ENTRY ACTIVATED</b>\n\n"
            f"Asset: <b>{symbol}</b>\n"
            f"Price: <code>{price}</code>\n"
            f"Size: {size:.0f} coins\n"
            f"Tier: {risk_tier}\n"
            f"⏰ Time: {now}"
        )
        self._send(msg)

    def send_exit(self, symbol, reason, pnl_usd, close_price):
        """Alerta de Salida (TP o SL)"""
        emoji = "💰" if pnl_usd >= 0 else "🛑"
        msg = (
            f"{emoji} <b>HYDRA EXIT: {reason}</b>\n\n"
            f"Asset: <b>{symbol}</b>\n"
            f"PnL: <b>${pnl_usd:.2f}</b>\n"
            f"Exit Price: <code>{close_price}</code>"
        )
        self._send(msg)

    def send_trailing_update(self, symbol, new_sl):
        """Aviso de movimiento de Stop"""
        msg = f"🛡️ <b>Trailing Update</b> ({symbol})\nNew Stop Loss: <code>{new_sl}</code>"
        self._send(msg)
        
    def send_msg(self, text):
        """Mensaje genérico"""
        self._send(f"🤖 <b>SYSTEM MSG:</b> {text}")