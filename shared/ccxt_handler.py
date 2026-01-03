import ccxt
import os
import pandas as pd
from dotenv import load_dotenv

# Cargar variables de entorno al importar el módulo
load_dotenv()

class BinanceHandler:
    def __init__(self):
        self.api_key = os.getenv('BINANCE_API_KEY')
        self.api_secret = os.getenv('BINANCE_API_SECRET')
        
        if not self.api_key or not self.api_secret:
            raise ValueError("❌ CRÍTICO: No se encontraron API KEYS en .env")

        # Configuración para FUTUROS
        self.exchange = ccxt.binance({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',  # <--- IMPORTANTE: Operamos Futuros
                'adjustForTimeDifference': True
            }
        })

    def check_connection(self):
        try:
            self.exchange.load_markets()
            print("✅ Conexión a Binance Futures establecida.")
            return True
        except Exception as e:
            print(f"❌ Error conectando a Binance: {e}")
            return False

    def fetch_candles(self, symbol, timeframe='4h', limit=100):
        """Descarga velas recientes para el análisis"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # Limpieza de nombres de columnas para que coincida con strategy.py
            df.columns = [col.capitalize() for col in df.columns] 
            return df
        except Exception as e:
            print(f"⚠️ Error descargando velas para {symbol}: {e}")
            return None

    def get_balance(self):
        """Devuelve el balance libre en USDT"""
        try:
            balance = self.exchange.fetch_balance()
            return float(balance['USDT']['free'])
        except Exception as e:
            print(f"⚠️ Error obteniendo balance: {e}")
            return 0.0

    def get_open_positions(self):
        """Devuelve una lista de símbolos con posiciones abiertas"""
        try:
            # En CCXT futures, fetch_positions devuelve todo, hay que filtrar las que tienen size > 0
            positions = self.exchange.fetch_positions()
            active = []
            for pos in positions:
                if float(pos['contracts']) > 0:
                    active.append({
                        'symbol': pos['symbol'],
                        'amount': float(pos['contracts']),
                        'entry_price': float(pos['entryPrice']),
                        'pnl': float(pos['unrealizedPnl']),
                        'side': pos['side'] # 'long' o 'short'
                    })
            return active
        except Exception as e:
            print(f"⚠️ Error leyendo posiciones: {e}")
            return []

    def set_leverage(self, symbol, leverage):
        try:
            # Binance requiere quitar la barra para setear leverage en algunos endpoints, 
            # pero ccxt suele manejarlo. Probamos standard.
            self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            print(f"⚠️ No se pudo setear leverage para {symbol}: {e}")