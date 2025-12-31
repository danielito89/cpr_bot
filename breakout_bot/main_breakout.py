import time
import json
import ccxt
from datetime import datetime
from strategy import BreakoutBotStrategy
# from shared.telegram_bot import send_msg

# Configuración
SYMBOL = 'BTC/USDT'
TIMEFRAME = '4h'
STATE_FILE = 'state_breakout.json'

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {} # Estado vacío

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def fetch_data(exchange):
    # Traemos 300 velas para tener suficiente data para EMA200 y ATR
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=300)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

def main():
    exchange = ccxt.binance() # O tu configuración cargada
    strategy = BreakoutBotStrategy()
    
    print(f"🚀 Breakout Bot 4H iniciado en {SYMBOL}")
    
    while True:
        # 1. Obtener hora actual y sincronizar
        # Esperamos al minuto 1 de la siguiente vela de 4H para asegurar cierre
        # Lógica simplificada: Chequeo cada 5 minutos
        
        try:
            print("⏳ Chequeando mercado...")
            state = load_state()
            df = fetch_data(exchange)
            
            # Calculamos indicadores con la data fresca
            df = strategy.calculate_indicators(df)
            
            # Obtenemos señal
            signal = strategy.get_signal(df, state)
            
            action = signal['action']
            
            if action != 'HOLD':
                print(f"⚡ ACCIÓN: {action}")
                # send_msg(f"Breakout Bot: {action}")
                
                # --- EJECUTAR CAMBIOS DE ESTADO ---
                if 'new_status' in signal:
                    state['status'] = signal['new_status']
                
                if 'new_sl' in signal:
                    state['stop_loss'] = signal['new_sl']
                    # Aquí llamarías a exchange.edit_order si usas SL en el exchange
                
                if action == 'ENTER_LONG':
                    state['entry_price'] = signal['entry_price']
                    state['stop_loss'] = signal['stop_loss']
                    state['tp_partial'] = signal['tp_partial']
                    state['position_size_pct'] = 1.0
                    state['highest_price_post_tp'] = 0.0
                    # AQUÍ: exchange.create_order(...)
                
                elif action == 'EXIT_PARTIAL':
                    state['position_size_pct'] = 0.5
                    state['trailing_active'] = True
                    state['highest_price_post_tp'] = signal['highest_price_post_tp']
                    # AQUÍ: exchange.create_order(side='sell', amount=50%...)
                
                elif action in ['EXIT_SL', 'EXIT_TRAILING']:
                    state['last_exit_time'] = str(df.index[-1]) # Fecha última vela
                    # AQUÍ: Cerrar posición restante en exchange
                
                elif action == 'UPDATE_TRAILING':
                    state['highest_price_post_tp'] = signal['highest_price_post_tp']
                    print(f"🔄 Trailing subido a {signal['new_sl']}")

                save_state(state)
            
            else:
                print("💤 Nada que hacer. Hold.")

        except Exception as e:
            print(f"❌ Error: {e}")
        
        # Dormir 5 minutos (300 seg)
        # No hace falta spamear la API en 4H
        time.sleep(300) 

if __name__ == "__main__":
    main()