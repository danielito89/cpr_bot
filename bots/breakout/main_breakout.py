import time
import sys
import os
from datetime import datetime
import pandas as pd

# Ajustar path para imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(PROJECT_ROOT)

import config
from bots.breakout.strategy import BreakoutBotStrategy
from shared.ccxt_handler import BinanceHandler
from shared.telegram_bot import TelegramBot
from shared.risk_manager import RiskManager

# --- INICIALIZACIÓN ---
bot_telegram = TelegramBot()
exchange = BinanceHandler()
strategy = BreakoutBotStrategy()

def get_btc_regime():
    """Chequea si BTC está alcista (Filtro Macro)"""
    try:
        df = exchange.fetch_candles(config.BTC_SYMBOL, timeframe='4h', limit=205)
        if df is None or len(df) < 200: return False
        
        sma200 = df['Close'].rolling(window=200).mean().iloc[-1]
        current_price = df['Close'].iloc[-1]
        
        return current_price > sma200
    except Exception as e:
        print(f"⚠️ Error checkeando BTC Regime: {e}")
        return False # Ante la duda, conservador

def run_bot_cycle():
    """Un ciclo de ejecución (se repite cada X minutos)"""
    print(f"\n🔄 Ciclo iniciado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. Actualizar Datos de Cuenta
    balance = exchange.get_balance()
    open_positions = exchange.get_open_positions() # Lista de dicts
    risk_manager = RiskManager(balance)
    
    macro_bullish = get_btc_regime()
    print(f"💰 Balance: ${balance:.2f} | 🐂 Macro Bullish: {macro_bullish} | 🔓 Open Positions: {len(open_positions)}")

    # 2. Iterar sobre el Portfolio Configurado
    for symbol, conf in config.PAIRS_CONFIG.items():
        try:
            # ¿Tenemos posición abierta en este par?
            current_pos = next((p for p in open_positions if p['symbol'] == symbol.replace('/', '')), None)
            
            # Descargar datos (Velas 4H)
            df = exchange.fetch_candles(symbol, timeframe=config.TIMEFRAME, limit=100)
            if df is None: continue
            
            # Calcular Indicadores
            df = strategy.calculate_indicators(df)
            
            # Construir Estado Actual para la Estrategia
            state_data = {'status': 'WAITING_BREAKOUT'}
            
            if current_pos:
                state_data = {
                    'status': 'IN_POSITION',
                    'entry_price': current_pos['entry_price'],
                    'stop_loss': 0.0, # TODO: Leer SL real de orden abierta si es posible, o gestionarlo interno
                    'position_size_pct': 1.0, # Asumimos 100% por ahora
                    'tp_partial': 999999, # Se recalcula o se mantiene
                    'trailing_active': True, 
                    'highest_price_post_tp': df['High'].iloc[-1] # Simplificación para trailing
                }
                # Aquí deberíamos tener una base de datos local (SQLite/JSON) para persistir 
                # el SL exacto y el estado del TP parcial. 
                # Para la V1, asumimos que la estrategia recalcula niveles dinámicos.

            # Obtener Señal
            window = df.iloc[-60:] # Ventana suficiente
            signal = strategy.get_signal(window, state_data)
            
            action = signal['action']
            
            # --- EJECUCIÓN DE LÓGICA ---
            
            # CASO A: ENTRADA (Solo si Macro es Bullish y no hay posición)
            if action == 'ENTER_LONG' and not current_pos and macro_bullish:
                allowed, reason = risk_manager.can_open_position(open_positions, symbol)
                
                if allowed:
                    entry_price = signal['entry_price']
                    sl_price = signal['stop_loss']
                    
                    qty, notional = risk_manager.calculate_position_size(symbol, entry_price, sl_price)
                    
                    if qty > 0:
                        print(f"🚀 OPENING LONG: {symbol} Size: {qty}")
                        
                        # 1. Set Leverage
                        exchange.set_leverage(symbol.replace('/', ''), conf['leverage'])
                        
                        # 2. Market Buy
                        # order = exchange.exchange.create_market_buy_order(symbol, qty)
                        # TODO: Descomentar linea de arriba para LIVE
                        
                        # 3. Stop Loss Order
                        # exchange.exchange.create_order(symbol, 'stop_market', 'sell', qty, params={'stopPrice': sl_price})
                        
                        bot_telegram.send_entry(symbol, entry_price, qty, conf['tier'])
                else:
                    print(f"🚫 Señal ignorada {symbol}: {reason}")

            # CASO B: GESTIÓN DE POSICIÓN (Trailing / Salida)
            elif current_pos:
                # Si la estrategia dice EXIT
                if 'EXIT' in action:
                    print(f"👋 CERRANDO POSICIÓN: {symbol} ({action})")
                    # exchange.exchange.create_market_sell_order(symbol, current_pos['amount'])
                    # bot_telegram.send_exit(...)
                    pass
                
                # Si la estrategia dice UPDATE TRAILING
                elif action == 'UPDATE_TRAILING':
                    new_sl = signal['new_sl']
                    print(f"🛡️ ACTUALIZANDO SL: {symbol} a {new_sl}")
                    # Cancelar SL anterior y poner uno nuevo
                    # bot_telegram.send_trailing_update(symbol, new_sl)

        except Exception as e:
            print(f"❌ Error procesando {symbol}: {e}")
            # bot_telegram.send_msg(f"Error en {symbol}: {e}")

if __name__ == "__main__":
    bot_telegram.send_msg("🤖 <b>HYDRA BOT INICIADO</b> (Production Mode)")
    while True:
        try:
            run_bot_cycle()
        except Exception as e:
            print(f"💥 Error crítico en main loop: {e}")
            time.sleep(60)
        
        # Esperar 5 minutos para el siguiente ciclo
        time.sleep(300)