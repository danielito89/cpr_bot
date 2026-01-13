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
            # --- FIX: NORMALIZACIÓN DE SÍMBOLOS ROBUSTA ---
            # Objetivo: Que 'WIF/USDT', 'WIFUSDT', 'WIF/USDT:USDT' sean iguales.
            target_clean = symbol.split(':')[0].replace('/', '').replace('1000', '') 
            
            current_pos = None
            for p in open_positions:
                pos_sym_clean = p['symbol'].split(':')[0].replace('/', '').replace('1000', '')
                if pos_sym_clean == target_clean:
                    current_pos = p
                    break
            # -----------------------------------------------

            # Descargar datos (Velas 4H)
            df = exchange.fetch_candles(symbol, timeframe=config.TIMEFRAME, limit=100)
            if df is None: continue
            
            # Calcular Indicadores
            df = strategy.calculate_indicators(df)
            
            # Construir Estado Actual para la Estrategia
            state_data = {'status': 'WAITING_BREAKOUT'}
            
            # SEGURIDAD EXTRA: Forzar estado si ya hay posición
            if current_pos:
                state_data = {
                    'status': 'IN_POSITION',
                    'entry_price': float(current_pos['entry_price']),
                    'stop_loss': 0.0, # Se actualizará abajo
                    'position_size_pct': 1.0,
                    'tp_partial': 999999,
                    'trailing_active': True,
                    'highest_price_post_tp': df['High'].iloc[-1]
                }

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
                        
                        # 2. Market Buy (ACTIVADO)
                        order = exchange.exchange.create_market_buy_order(symbol, qty)
                        
                        # 3. Stop Loss Order (ACTIVADO)
                        exchange.exchange.create_order(symbol, 'stop_market', 'sell', qty, params={'stopPrice': sl_price, 'reduceOnly': True})
                        
                        bot_telegram.send_entry(symbol, entry_price, qty, conf['tier'])
                else:
                    print(f"🚫 Señal ignorada {symbol}: {reason}")

            # CASO B: GESTIÓN DE POSICIÓN (Trailing / Salida)
            elif current_pos:
                
                # --- SUB-CASO 1: SALIDA TOTAL (TP, SL o Trailing Hit) ---
                if 'EXIT' in action:
                    print(f"👋 CERRANDO POSICIÓN: {symbol} ({action})")
                    
                    try:
                        # 1. Cerrar la posición a Mercado
                        qty = abs(float(current_pos['amount']))
                        exchange.exchange.create_market_sell_order(symbol, qty, params={'reduceOnly': True})
                        
                        # 2. Cancelar órdenes pendientes (SL viejo)
                        exchange.exchange.cancel_all_orders(symbol)
                        
                        # 3. Notificar
                        pnl = float(current_pos['pnl'])
                        close_price = df['Close'].iloc[-1]
                        bot_telegram.send_exit(symbol, action, pnl, close_price)
                        
                    except Exception as e:
                        print(f"❌ Error crítico cerrando {symbol}: {e}")
                        bot_telegram.send_msg(f"⚠️ FALLO AL CERRAR {symbol}: {e}")

                # --- SUB-CASO 2: ACTUALIZAR EL TRAILING STOP ---
                elif action == 'UPDATE_TRAILING':
                    new_sl = signal['new_sl']
                    print(f"🛡️ ACTUALIZANDO SL: {symbol} a {new_sl}")
                    
                    try:
                        # 1. Cancelar el Stop Loss anterior
                        exchange.exchange.cancel_all_orders(symbol)
                        
                        # 2. Crear el nuevo Stop Loss más arriba
                        qty = abs(float(current_pos['amount']))
                        exchange.exchange.create_order(
                            symbol, 
                            'stop_market', 
                            'sell', 
                            qty, 
                            params={'stopPrice': new_sl, 'reduceOnly': True}
                        )
                        
                        bot_telegram.send_trailing_update(symbol, new_sl)
                        
                    except Exception as e:
                        print(f"⚠️ Error actualizando SL para {symbol}: {e}")

        except Exception as e:
            print(f"❌ Error procesando {symbol}: {e}")

if __name__ == "__main__":
    bot_telegram.send_msg("🤖 <b>HYDRA BOT INICIADO</b> (Docker Mode)")
    
    # Limpiar señal de parada al inicio
    if os.path.exists("STOP_SIGNAL"):
        os.remove("STOP_SIGNAL")

    while True:
        # Check de Parada Suave
        if os.path.exists("STOP_SIGNAL"):
            print("🛑 SEÑAL DE PARADA DETECTADA. Cerrando bot...")
            bot_telegram.send_msg("🛑 <b>BOT DETENIDO POR COMANDO</b>")
            os.remove("STOP_SIGNAL")
            sys.exit(0)

        try:
            run_bot_cycle()
        except Exception as e:
            print(f"💥 Error crítico en main loop: {e}")
            time.sleep(60)
        
        # Esperar 5 minutos
        time.sleep(300)