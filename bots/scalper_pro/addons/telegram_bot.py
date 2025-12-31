import requests
import threading

class TelegramBot:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send_msg(self, message):
        """Envía mensaje en un hilo separado para no bloquear el trading"""
        def _send():
            try:
                payload = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "Markdown"
                }
                requests.post(self.base_url, json=payload, timeout=5)
            except Exception as e:
                print(f"⚠️ Error Telegram: {e}")

        threading.Thread(target=_send).start()