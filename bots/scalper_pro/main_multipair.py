import time
import sys
import os
import traceback
from datetime import datetime
from dotenv import load_dotenv

# --- 1. CONFIGURACIÓN DE RUTAS Y ENTORNO ---
# Ruta del archivo actual
current_dir = os.path.dirname(os.path.abspath(__file__))

# Cargar .env (Subimos 2 niveles: bots/scalper/ -> bots/ -> root/.env)
dotenv_path = os.path.abspath(os.path.join(current_dir, '../../.env'))
load_dotenv(dotenv_path)

# Añadir ruta raíz para que Python encuentre 'shared', 'core', 'config', etc.
root_path = os.path.abspath(os.path.join(current_dir, '../..'))
sys.path.append(root_path)

# --- 2. IMPORTS ---
import config  # Configuración general (pares, blacklist, etc.)

# Imports Shared
# (Opcional si vas a migrar el Scalper a la nueva arquitectura completa,
# por ahora lo dejamos comentado para no romper tu lógica antigua)
# from shared.ccxt_handler import ExchangeHandler 

# Imports Legacy (Tu estructura actual)
try:
    from core.binance_api import BinanceClient
except ImportError:
    from core.binance_api import BinanceAPI as BinanceClient

from core.data_processor import DataProcessor
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot
from core.risk_manager import RiskManager
from strategies.strategy_v6_5 import StrategyV6_5

def main():
    print("🐲 INICIANDO HYDRA V6.5 (PRODUCCIÓN)...")
    
    # Verificación de Seguridad
    if not os.getenv('BINANCE_API_KEY'):
        print("❌ ERROR CRÍTICO: No se detectaron API KEYS en el entorno.")
        print("   Asegúrate de que el archivo .env existe y está cargado.")
        return

    # --- INICIALIZACIÓN DE SERVICIOS ---
    try:
        # ⚠️ IMPORTANTE: Asegúrate de que BinanceClient dentro de core/binance_api.py
        # también esté leyendo os.getenv('BINANCE_API_KEY') y no config.API_KEY
        api = BinanceClient() 
        
        state = StateManager()
        
        # --- CORRECCIÓN CLAVE: LEER CREDENCIALES DEL .ENV ---
        tg_token = os.getenv('TELEGRAM_TOKEN')
        tg_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if not tg_token or not tg_chat_id:
             print("⚠️ Advertencia: Credenciales de Telegram no encontradas en .env")
             tg = None
        else:
             tg = TelegramBot(token=tg_token, chat_id=tg_chat_id)

        processor = DataProcessor()
        strategy = StrategyV6_5()
        
        # Risk Manager
        initial_balance = api.get_balance_usdt()
        risk_mgr = RiskManager(initial_balance)
        print(f"💰 Saldo Inicial: ${initial_balance:.2f} USDT")

        # Notificación de arranque
        mode_txt = "LIVE 💸" if not config.DRY_RUN else "TEST 🧪"
        
        if tg:
            try:
                tg.send_msg(f"🐲 *Hydra V6.5 Activado*\nModo: `{mode_txt}`\nActivos: {len(config.PAIRS)}")
            except Exception as e:
                print(f"⚠️ No se pudo enviar mensaje de inicio: {e}")

        # --- BUCLE PRINCIPAL ---
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
                        df = api.get_historical_data(symbol, limit=300)
                        if df is None or df.empty:
                            continue
                        
                        df['symbol_name'] = symbol
                        
                        # Indicadores + Bandas
                        df = processor.calculate_indicators(df)
                        zones = processor.get_volume_profile_zones(df)
                        
                    except Exception as e:
                        print(f"❌ Data Error {symbol}: {e}")
                        continue

                    # 3. LÓGICA DE TRADING
                    if not current_pos:
                        # --- ESTRATEGIA ---
                        profile_name = config.ASSET_MAP.get(symbol, 'SNIPER')
                        
                        # Manejo seguro de configuración de perfiles
                        if hasattr(config, 'PROFILES') and profile_name in config.PROFILES:
                            profile_params = config.PROFILES[profile_name].copy()
                        else:
                            # Fallback si falla la config
                            profile_params = {'name': 'DEFAULT', 'risk_type': 'conservative'}
                            
                        profile_params['name'] = profile_name
                        profile_params['symbol_name'] = symbol  
                        
                        trade = strategy.get_signal(df, zones, profile_params)
                        
                        if trade:
                            print(f"🎯 SEÑAL CONFIRMADA {symbol} [{profile_name}] {trade['type']}")
                            
                            risk_tier = trade.get('risk_type', 'standard')
                            
                            # Calcular tamaño
                            try:
                                qty = risk_mgr.calculate_position_size(
                                    trade['entry_price'], 
                                    trade['stop_loss'], 
                                    quality=risk_tier 
                                )
                            except Exception as e:
                                print(f"⚠️ Error calculando tamaño: {e}")
                                qty = 0
                            
                            if qty > 0:
                                if not config.DRY_RUN:
                                    # EJECUCIÓN REAL
                                    side = 'buy' if trade['type'] == 'LONG' else 'sell'
                                    
                                    if api.place_order(symbol, side, qty):
                                        sl_side = 'sell' if side == 'buy' else 'buy'
                                        # Orden Stop Loss
                                        api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                                       {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                        
                                        # Guardar Estado
                                        state.set_entry(symbol, trade['entry_price'], trade['timestamp'], trade['stop_loss'], trade['type'])
                                        
                                        # Notificar
                                        if tg:
                                            emoji = "🟢" if trade['type'] == 'LONG' else "🔴"
                                            tg.send_msg(f"{emoji} *ENTRADA {symbol}*\nPerfil: `{profile_name}`\nTipo: {trade['type']}\nRisk: `{risk_tier}`")
                                else:
                                    print(f"🧪 DRY RUN: {symbol} {trade['type']} Qty: {qty}")
                            else:
                                print(f"⚠️ Señal válida pero tamaño de posición 0")

                    # B) GESTIONAR SALIDA
                    else:
                        active_symbols = api.get_open_positions_symbols()
                        if symbol not in active_symbols and not config.DRY_RUN:
                             state.clear_position(symbol)

            except KeyboardInterrupt:
                print("\n🛑 Apagando Hydra...")
                break
            except Exception as e:
                print(f"🔥 Error Crítico en Loop Principal: {e}")
                traceback.print_exc()
                time.sleep(30)

    except Exception as e:
        print(f"🔥 Error de Inicialización General: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()