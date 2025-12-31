import ccxt
import pandas as pd
import time
import sys
import os

# Ajuste de ruta para encontrar config si es necesario
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class BinanceClient:
    def __init__(self):
        self.client = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'), 
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future'  # Operamos en Futuros
            }
        })
        
        # Si estamos en Testnet/DryRun, a veces es útil cambiar la URL, 
        # pero para DryRun local usamos la API real para datos y no ejecutamos órdenes.
        # Si quisieras usar la Testnet de Binance real:
        # self.exchange.set_sandbox_mode(True) 

    def get_historical_data(self, symbol, interval=None, limit=300):
        """
        Descarga velas históricas y las devuelve como DataFrame.
        """
        if interval is None:
            interval = config.TIMEFRAME
            
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=interval, limit=limit)
            
            if not ohlcv:
                return None
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Convertir a float
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
                
            return df
            
        except Exception as e:
            print(f"❌ Error API (Data) {symbol}: {e}")
            return None

    def get_balance_usdt(self):
        """
        Obtiene el saldo disponible en USDT.
        """
        try:
            balance = self.exchange.fetch_balance()
            # En futuros, suele ser 'USDT' en 'free' o 'total'
            return float(balance['USDT']['free'])
        except Exception as e:
            print(f"⚠️ Error Balance: {e}")
            return 0.0

    def place_order(self, symbol, side, amount, order_type='MARKET', params={}):
        """
        Ejecuta una orden real.
        """
        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                params=params
            )
            return order
        except Exception as e:
            print(f"❌ Error Orden {symbol} {side}: {e}")
            return None

    def get_open_positions_symbols(self):
        """
        Devuelve una lista de símbolos con posiciones abiertas.
        """
        try:
            positions = self.exchange.fetch_positions()
            active_symbols = []
            for pos in positions:
                if float(pos['contracts']) > 0:
                    active_symbols.append(pos['symbol'])
            return active_symbols
        except Exception as e:
            # print(f"⚠️ Error Posiciones: {e}") 
            return []
            
# Alias por compatibilidad si algún script viejo llama a BinanceAPI
BinanceAPI = BinanceClient