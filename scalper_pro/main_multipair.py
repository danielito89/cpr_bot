import time
import sys
import os
import traceback
from datetime import datetime

# Importación de módulos propios
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
from core.binance_client import BinanceClient
from core.data_processor import DataProcessor
from core.state_manager import StateManager
from core.telegram_bot import TelegramBot
from core.risk_manager import RiskManager
from strategies.strategy_v6_5 import StrategyV6_5 # <--- Importamos la V6.5

def main():
    print("🐲 INICIANDO HYDRA V6.5 (PRODUCCIÓN)...")
    
    # Inicialización de Servicios
    api = BinanceClient()
    state = StateManager()
    tg = TelegramBot()
    processor = DataProcessor()
    strategy = StrategyV6_5()
    
    # Risk Manager
    initial_balance = api.get_balance_usdt()
    risk_mgr = RiskManager(initial_balance)
    print(f"💰 Saldo Inicial: ${initial_balance:.2f} USDT")

    # Notificación de arranque
    mode_txt = "LIVE 💸" if not config.DRY_RUN else "TEST 🧪"
    tg.send_msg(f"🐲 *Hydra V6.5 Activado*\nModo: `{mode_txt}`\nActivos: {len(config.PAIRS)}")

    # Bucle Principal
    while True:
        try:
            # Sincronización (Loop cada 10s para no saturar CPU)
            time.sleep(10)
            
            # Actualizar saldo real
            risk_mgr.balance = api.get_balance_usdt()

            for symbol in config.PAIRS:
                
                # 1. GESTIÓN DE ESTADO
                current_pos = state.get_position(symbol)
                
                # 2. OBTENCIÓN DE DATOS
                try:
                    df = api.get_historical_data(symbol, limit=300)
                    if df is None or df.empty:
                        continue
                    
                    df['symbol_name'] = symbol
                    df = processor.calculate_indicators(df)
                    zones = processor.get_volume_profile_zones(df)
                    
                except Exception as e:
                    print(f"❌ Data Error {symbol}: {e}")
                    continue

                # 3. LÓGICA DE TRADING
                
                # A) BUSCAR ENTRADA (Si no hay posición)
                if not current_pos:
                    # --- MAGIC HAPPENS HERE ---
                    # 1. Buscamos el perfil asignado en el Mapa
                    profile_name = config.ASSET_MAP.get(symbol, 'SNIPER') # Default seguro
                    # 2. Cargamos sus parámetros
                    profile_params = config.PROFILES[profile_name].copy()
                    profile_params['name'] = profile_name
                    
                    # 3. Pedimos señal a la estrategia con esos parámetros
                    trade = strategy.get_signal(df, zones, profile_params)
                    
                    if trade:
                        print(f"🎯 SEÑAL {symbol} [{profile_name}] {trade['type']}")
                        
                        # Gestión de Riesgo según Perfil
                        risk_tier = trade['risk_type'] # PREMIUM o STANDARD
                        
                        # Calculamos cantidad (Lote)
                        qty = risk_mgr.calculate_position_size(
                            trade['entry_price'], 
                            trade['stop_loss'],
                            quality=risk_tier 
                        )
                        
                        if qty > 0:
                            if not config.DRY_RUN:
                                # Ejecutar Orden
                                side = 'buy' if trade['type'] == 'LONG' else 'sell'
                                if api.place_order(symbol, side, qty):
                                    # Poner Stop Loss
                                    sl_side = 'sell' if side == 'buy' else 'buy'
                                    api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                                   {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                    
                                    # Guardar Estado
                                    state.set_entry(symbol, trade['entry_price'], trade['timestamp'], trade['stop_loss'], trade['type'])
                                    
                                    # Notificar
                                    emoji = "🟢" if trade['type'] == 'LONG' else "🔴"
                                    tg.send_msg(f"{emoji} *ENTRADA {symbol}*\nPerfil: `{profile_name}`\nTipo: {trade['type']}\nRisk: `{risk_tier}`")
                            else:
                                print(f"🧪 DRY RUN: {symbol} {trade['type']} Qty: {qty}")

                # B) GESTIONAR SALIDA (Si hay posición)
                else:
                    # Aquí implementas tu lógica de Trailing Stop o Break Even
                    # O simplemente dejas que el TP/SL del exchange actúe y
                    # limpias el estado cuando la posición desaparezca de la API.
                    
                    # Chequeo simple de limpieza de estado si la posición ya no existe en Binance
                    open_positions = api.get_open_positions_symbols() # Método hipotético, o check manual
                    if symbol not in open_positions and not config.DRY_RUN:
                         state.clear_position(symbol)
                         # tg.send_msg(f"🏁 Salida detectada {symbol}")

        except KeyboardInterrupt:
            print("\n🛑 Apagando Hydra...")
            break
        except Exception as e:
            print(f"🔥 Error Crítico: {e}")
            traceback.print_exc()
            time.sleep(30) # Pausa de seguridad

if __name__ == "__main__":
    main()