import time
import sys
import os
import traceback
from datetime import datetime

# --- PARCHE DE RUTAS (PATH FIX) ---
# Esto asegura que Python encuentre la carpeta 'core' y 'strategies' 
# sin importar desde dónde ejecutes el script.
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
# ----------------------------------

import config

# --- CORRECCIÓN DE IMPORTS SEGÚN TU IMAGEN ---
# 1. binance_api.py en lugar de binance_client
# (Asumo que la clase dentro se llama BinanceClient o BinanceAPI, prueba con la primera)
try:
    from core.binance_api import BinanceClient 
except ImportError:
    from core.binance_api import BinanceAPI as BinanceClient

from core.data_processor import DataProcessor
from core.risk_manager import RiskManager

# 2. Archivos que NO veo en tu imagen (Los vamos a crear abajo)
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot

# Importamos la estrategia
from strategies.strategy_v6_5 import StrategyV6_5

def main():
    print("🐲 INICIANDO HYDRA V6.5 (PRODUCCIÓN)...")
    
    # Inicialización de Servicios
    try:
        api = BinanceClient() # Si falla aquí, revisa el nombre de la clase en binance_api.py
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
                # Sincronización (Loop cada 10s)
                time.sleep(10)
                
                # Actualizar saldo real
                risk_mgr.balance = api.get_balance_usdt()

                for symbol in config.PAIRS:
                    
                    # 1. GESTIÓN DE ESTADO
                    current_pos = state.get_position(symbol)
                    
                    # 2. OBTENCIÓN DE DATOS
                    try:
                        # Usamos get_historical_data de tu api
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
                    
                    # A) BUSCAR ENTRADA
                    if not current_pos:
                        profile_name = config.ASSET_MAP.get(symbol, 'SNIPER')
                        profile_params = config.PROFILES[profile_name].copy()
                        profile_params['name'] = profile_name
                        
                        trade = strategy.get_signal(df, zones, profile_params)
                        
                        if trade:
                            print(f"🎯 SEÑAL {symbol} [{profile_name}] {trade['type']}")
                            
                            risk_tier = trade['risk_type']
                            qty = risk_mgr.calculate_position_size(
                                trade['entry_price'], 
                                trade['stop_loss'],
                                quality=risk_tier 
                            )
                            
                            if qty > 0:
                                if not config.DRY_RUN:
                                    side = 'buy' if trade['type'] == 'LONG' else 'sell'
                                    if api.place_order(symbol, side, qty):
                                        sl_side = 'sell' if side == 'buy' else 'buy'
                                        # Colocar SL
                                        api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                                       {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                        
                                        state.set_entry(symbol, trade['entry_price'], trade['timestamp'], trade['stop_loss'], trade['type'])
                                        
                                        emoji = "🟢" if trade['type'] == 'LONG' else "🔴"
                                        tg.send_msg(f"{emoji} *ENTRADA {symbol}*\nPerfil: `{profile_name}`\nTipo: {trade['type']}\nRisk: `{risk_tier}`")
                                else:
                                    print(f"🧪 DRY RUN: {symbol} {trade['type']} Qty: {qty}")

                    # B) GESTIONAR SALIDA
                    else:
                        # Limpieza de estado si la posición ya se cerró en Binance
                        # (Implementación simplificada)
                        pass 

            except KeyboardInterrupt:
                print("\n🛑 Apagando Hydra...")
                break
            except Exception as e:
                print(f"🔥 Error en Loop: {e}")
                traceback.print_exc()
                time.sleep(30)

    except Exception as e:
        print(f"🔥 Error de Inicialización: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()