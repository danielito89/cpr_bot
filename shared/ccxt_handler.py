# shared/ccxt_handler.py
import ccxt
import os
from dotenv import load_dotenv

# Cargar variables de entorno una sola vez
load_dotenv()

class ExchangeHandler:
    _instance = None

    @classmethod
    def get_instance(cls):
        """Patrón Singleton: Garantiza una sola conexión para toda la app"""
        if cls._instance is None:
            cls._instance = cls._create_exchange()
        return cls._instance

    @staticmethod
    def _create_exchange():
        exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'future'} # Asumimos Futuros
        })
        # Cargar mercados al inicio
        exchange.load_markets()
        return exchange

    @staticmethod
    def fetch_data(symbol, timeframe, limit=100):
        """Wrapper seguro para obtener velas"""
        exchange = ExchangeHandler.get_instance()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            print(f"❌ Error API ({symbol}): {e}")
            return []