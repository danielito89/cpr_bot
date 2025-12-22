# core/binance_api.py
import ccxt
import pandas as pd
import time
import sys
import os

# Añadimos el directorio padre al path para importar config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class BinanceAPI:
    def __init__(self):
        try:
            self.exchange = ccxt.binance({
                'apiKey': config.API_KEY,
                'secret': config.API_SECRET,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future'  # IMPORTANTE: Operar en Futuros
                }
            })
            # Cargar mercados para tener precisión de precios/cantidades
            self.exchange.load_markets()
            print("✅ Conexión con Binance Futures establecida.")
        except Exception as e:
            print(f"❌ Error conectando a Binance: {e}")
            sys.exit(1)

    def fetch_ohlcv(self, limit=500):
        """Descarga velas recientes"""
        try:
            # Fetch data
            ohlcv = self.exchange.fetch_ohlcv(config.SYMBOL, config.TIMEFRAME, limit=limit)
            
            # Convertir a DataFrame
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Asegurar tipos float
            cols = ['open', 'high', 'low', 'close', 'volume']
            df[cols] = df[cols].astype(float)
            
            return df
        except Exception as e:
            print(f"⚠️ Error descargando velas: {e}")
            return None

    def get_balance_usdt(self):
        """Obtiene el saldo disponible en USDT"""
        try:
            balance = self.exchange.fetch_balance()
            return float(balance['USDT']['free'])
        except Exception as e:
            print(f"⚠️ Error obteniendo saldo: {e}")
            return 0.0

    def get_position(self):
        """Revisa si tenemos una posición abierta en el par"""
        try:
            positions = self.exchange.fetch_positions([config.SYMBOL])
            for pos in positions:
                if float(pos['contracts']) > 0:
                    return {
                        'side': pos['side'].upper(), # 'LONG' o 'SHORT'
                        'amount': float(pos['contracts']),
                        'entry_price': float(pos['entryPrice']),
                        'pnl': float(pos['unrealizedPnl'])
                    }
            return None # No hay posición
        except Exception as e:
            print(f"⚠️ Error obteniendo posición: {e}")
            return None

    def place_order(self, side, amount, order_type='market', price=None, params={}):
        # Buscar 'symbol' en params, si no está usar el de config
        symbol = params.get('symbol', config.SYMBOL)
        """
        Ejecuta una orden.
        side: 'buy' o 'sell'
        """
        if config.DRY_RUN:
            print(f"🧪 DRY RUN: Simulando orden {side.upper()} de {amount} {config.SYMBOL}")
            return {'id': 'simulated_id', 'status': 'closed', 'price': price}

        try:
            if order_type == 'market':
                order = self.exchange.create_market_order(config.SYMBOL, side, amount, params)
            elif order_type == 'limit':
                order = self.exchange.create_limit_order(config.SYMBOL, side, amount, price, params)
            
            print(f"🚀 Orden {side.upper()} ejecutada: {amount} contratos.")
            return order
        except Exception as e:
            print(f"❌ Error ejecutando orden: {e}")
            return None

    def close_position(self, current_position):
        """Cierra cualquier posición abierta a mercado"""
        if not current_position:
            return
        
        side = 'sell' if current_position['side'] == 'LONG' else 'buy'
        amount = current_position['amount']
        
        print(f"🔄 Cerrando posición {current_position['side']}...")
        return self.place_order(side, amount, 'market')