# core/binance_api.py
import ccxt
import pandas as pd
import sys
import os

# Añadimos path para config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class BinanceAPI:
    def __init__(self):
        try:
            self.exchange = ccxt.binance({
                'apiKey': config.API_KEY,
                'secret': config.API_SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            })
            self.exchange.load_markets()
            print("✅ Conexión Binance Futures (Multipair) OK.")
        except Exception as e:
            print(f"❌ Error conexión: {e}")
            sys.exit(1)

    def fetch_ohlcv(self, symbol, limit=500):
        """Descarga velas para un par específico"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, config.TIMEFRAME, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            cols = ['open', 'high', 'low', 'close', 'volume']
            df[cols] = df[cols].astype(float)
            return df
        except Exception as e:
            print(f"⚠️ Error data {symbol}: {e}")
            return None

    def get_balance_usdt(self):
        try:
            bal = self.exchange.fetch_balance()
            return float(bal['USDT']['free'])
        except: return 0.0

    def get_position(self, symbol):
        """Busca la posición específica de un par"""
        try:
            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                # CCXT a veces devuelve info extra, filtramos por contrato > 0
                if float(pos['contracts']) > 0:
                    return {
                        'symbol': symbol,
                        'side': pos['side'].upper(),
                        'amount': float(pos['contracts']),
                        'entry_price': float(pos['entryPrice']),
                        'pnl': float(pos['unrealizedPnl'])
                    }
            return None # Flat
        except Exception as e:
            print(f"⚠️ Error get_position {symbol}: {e}")
            return None

    def place_order(self, symbol, side, amount, order_type='market', params={}):
        """Ejecuta orden en el par indicado"""
        if config.DRY_RUN:
            print(f"🧪 DRY: {side} {amount} {symbol}")
            return {'id': 'sim', 'average': 0}

        try:
            if order_type == 'market':
                return self.exchange.create_market_order(symbol, side, amount, params)
            elif order_type == 'limit':
                # Nota: precio debe venir en params o argumento aparte, 
                # pero para scalper market usamos params para stop loss orders
                return self.exchange.create_order(symbol, order_type, side, amount, None, params)
            elif order_type == 'STOP_MARKET':
                 return self.exchange.create_order(symbol, 'STOP_MARKET', side, amount, None, params)
        except Exception as e:
            print(f"❌ Error orden {symbol}: {e}")
            return None

    def close_position(self, position):
        """Cierra la posición recibida"""
        if not position: return
        symbol = position['symbol']
        side = 'sell' if position['side'] == 'LONG' else 'buy'
        amount = position['amount']
        print(f"🔄 Cerrando {symbol}...")
        
        # 1. Cancelar órdenes abiertas (TP/SL pendientes)
        try: self.exchange.cancel_all_orders(symbol)
        except: pass
        
        # 2. Cerrar mercado
        return self.place_order(symbol, side, amount, 'market', {'reduceOnly': True})