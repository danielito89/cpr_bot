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

# --- CONFIGURACIÓN LOCAL ---
# Puedes pasar el símbolo por argumento: python3 main_breakout.py SOL/USDT
SYMBOL = sys.argv[1] if len(sys.argv) > 1 else 'SOL/USDT'
STATE_FILE = os.path.join(os.path.dirname(__file__), f'state_{SYMBOL.replace("/","_")}.json')

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f: return json.load(f)
        except: return {} # Si el archivo está corrupto
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=4)

def main():
    print(f"🚀 Iniciando Breakout Bot para {SYMBOL} (4H)...")
    
    # --- TELEGRAM ---
    tg_token = os.getenv('TELEGRAM_TOKEN')
    tg_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    tg = None
    if tg_token and tg_chat_id:
        tg = TelegramBot(token=tg_token, chat_id=tg_chat_id)

    # --- INICIALIZACIÓN (CORREGIDA: Singleton) ---
    # 1. Obtenemos la instancia ÚNICA compartida
    exchange = ExchangeHandler.get_instance() 
    risk_manager = RiskManager(exchange)
    
    # --- ESTRATEGIA ---
    # Cargar perfil de riesgo específico
    profiles = getattr(config, 'RISK_PROFILES_BREAKOUT', getattr(config, 'RISK_PROFILES', {}))
    params = profiles.get(SYMBOL, profiles.get('DEFAULT', {}))
    
    strategy = BreakoutBotStrategy()
    strategy.sl_atr = params.get('sl_atr', 1.0)
    strategy.tp_partial_atr = params.get('tp_partial_atr', 2.0)
    strategy.trailing_dist_atr = params.get('trailing_dist_atr', 1.5)
    strategy.vol_multiplier = params.get('vol_multiplier', 1.5)
    
    print(f"⚙️ Config cargada: Vol x{strategy.vol_multiplier} | SL {strategy.sl_atr}ATR")
    
    last_heartbeat_day = datetime.now().day

    while True:
        try:
            now_str = datetime.now().strftime('%H:%M:%S')
            
            # --- 💓 HEARTBEAT DIARIO ---
            current_day = datetime.now().day
            if current_day != last_heartbeat_day:
                if tg:
                    st = load_state()
                    active = 1 if st.get('status') == 'IN_POSITION' else 0
                    tg.send_daily_report(f"Breakout {SYMBOL}", [SYMBOL], active)
                last_heartbeat_day = current_day

            # --- 1. OBTENER DATOS ---
            # Pedimos suficiente historial para EMA200
            ohlcv = exchange.fetch_ohlcv(SYMBOL, config.TIMEFRAME_BREAKOUT, limit=300)
            if not ohlcv:
                print("⚠️ No data. Retrying in 1 min...")
                time.sleep(60)
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # --- 🔒 CANDLE LOCK (FIX CRÍTICO) ---
            # Identificador único de la última vela cerrada
            current_candle_time = str(df.index[-1])
            state = load_state()
            last_processed = state.get('last_processed_candle')
            
            # Si ya procesamos esta vela, a dormir (ahorramos API y CPU)
            if last_processed == current_candle_time:
                print(f"💤 {now_str} Vela {current_candle_time} ya procesada. Durmiendo...")
                time.sleep(900) # 15 minutos de siesta
                continue

            print(f"\n🔎 {now_str} Analizando Vela Nueva: {current_candle_time} | Close: {df['Close'].iloc[-1]}")

            # --- 2. LÓGICA ESTRATEGIA ---
            df = strategy.calculate_indicators(df)
            signal = strategy.get_signal(df, state)
            action = signal['action']
            
            if action != 'HOLD':
                print(f"⚡ SEÑAL GENERADA: {action}")
                
                # --- EJECUCIÓN ---
                if action == 'ENTER_LONG':
                    if risk_manager.can_open_position(SYMBOL):
                        print("✅ Risk Check Passed.")
                        # exchange.create_order(...)
                        
                        state.update({
                            'status': 'IN_POSITION',
                            'entry_price': signal['entry_price'],
                            'stop_loss': signal['stop_loss'],
                            'tp_partial': signal['tp_partial'],
                            'position_size_pct': 1.0,
                            'trailing_active': False,
                            # FIX 5: Persistir ATR para análisis posterior
                            'atr_at_breakout': state.get('atr_at_breakout', 0.0) 
                        })
                        
                        if tg:
                            tg.send_trade_entry(
                                symbol=SYMBOL,
                                strategy="Breakout 4H",
                                side="LONG",
                                entry=f"{signal['entry_price']:.4f}",
                                sl=f"{signal['stop_loss']:.4f}",
                                tp=f"{signal['tp_partial']:.4f}"
                            )
                    else:
                        print("⛔ Risk Check Failed.")

                elif action == 'EXIT_PARTIAL':
                    print("💰 Partial Profit.")
                    # exchange.create_order(...)
                    state.update({
                        'position_size_pct': 0.5,
                        'stop_loss': signal['new_sl'],
                        'trailing_active': True,
                        'highest_price_post_tp': signal['highest_price_post_tp']
                    })
                    if tg: tg.send_trade_update(SYMBOL, 'PARTIAL', f"SL @ {signal['new_sl']:.4f}")

                elif action in ['EXIT_SL', 'EXIT_TRAILING']:
                    print("🛑 Closing.")
                    # exchange.create_order(...)
                    state = {'status': 'COOLDOWN', 'last_exit_time': str(df.index[-1])}
                    if tg: tg.send_trade_update(SYMBOL, 'CLOSE', f"Tipo: {action}")

                # Actualización de estados
                elif 'new_status' in signal:
                    state['status'] = signal['new_status']
                    if 'breakout_level' in signal: state['breakout_level'] = signal['breakout_level']
                    if 'atr_at_breakout' in signal: state['atr_at_breakout'] = signal['atr_at_breakout']
                
                if action == 'UPDATE_TRAILING':
                    state['stop_loss'] = signal['new_sl']
                    state['highest_price_post_tp'] = signal['highest_price_post_tp']
                    if tg: tg.send_trade_update(SYMBOL, 'TRAILING', f"New SL: {signal['new_sl']:.4f}")

            # --- 🔒 ACTUALIZAR LOCK ---
            # Marcamos esta vela como "vista" para no volver a entrar en este ciclo de 4H
            state['last_processed_candle'] = current_candle_time
            save_state(state)
            
        except Exception as e:
            print(f"❌ Error Main Loop: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60) # Espera corta en error
        
        # Espera normal entre chequeos (si no entró en el if de lock)
        # Esto ocurre la primera vez que procesa la vela nueva
        print("⏳ Esperando 15 min...")
        time.sleep(900) 

if __name__ == "__main__":
    main()