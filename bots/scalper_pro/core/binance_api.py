import ccxt
import os
import pandas as pd
import time
from dotenv import load_dotenv

# Cargar entorno
load_dotenv()

class BinanceAPI:
    def __init__(self):
        # Leer credenciales del .env
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_SECRET')

        if not api_key or not api_secret:
            print("⚠️ Advertencia: API KEYS no encontradas en .env (BinanceClient)")

        # Inicializar CCXT
        # OJO: Aquí usamos 'self.client', NO 'self.exchange'
        self.client = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
        try:
            self.client.load_markets()
            print("✅ Binance API (Legacy) Conectado.")
        except Exception as e:
            print(f"❌ Error conectando Binance Legacy: {e}")

    def get_balance_usdt(self):
        """Obtiene saldo disponible en USDT"""
        try:
            # CORRECCIÓN: Usamos self.client
            balance = self.client.fetch_balance()
            return float(balance['total']['USDT'])
        except Exception as e:
            print(f"⚠️ Error Balance: {e}")
            return 0.0

    def get_historical_data(self, symbol, timeframe='5m', limit=100):
        """Descarga velas"""
        try:
            ohlcv = self.client.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv: return None
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Convertir a float
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
                
            return df
        except Exception as e:
            print(f"❌ Error Data {symbol}: {e}")
            return None

    def place_order(self, symbol, side, amount, order_type='MARKET', params={}):
        """Ejecutar orden"""
        try:
            return self.client.create_order(symbol, order_type, side, amount, None, params)
        except Exception as e:
            print(f"❌ Error Order {symbol} {side}: {e}")
            return None

    def get_open_positions_symbols(self):
        """Devuelve lista de símbolos con posiciones abiertas"""
        try:
            bal = self.client.fetch_balance()
            positions = bal['info']['positions']
            active = [p['symbol'] for p in positions if float(p['positionAmt']) != 0]
            # Convertir formato si es necesario (ej: BTCUSDT -> BTC/USDT)
            # CCXT suele manejar esto, pero por si acaso devolvemos lista limpia
            return active 
        except Exception as e:
            print(f"⚠️ Error Open Positions: {e}")
            return []