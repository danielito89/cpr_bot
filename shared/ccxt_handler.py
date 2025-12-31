import ccxt
import os
import time
from dotenv import load_dotenv

load_dotenv()

class ExchangeHandler:
    _instance = None

    @classmethod
    def get_instance(cls):
        """Método estático para obtener la instancia única (Singleton)."""
        if cls._instance is None:
            # Aquí creamos la instancia si no existe
            cls._instance = cls()
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Inicialización privada."""
        print("🔌 Conectando a Binance Futures (Singleton)...")
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

    # Métodos de instancia
    def fetch_ohlcv(self, symbol, timeframe, limit=100, since=None):
        return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        return self.exchange.create_order(symbol, type, side, amount, price, params)
    
    def get_balance(self):
        return self.exchange.fetch_balance()
    
    def get_open_positions(self):
        # Helper útil para el Risk Manager
        bal = self.get_balance()
        if not bal: return []
        return [p for p in bal['info']['positions'] if float(p['positionAmt']) != 0]