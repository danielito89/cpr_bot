import time
import sys
import os
import traceback
from datetime import datetime

# --- PARCHE DE RUTAS (CRÍTICO) ---
# Esto asegura que Python encuentre las carpetas 'core' y 'strategies'
# sin importar desde qué directorio ejecutes el comando.
current_dir = os.path.dirname(os.path.abspath(__file__))
# Agregamos la carpeta superior (bot_cpr) al path
sys.path.append(os.path.dirname(current_dir))
# ----------------------------------

import config

# --- IMPORTS ---
# Usamos try/except para manejar posibles diferencias de nombres en tus archivos
try:
    from core.binance_api import BinanceClient
except ImportError:
    from core.binance_api import BinanceAPI as BinanceClient

from core.data_processor import DataProcessor
from core.state_manager import StateManager
from core.telegram_bot import TelegramBot
from core.risk_manager import RiskManager
from strategies.strategy_v6_5 import StrategyV6_5

def main():
    print("🐲 INICIANDO HYDRA V6.5 (PRODUCCIÓN)...")
    
    # --- INICIALIZACIÓN DE SERVICIOS ---
    try:
        api = BinanceClient()
        state = StateManager()
        # Pasamos las credenciales explícitamente para evitar el error anterior
        tg = TelegramBot(token=config.TELEGRAM_TOKEN, chat_id=config.TELEGRAM_CHAT_ID)
        processor = DataProcessor()
        strategy = StrategyV6_5()
        
        # Risk Manager
        initial_balance = api.get_balance_usdt()
        risk_mgr = RiskManager(initial_balance)
        print(f"💰 Saldo Inicial: ${initial_balance:.2f} USDT")

        # Notificación de arranque
        mode_txt = "LIVE 💸" if not config.DRY_RUN else "TEST 🧪"
        try:
            tg.send_msg(f"🐲 *Hydra V6.5 Activado*\nModo: `{mode_txt}`\nActivos: {len(config.PAIRS)}")
        except Exception as e:
            print(f"⚠️ No se pudo enviar mensaje de inicio: {e}")

        # --- BUCLE PRINCIPAL ---
        while True:
            try:
                # Sincronización (Loop cada 10s para no saturar CPU)
                time.sleep(10)
                
                # Actualizar saldo real en cada ciclo
                risk_mgr.balance = api.get_balance_usdt()

                for symbol in config.PAIRS:
                    
                    # 1. GESTIÓN DE ESTADO (¿Ya tenemos posición?)
                    current_pos = state.get_position(symbol)
                    
                    # 2. OBTENCIÓN DE DATOS
                    try:
                        # Descarga de velas
                        df = api.get_historical_data(symbol, limit=300)
                        if df is None or df.empty:
                            continue
                        
                        df['symbol_name'] = symbol
                        
                        # Cálculo de indicadores (RSI, ATR, Vol_MA)
                        df = processor.calculate_indicators(df)
                        
                        # Cálculo de Zonas (VAH/VAL) - ¡AQUÍ ESTÁ LA LÓGICA DE BANDAS!
                        zones = processor.get_volume_profile_zones(df)
                        
                    except Exception as e:
                        print(f"❌ Data Error {symbol}: {e}")
                        continue

                    # 3. LÓGICA DE TRADING
                    
                    # A) BUSCAR ENTRADA (Solo si no estamos comprados)
                    if not current_pos:
                        # --- CONFIGURACIÓN DE ESTRATEGIA ---
                        # 1. Buscamos el perfil asignado (Sniper vs Flow)
                        profile_name = config.ASSET_MAP.get(symbol, 'SNIPER') # Default
                        
                        # 2. Cargamos sus parámetros
                        profile_params = config.PROFILES[profile_name].copy()
                        profile_params['name'] = profile_name
                        # CORRECCIÓN: Agregamos el nombre del símbolo para los logs
                        profile_params['symbol_name'] = symbol  
                        
                        # 3. PEDIMOS SEÑAL A LA ESTRATEGIA
                        # Aquí se envían las 'zones' (bandas) y el 'df' (datos)
                        trade = strategy.get_signal(df, zones, profile_params)
                        
                        if trade:
                            print(f"🎯 SEÑAL CONFIRMADA {symbol} [{profile_name}] {trade['type']}")
                            
                            # Gestión de Riesgo
                            risk_tier = trade['risk_type']
                            qty = risk_mgr.calculate_position_size(
                                trade['entry_price'], 
                                trade['stop_loss'],
                                quality=risk_tier 
                            )
                            
                            if qty > 0:
                                if not config.DRY_RUN:
                                    # EJECUCIÓN REAL
                                    side = 'buy' if trade['type'] == 'LONG' else 'sell'
                                    
                                    # 1. Orden de Mercado
                                    if api.place_order(symbol, side, qty):
                                        # 2. Stop Loss
                                        sl_side = 'sell' if side == 'buy' else 'buy'
                                        api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                                       {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                        
                                        # 3. Guardar Estado
                                        state.set_entry(symbol, trade['entry_price'], trade['timestamp'], trade['stop_loss'], trade['type'])
                                        
                                        # 4. Notificar
                                        emoji = "🟢" if trade['type'] == 'LONG' else "🔴"
                                        tg.send_msg(f"{emoji} *ENTRADA {symbol}*\nPerfil: `{profile_name}`\nTipo: {trade['type']}\nRisk: `{risk_tier}`")
                                else:
                                    print(f"🧪 DRY RUN: {symbol} {trade['type']} Qty: {qty}")
                            else:
                                print(f"⚠️ Señal válida pero tamaño de posición 0 (Saldo insuficiente o riesgo alto)")

                    # B) GESTIONAR SALIDA (Si ya estamos dentro)
                    else:
                        # Aquí podrías verificar si la orden se cerró en Binance para limpiar el estado
                        # Por ahora confiamos en el SL/TP del exchange, pero limpiamos si ya no hay posición
                        active_symbols = api.get_open_positions_symbols()
                        if symbol not in active_symbols and not config.DRY_RUN:
                             state.clear_position(symbol)
                             # print(f"🧹 Estado limpiado para {symbol} (Posición cerrada en exchange)")

            except KeyboardInterrupt:
                print("\n🛑 Apagando Hydra...")
                break
            except Exception as e:
                print(f"🔥 Error Crítico en Loop Principal: {e}")
                traceback.print_exc()
                time.sleep(30) # Espera de seguridad ante errores graves

    except Exception as e:
        print(f"🔥 Error de Inicialización General: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()