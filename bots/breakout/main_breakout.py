import sys
import os
import time
import json
import pandas as pd
from datetime import datetime

# Añadir ruta raíz para importar shared
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.ccxt_handler import ExchangeHandler
from shared.risk_manager import RiskManager
from bots.breakout.strategy import BreakoutBotStrategy
import config

# --- CONFIGURACIÓN LOCAL ---
SYMBOL = 'SOL/USDT' # Empezamos con el ganador
STATE_FILE = os.path.join(os.path.dirname(__file__), f'state_{SYMBOL.replace("/","_")}.json')

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=4)

def main():
    print(f"🚀 Iniciando Breakout Bot para {SYMBOL} (4H)...")
    
    # Inicialización de Singletons
    exchange = ExchangeHandler() # Instancia única
    risk_manager = RiskManager(exchange)
    
    # Cargar Estrategia con Perfil de Riesgo
    params = config.RISK_PROFILES.get(SYMBOL, config.RISK_PROFILES['BTC/USDT'])
    strategy = BreakoutBotStrategy()
    strategy.sl_atr = params['sl_atr']
    strategy.tp_partial_atr = params['tp_partial_atr']
    strategy.trailing_dist_atr = params['trailing_dist_atr']
    strategy.vol_multiplier = params['vol_multiplier']
    
    while True:
        try:
            print(f"\n⏳ {datetime.now().strftime('%H:%M:%S')} - Analizando mercado...")
            
            # 1. Obtener Datos
            ohlcv = exchange.fetch_ohlcv(SYMBOL, config.TIMEFRAME_BREAKOUT, limit=300)
            if not ohlcv:
                print("⚠️ No data fetched. Retrying...")
                time.sleep(60)
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # 2. Calcular Indicadores
            df = strategy.calculate_indicators(df)
            
            # 3. Obtener Señal
            state = load_state()
            signal = strategy.get_signal(df, state)
            action = signal['action']
            
            if action != 'HOLD':
                print(f"⚡ SEÑAL GENERADA: {action}")
                
                # --- EJECUCIÓN ---
                if action == 'ENTER_LONG':
                    # Verificar Riesgo Global antes de entrar
                    if risk_manager.can_open_position(SYMBOL):
                        print("✅ Risk Check Passed. Executing Entry...")
                        # AQUI IRÍA exchange.create_order(...)
                        # Por ahora simulamos el fill para guardar estado
                        state.update({
                            'status': 'IN_POSITION',
                            'entry_price': signal['entry_price'],
                            'stop_loss': signal['stop_loss'],
                            'tp_partial': signal['tp_partial'],
                            'position_size_pct': 1.0,
                            'trailing_active': False
                        })
                        # Notificar Telegram aquí...
                    else:
                        print("⛔ Risk Check Failed (Max positions?). Skipping.")

                elif action == 'EXIT_PARTIAL':
                    print("💰 Taking Partial Profit...")
                    # exchange.create_order(sell 50%...)
                    state.update({
                        'position_size_pct': 0.5,
                        'stop_loss': signal['new_sl'],
                        'trailing_active': True,
                        'highest_price_post_tp': signal['highest_price_post_tp']
                    })
                
                elif action in ['EXIT_SL', 'EXIT_TRAILING']:
                    print("🛑 Closing Position...")
                    # exchange.create_order(close all...)
                    state = {'status': 'COOLDOWN', 'last_exit_time': str(df.index[-1])}

                # Actualización de estados intermedios (Waiting, Trailing Update)
                elif 'new_status' in signal:
                    state['status'] = signal['new_status']
                    if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
                    if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']
                
                if action == 'UPDATE_TRAILING':
                    state['stop_loss'] = signal['new_sl']
                    state['highest_price_post_tp'] = signal['highest_price_post_tp']
                    print(f"🔄 Trailing Stop subido a {signal['new_sl']}")

                save_state(state)
            
            else:
                print(f"💤 Estado: {state.get('status', 'WAITING')} | Precio: {df['Close'].iloc[-1]}")

        except Exception as e:
            print(f"❌ Error Main Loop: {e}")
        
        # Esperar 5 minutos antes del siguiente chequeo
        # (Suficiente para 4H, no saturamos API)
        time.sleep(300)

if __name__ == "__main__":
    main()