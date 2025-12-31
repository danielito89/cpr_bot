import time
import sys
import os
import traceback
from datetime import datetime
from dotenv import load_dotenv

# --- 1. CONFIGURACIÓN DE RUTAS Y ENTORNO ---
current_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.abspath(os.path.join(current_dir, '../../.env'))
load_dotenv(dotenv_path)

# Añadir ruta raíz
root_path = os.path.abspath(os.path.join(current_dir, '../..'))
sys.path.append(root_path)

# --- 2. IMPORTS ---
import config

# Imports Shared (Nueva Arquitectura)
from shared.telegram_bot import TelegramBot  # <--- USAMOS EL COMPARTIDO
# from shared.ccxt_handler import ExchangeHandler (Opcional si migras todo luego)

# Imports Legacy
try:
    from core.binance_api import BinanceClient
except ImportError:
    from core.binance_api import BinanceAPI as BinanceClient

from core.data_processor import DataProcessor
from addons.state_manager import StateManager
from core.risk_manager import RiskManager
from strategies.strategy_v6_5 import StrategyV6_5

def main():
    print("🐲 INICIANDO HYDRA V6.5 (PRODUCCIÓN)...")
    
    # Verificación de Seguridad
    if not os.getenv('BINANCE_API_KEY'):
        print("❌ ERROR CRÍTICO: No se detectaron API KEYS en el entorno .env")
        return

    # --- INICIALIZACIÓN DE SERVICIOS ---
    try:
        api = BinanceClient() 
        state = StateManager()
        processor = DataProcessor()
        strategy = StrategyV6_5()
        
        # --- TELEGRAM SETUP ---
        tg_token = os.getenv('TELEGRAM_TOKEN')
        tg_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        tg = None
        
        if tg_token and tg_chat_id:
             tg = TelegramBot(token=tg_token, chat_id=tg_chat_id)
             print("✅ Telegram Conectado")
        else:
             print("⚠️ Telegram Desactivado (Faltan credenciales)")

        # Risk Manager
        initial_balance = api.get_balance_usdt()
        risk_mgr = RiskManager(initial_balance)
        print(f"💰 Saldo Inicial: ${initial_balance:.2f} USDT")

        # Notificación de arranque
        mode_txt = "LIVE 💸" if not config.DRY_RUN else "TEST 🧪"
        if tg:
            tg.send_msg(f"🐲 *Hydra V6.5 Activado*\nModo: `{mode_txt}`\nActivos: {len(config.PAIRS_SCALPER)}")

        # Variable para el Heartbeat (Anti-Zombie)
        last_heartbeat_day = datetime.now().day

        # --- BUCLE PRINCIPAL ---
        while True:
            try:
                time.sleep(10) # Loop cada 10s
                
                # --- 💓 HEARTBEAT DIARIO (Anti-Zombie) ---
                current_day = datetime.now().day
                if current_day != last_heartbeat_day:
                    if tg:
                        # Contamos posiciones abiertas
                        positions_count = len(api.get_open_positions_symbols())
                        tg.send_daily_report("Hydra Scalper 🐲", config.PAIRS_SCALPER, positions_count)
                    last_heartbeat_day = current_day
                    print("💓 Heartbeat diario enviado.")

                # Actualizar saldo
                risk_mgr.balance = api.get_balance_usdt()

                # Iteramos sobre la lista de SCALPER (definida en config nuevo)
                # Si aún usas config.PAIRS viejo, cámbialo aquí a config.PAIRS
                pairs_to_scan = getattr(config, 'PAIRS_SCALPER', config.PAIRS) 

                for symbol in pairs_to_scan:
                    
                    # 1. GESTIÓN DE ESTADO
                    current_pos = state.get_position(symbol)
                    
                    # 2. OBTENCIÓN DE DATOS
                    try:
                        df = api.get_historical_data(symbol, limit=300)
                        if df is None or df.empty: continue
                        
                        df['symbol_name'] = symbol
                        df = processor.calculate_indicators(df)
                        zones = processor.get_volume_profile_zones(df)
                    except Exception as e:
                        print(f"❌ Data Error {symbol}: {e}")
                        continue

                    # 3. LÓGICA DE TRADING
                    if not current_pos:
                        profile_name = config.ASSET_MAP.get(symbol, 'SNIPER')
                        
                        # Cargar perfil
                        if hasattr(config, 'PROFILES') and profile_name in config.PROFILES:
                            profile_params = config.PROFILES[profile_name].copy()
                        else:
                            profile_params = {'name': 'DEFAULT', 'risk_type': 'conservative'}
                            
                        profile_params['name'] = profile_name
                        profile_params['symbol_name'] = symbol  
                        
                        trade = strategy.get_signal(df, zones, profile_params)
                        
                        if trade:
                            print(f"🎯 SEÑAL {symbol} [{profile_name}] {trade['type']}")
                            risk_tier = trade.get('risk_type', 'standard')
                            
                            # Calcular tamaño
                            try:
                                qty = risk_mgr.calculate_position_size(
                                    trade['entry_price'], 
                                    trade['stop_loss'], 
                                    quality=risk_tier 
                                )
                            except: qty = 0
                            
                            if qty > 0:
                                if not config.DRY_RUN:
                                    # EJECUCIÓN REAL
                                    side = 'buy' if trade['type'] == 'LONG' else 'sell'
                                    if api.place_order(symbol, side, qty):
                                        # SL Order
                                        sl_side = 'sell' if side == 'buy' else 'buy'
                                        api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                                       {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                        
                                        # Guardar Estado
                                        state.set_entry(symbol, trade['entry_price'], trade['timestamp'], trade['stop_loss'], trade['type'])
                                        
                                        # Notificar Telegram (Nuevo Formato)
                                        if tg:
                                            tg.send_trade_entry(
                                                symbol=symbol,
                                                strategy=f"Scalper {profile_name}",
                                                side=trade['type'],
                                                entry=trade['entry_price'],
                                                sl=trade['stop_loss'],
                                                tp="Dynamic (Fib)" # Scalper usa TP dinámico a veces
                                            )
                                else:
                                    print(f"🧪 DRY RUN: {symbol} Qty: {qty}")
                                    if tg: tg.send_msg(f"🧪 *DRY RUN SIGNAL* {symbol} {trade['type']}")

                    # B) GESTIONAR SALIDA (Limpieza de estado)
                    else:
                        active_symbols = api.get_open_positions_symbols()
                        if symbol not in active_symbols and not config.DRY_RUN:
                             state.clear_position(symbol)
                             # Opcional: Avisar cierre si quieres mucho ruido
                             # if tg: tg.send_trade_update(symbol, 'CLOSE', "Posición cerrada en exchange")

            except KeyboardInterrupt:
                print("\n🛑 Apagando Hydra...")
                break
            except Exception as e:
                print(f"🔥 Error Loop: {e}")
                traceback.print_exc()
                time.sleep(30)

    except Exception as e:
        print(f"🔥 Error Init: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()