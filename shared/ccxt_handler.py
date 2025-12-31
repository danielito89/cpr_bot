import ccxt
import os
import time
from dotenv import load_dotenv

load_dotenv()

class ExchangeHandler:
    _instance = None

    def __new__(cls):
        """Singleton Pattern: Garantiza una única conexión al Exchange."""
        if cls._instance is None:
            cls._instance = super(ExchangeHandler, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Inicialización privada (se ejecuta una sola vez)."""
        print("🔌 Conectando a Binance Futures...")
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        try:
            self.exchange.load_markets()
            print("✅ Mercados cargados correctamente.")
        except Exception as e:
            print(f"❌ Error crítico conectando a Binance: {e}")

    def fetch_ohlcv(self, symbol, timeframe, limit=100, since=None):
        """Wrapper de instancia con manejo de errores básico."""
        try:
            return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)
        except Exception as e:
            print(f"⚠️ Error fetch_ohlcv ({symbol}): {e}")
            return []

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        """Wrapper para órdenes."""
        try:
            return self.exchange.create_order(symbol, type, side, amount, price, params)
        except Exception as e:
            print(f"❌ Error create_order ({symbol}): {e}")
            return None

    def get_balance(self):
        try:
            return self.exchange.fetch_balance()
        except Exception as e:
            print(f"⚠️ Error fetch_balance: {e}")
            return None