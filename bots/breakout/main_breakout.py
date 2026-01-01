import sys
import os
import time
import json
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# --- 1. CONFIGURACIÓN DE RUTAS ---
current_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.abspath(os.path.join(current_dir, '../../.env'))
load_dotenv(dotenv_path)

# Añadir ruta raíz
sys.path.append(os.path.abspath(os.path.join(current_dir, '../..')))

# --- 2. IMPORTS ---
from shared.ccxt_handler import ExchangeHandler
from shared.risk_manager import RiskManager
from shared.telegram_bot import TelegramBot
from bots.breakout.strategy import BreakoutBotStrategy
import config

# --- HELPER FUNCTIONS DE ESTADO ---
def get_state_file(symbol):
    safe_symbol = symbol.replace('/', '_')
    return os.path.join(os.path.dirname(__file__), f'state_{safe_symbol}.json')

def load_state(symbol):
    file_path = get_state_file(symbol)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f: return json.load(f)
        except: return {}
    return {}

def save_state(symbol, state):
    with open(get_state_file(symbol), 'w') as f: json.dump(state, f, indent=4)

def main():
    print(f"🚀 Iniciando Breakout Bot MULTI-PAR (4H)...")
    
    # --- CARGAR LISTA DE PARES ---
    # Busca la lista específica, si no, usa la general
    pairs_to_scan = getattr(config, 'PAIRS_BREAKOUT', getattr(config, 'PAIRS', []))
    print(f"📋 Pares activos: {pairs_to_scan}")

    # --- TELEGRAM ---
    tg_token = os.getenv('TELEGRAM_TOKEN')
    tg_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    tg = None
    if tg_token and tg_chat_id:
        tg = TelegramBot(token=tg_token, chat_id=tg_chat_id)

    # --- INICIALIZACIÓN SINGLETON ---
    exchange = ExchangeHandler.get_instance() 
    risk_manager = RiskManager(exchange)
    
    # Estrategia Base
    strategy_logic = BreakoutBotStrategy()
    
    last_heartbeat_day = datetime.now().day

    # --- BUCLE PRINCIPAL ---
    while True:
        cycle_start_time = time.time()
        
        # --- 💓 HEARTBEAT DIARIO ---
        current_day = datetime.now().day
        if current_day != last_heartbeat_day:
            if tg:
                active_count = 0
                for s in pairs_to_scan:
                    st = load_state(s)
                    if st.get('status') == 'IN_POSITION': active_count += 1
                tg.send_daily_report(f"Breakout Multi", pairs_to_scan, active_count)
            last_heartbeat_day = current_day

        # --- ITERAR SOBRE CADA PAR ---
        for symbol in pairs_to_scan:
            try:
                # print(f"🔎 Analizando {symbol}...", end='\r') # Log silencioso
                
                # 1. Cargar Configuración Específica del Par
                profiles = getattr(config, 'RISK_PROFILES_BREAKOUT', getattr(config, 'RISK_PROFILES', {}))
                params = profiles.get(symbol, profiles.get('DEFAULT', {}))
                
                # Inyectar parámetros a la estrategia (Hot Swap)
                strategy_logic.sl_atr = params.get('sl_atr', 1.0)
                strategy_logic.tp_partial_atr = params.get('tp_partial_atr', 2.0)
                strategy_logic.trailing_dist_atr = params.get('trailing_dist_atr', 1.5)
                strategy_logic.vol_multiplier = params.get('vol_multiplier', 1.5)

                # 2. Cargar Estado
                state = load_state(symbol)
                
                # 3. Obtener Datos (Con Timeframe seguro)
                # Si pasamos un argumento (ej: '1h'), lo usamos. Si no, leemos config.
                if len(sys.argv) > 1:
                    tf = sys.argv[1]
                else:
                    tf = getattr(config, 'TIMEFRAME_BREAKOUT', '4h')

                print(f"⏱️ Timeframe seleccionado: {tf}")

                # SELECCIÓN DE LISTA DE PARES
                if tf == '1h':
                    pairs_to_scan = getattr(config, 'PAIRS_FAST', [])
                else:
                    pairs_to_scan = getattr(config, 'PAIRS_SLOW', [])
        
                print(f"📋 Pares activos: {pairs_to_scan}")
                
                ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=200) # 200 es suficiente
                
                if not ohlcv:
                    print(f"⚠️ {symbol}: No data.")
                    continue
                    
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                # --- 🔒 CANDLE LOCK POR MONEDA ---
                current_candle_time = str(df.index[-1])
                last_processed = state.get('last_processed_candle')
                
                if last_processed == current_candle_time:
                    # Ya procesamos esta moneda para esta vela 4H
                    continue

                print(f"⚡ {symbol}: Nueva vela {current_candle_time} detectada. Analizando...")

                # 4. Calcular Indicadores y Señal
                df = strategy_logic.calculate_indicators(df)
                signal = strategy_logic.get_signal(df, state)
                action = signal['action']
                
                if action != 'HOLD':
                    print(f"🔥 {symbol} ACCIÓN: {action}")
                    
                    # --- EJECUCIÓN ---
                    if action == 'ENTER_LONG':
                        if risk_manager.can_open_position(symbol):
                            print(f"✅ {symbol}: Entrando al mercado.")
                            # exchange.create_order(...)
                            
                            state.update({
                                'status': 'IN_POSITION',
                                'entry_price': signal['entry_price'],
                                'stop_loss': signal['stop_loss'],
                                'tp_partial': signal['tp_partial'],
                                'position_size_pct': 1.0,
                                'trailing_active': False,
                                'atr_at_breakout': state.get('atr_at_breakout', 0.0)
                            })
                            
                            if tg:
                                tg.send_trade_entry(symbol, "Breakout 4H", "LONG", 
                                                  f"{signal['entry_price']}", f"{signal['stop_loss']}", f"{signal['tp_partial']}")
                        else:
                            print(f"⛔ {symbol}: Risk Check Failed (Max positions?).")

                    elif action == 'EXIT_PARTIAL':
                        state.update({
                            'position_size_pct': 0.5,
                            'stop_loss': signal['new_sl'],
                            'trailing_active': True,
                            'highest_price_post_tp': signal['highest_price_post_tp']
                        })
                        if tg: tg.send_trade_update(symbol, 'PARTIAL', f"SL @ {signal['new_sl']}")

                    elif action in ['EXIT_SL', 'EXIT_TRAILING']:
                        state = {'status': 'COOLDOWN', 'last_exit_time': str(df.index[-1])}
                        if tg: tg.send_trade_update(symbol, 'CLOSE', f"Tipo: {action}")

                    elif 'new_status' in signal:
                        state['status'] = signal['new_status']
                        if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
                        if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']
                    
                    if action == 'UPDATE_TRAILING':
                        state['stop_loss'] = signal['new_sl']
                        state['highest_price_post_tp'] = signal['highest_price_post_tp']
                        if tg: tg.send_trade_update(symbol, 'TRAILING', f"New SL: {signal['new_sl']}")

                # Guardamos estado y marcamos vela procesada
                state['last_processed_candle'] = current_candle_time
                save_state(symbol, state)
                
            except Exception as e:
                print(f"❌ Error en {symbol}: {e}")
                # No frenamos el loop, pasamos al siguiente par
                continue
        
        # --- GESTIÓN DE TIEMPO DEL BUCLE ---
        # Como es multi-par, chequeamos más frecuentemente (cada 1 minuto)
        # para no perder tiempo si arrancamos desfasados.
        # El "Candle Lock" se encarga de no repetir operaciones.
        time.sleep(60)

if __name__ == "__main__":
    main()