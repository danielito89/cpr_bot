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
from strategies.strategy_v6_4 import StrategyV6_4

def main():
    # --- INICIALIZACIÓN ---
    print("🐲 INICIANDO HYDRA V6.5 (MULTI-PROFILE ENGINE)...")
    
    api = BinanceClient()
    state = StateManager()
    tg = TelegramBot()
    
    # Inicializar saldo en Risk Manager
    initial_balance = api.get_balance_usdt()
    risk_mgr = RiskManager(initial_balance)
    print(f"💰 Saldo Inicial: ${initial_balance:.2f} USDT")

    processor = DataProcessor()
    strategy = StrategyV6_4()

    # Variables de control
    last_candle_time = {} 

    # Mensaje de arranque
    tg.send_msg(f"🐲 *Hydra V6.5 Activado*\nModo: `{'LIVE 💸' if not config.DRY_RUN else 'TEST 🧪'}`\nPerfiles Activos: {len(config.ASSET_MAP)} Pares")

    # --- BUCLE PRINCIPAL ---
    while True:
        try:
            # Sincronización de Reloj (Cada 5 seg)
            time.sleep(5)
            
            # Actualizar saldo real para cálculos de riesgo precisos
            current_balance = api.get_balance_usdt()
            risk_mgr.balance = current_balance

            # Iterar sobre la lista maestra de pares
            for symbol in config.PAIRS:
                
                # 1. DETECCIÓN DE PERFIL (V6.5 Logic) 🧠
                # Buscamos qué personalidad tiene este activo (Sniper vs Flow)
                profile_name = config.ASSET_MAP.get(symbol, 'SNIPER') # Default seguro
                profile_params = config.PROFILES.get(profile_name).copy()
                profile_params['name'] = profile_name # Para logs
                
                # 2. GESTIÓN DE ESTADO
                current_pos = state.get_position(symbol)
                
                # 3. OBTENCIÓN DE DATOS
                try:
                    df = api.get_historical_data(symbol, limit=350) # 300 para VP + buffer
                    if df is None or df.empty:
                        print(f"⚠️ No data {symbol}")
                        continue
                    
                    # Inyectar nombre para la estrategia
                    df['symbol_name'] = symbol
                    
                    # Calcular Indicadores
                    df = processor.calculate_indicators(df)
                    zones = processor.get_volume_profile_zones(df)
                    
                except Exception as e:
                    print(f"❌ Error Data {symbol}: {e}")
                    continue

                # 4. LOGICA DE TRADING
                
                # A) Si NO tenemos posición -> BUSCAR ENTRADA
                if not current_pos:
                    # Pasamos los parámetros del perfil a la estrategia
                    trade = strategy.get_signal(df, zones, profile_params)
                    
                    if trade:
                        print(f"🎯 SEÑAL {symbol} [{profile_name}] {trade['type']}")
                        
                        # Cálculo de Tamaño de Posición (Tiered Sizing)
                        # El perfil dicta si arriesgamos 1.5% o 3.0%
                        qty = risk_mgr.calculate_position_size(
                            trade['entry_price'], 
                            trade['stop_loss'],
                            quality=trade['risk_type'] # 'PREMIUM' o 'STANDARD'
                        )
                        
                        if qty > 0:
                            if not config.DRY_RUN:
                                # Ejecutar Orden Real
                                side = 'buy' if trade['type'] == 'LONG' else 'sell'
                                if api.place_order(symbol, side, qty):
                                    # Poner Stop Loss en el Exchange
                                    sl_side = 'sell' if side == 'buy' else 'buy'
                                    api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                                   {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                    
                                    # Guardar Estado
                                    state.set_entry(symbol, trade['entry_price'], trade['timestamp'], trade['stop_loss'], trade['type'])
                                    
                                    # Notificar
                                    emoji = "🟢" if trade['type'] == 'LONG' else "🔴"
                                    tg.send_msg(f"{emoji} *ENTRADA {symbol}*\nPerfil: `{profile_name}`\nTipo: {trade['type']}\nRisk: `{trade['risk_type']}`")
                            else:
                                print(f"🧪 DRY RUN: Buy {symbol} Qty: {qty}")

                # B) Si YA tenemos posición -> GESTIONAR SALIDA
                else:
                    # Verificar si la posición sigue viva en Binance (Zombie Check)
                    # (Simplificado: Asumimos que si hay posición en state, revisamos profit/sl)
                    
                    # Lógica de Break Even (Mover SL a entrada si ganamos 1R)
                    entry_price = current_pos['entry_price']
                    stop_loss = current_pos['stop_loss']
                    current_price = df['close'].iloc[-1]
                    side = current_pos['type']
                    
                    # Distancia de riesgo inicial
                    risk_dist = abs(entry_price - stop_loss)
                    
                    # Calcular R actual
                    if side == 'LONG':
                        r_current = (current_price - entry_price) / risk_dist
                    else:
                        r_current = (entry_price - current_price) / risk_dist
                    
                    # Notificar TP/SL si se cerró (Check against balance or order status needed in production)
                    # Aquí hacemos una comprobación pasiva: si el estado dice "Open" pero no hay orden en binance, se cerró.
                    # Para simplificar este script, asumimos que el TP/SL del exchange se encarga del cierre
                    # y el 'watchdog' o el reinicio limpia el estado.
                    
                    # Autolimpieza básica: Si pasaron > 12 horas, forzar cierre (Time Stop extremo)
                    entry_time = pd.to_datetime(current_pos['time'])
                    hours_open = (datetime.utcnow() - entry_time).total_seconds() / 3600
                    
                    if hours_open > 12:
                        print(f"⏰ TIME STOP {symbol} (>12h)")
                        close_side = 'sell' if side == 'LONG' else 'buy'
                        # Obtener cantidad (consultar API en prod, aquí simulado)
                        # api.close_position(symbol) ...
                        state.clear_position(symbol)
                        tg.send_msg(f"⏰ *Cierre por Tiempo {symbol}*")

        except KeyboardInterrupt:
            print("\n🛑 Deteniendo Hydra...")
            break
        except Exception as e:
            print(f"🔥 CRITICAL ERROR: {e}")
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    main()