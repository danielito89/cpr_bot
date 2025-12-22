import time
import sys
from datetime import datetime
import pandas as pd

# Imports Locales
import config
from core.binance_api import BinanceAPI
from core.data_processor import DataProcessor
from core.risk_manager import RiskManager
from strategies.strategy_v6_4 import StrategyV6_4
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot

def main():
    print(f"\n🐲 INICIANDO HYDRA MULTIPAIR V6.4 - {len(config.PAIRS)} PARES 🐲")
    
    # 1. INIT
    try:
        api = BinanceAPI()
        processor = DataProcessor()
        # Risk manager inicial (se actualiza saldo en loop)
        risk_mgr = RiskManager(balance=0, risk_per_trade=config.RISK_PER_TRADE, leverage=config.LEVERAGE)
        strategy = StrategyV6_4()
        state = StateManager()
        tg = TelegramBot(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)
        
        tg.send_msg(f"🐲 *Hydra System Online*\nPares: `{config.PAIRS}`\nModo: `{'DRY' if config.DRY_RUN else 'LIVE'}`")
    except Exception as e:
        print(f"❌ Error Init: {e}")
        sys.exit(1)

    # 2. VARIABLES GLOBALES
    daily_pnl_r = 0.0
    last_reset_day = datetime.utcnow().day
    last_heartbeat = time.time()
    
    # Estimación de fees para PnL neto
    ESTIMATED_FEE_R = 0.05 

    while True:
        try:
            now = datetime.utcnow()
            
            # --- HEARTBEAT (4H) ---
            if time.time() - last_heartbeat > (4 * 3600):
                bal = api.get_balance_usdt()
                tg.send_msg(f"💓 *Hydra Heartbeat*\nSaldo: `${bal:.2f}`\nPnL Hoy: `{daily_pnl_r:.2f}R`")
                last_heartbeat = time.time()

            # --- RESET DIARIO ---
            if now.day != last_reset_day:
                daily_pnl_r = 0.0
                last_reset_day = now.day
                tg.send_msg("🗓️ *Nuevo Día UTC* - PnL Reset.")

            # --- SINCRONIZACIÓN (05s) ---
            if now.second != 5 or now.minute % 5 != 0:
                time.sleep(1)
                continue

            print(f"\n--- 🕒 CICLO: {now.strftime('%H:%M:%S')} ---")
            
            # Actualizar Saldo Global
            risk_mgr.balance = api.get_balance_usdt()

            # ==========================================
            # LOOP DE PARES
            # ==========================================
            for symbol in config.PAIRS:
                print(f"🔍 {symbol}...", end=' ')
                
                # A. Descargar
                df = api.fetch_ohlcv(symbol, limit=500)
                if df is None: 
                    print("❌ Data err")
                    continue

                # B. Indicadores
                df = processor.calculate_indicators(df)
                zones = processor.get_volume_profile_zones(df)
                if not zones: 
                    print("⏩ No Zones")
                    continue

                # C. Estado & Reconciliación
                bot_state = state.get_pair_state(symbol)
                real_pos = api.get_position(symbol)
                
                # --- AUDITORÍA DE SEGURIDAD ---
                # 1. Zombie: Binance tiene pos, Bot no.
                if real_pos and not bot_state.get("in_position"):
                    print("🧟 ZOMBIE! Cerrando.")
                    api.close_position(real_pos)
                    tg.send_msg(f"🧟 *Zombie Eliminado*: {symbol}")
                    continue
                
                # 2. Ghost: Bot tiene pos, Binance no.
                if not real_pos and bot_state.get("in_position"):
                    print("👻 GHOST! Limpiando.")
                    state.clear_pair_state(symbol)
                    continue
                    
                # 3. Alineación: Lados opuestos.
                if real_pos and bot_state.get("in_position"):
                    if real_pos['side'] != bot_state['side']:
                        print("⚠️ Lado incorrecto. Cerrando.")
                        api.close_position(real_pos)
                        state.clear_pair_state(symbol)
                        continue

                # --- GESTIÓN DE POSICIÓN ---
                if real_pos:
                    entry_price = float(bot_state['entry_price'])
                    current_price = float(df.iloc[-1]['close'])
                    bars_held = state.update_bars_held(symbol)
                    
                    sl_dist = abs(entry_price - bot_state['stop_loss'])
                    if sl_dist == 0: sl_dist = entry_price * 0.01
                    
                    if real_pos['side'] == 'LONG':
                        pnl_r = (current_price - entry_price) / sl_dist
                    else:
                        pnl_r = (entry_price - current_price) / sl_dist
                    
                    print(f"Holding ({bars_held} bars) R: {pnl_r:.2f}")

                    # Reglas V6.4
                    close = False
                    reason = ""
                    if bars_held == 2 and pnl_r < -0.10: close=True; reason="Failed FT"
                    if bars_held == 4 and pnl_r < 0.25: close=True; reason="Stagnant 4"
                    if bars_held == 6 and pnl_r < 0.20: close=True; reason="Stagnant 6"
                    if bars_held >= 11: close=True; reason="Time Stop"
                    if pnl_r >= 3.0: close=True; reason="TP2 Hit 🎯"
                    if pnl_r <= -1.1: close=True; reason="Hard SL 🛑"

                    # TP1 Move to BE
                    if pnl_r >= 1.0 and not bot_state.get("tp1_hit"):
                        state.set_tp1_hit(symbol)
                        print("💰 TP1 Hit -> Moviendo SL a BE")
                        try:
                            api.exchange.cancel_all_orders(symbol)
                            be_price = entry_price * (1.001 if real_pos['side']=='LONG' else 0.999)
                            sl_side = 'sell' if real_pos['side']=='LONG' else 'buy'
                            api.place_order(symbol, sl_side, real_pos['amount'], 'STOP_MARKET', 
                                {'stopPrice': be_price, 'closePosition': True})
                            tg.send_msg(f"🛡️ *{symbol} TP1 Hit*: SL movido a BE")
                        except Exception as e:
                            print(f"Error moviendo SL: {e}")

                    if close:
                        print(f"⚡ Cerrando: {reason}")
                        api.close_position(real_pos)
                        state.clear_pair_state(symbol)
                        
                        net_r = pnl_r - ESTIMATED_FEE_R
                        daily_pnl_r += net_r
                        
                        icon = "✅" if net_r > 0 else "❌"
                        tg.send_msg(f"{icon} *Cierre {symbol}*\nRes: `{net_r:.2f}R`\nMotivo: {reason}")

                # --- BÚSQUEDA DE ENTRADA ---
                else:
                    print("Analizando...", end=' ')
                    trade = strategy.get_signal(df, zones)
                    
                    if trade:
                        print(f"🚀 SEÑAL {trade['type']}")
                        qty = risk_mgr.calculate_position_size(trade['entry_price'], trade['stop_loss'])
                        
                        if qty > 0:
                            # 1. Entry
                            order = api.place_order(symbol, 'buy' if trade['type']=='LONG' else 'sell', qty)
                            
                            if order:
                                # 2. SL (Close Position True)
                                sl_side = 'sell' if trade['type']=='LONG' else 'buy'
                                api.place_order(symbol, sl_side, qty, 'STOP_MARKET', 
                                    {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                
                                # 3. Save State
                                state.set_entry(symbol, trade['entry_price'], trade['time'], trade['stop_loss'], trade['type'])
                                
                                # 4. Telegram
                                tp_calc = trade['entry_price'] + (trade['atr']*3) if trade['type']=='LONG' else trade['entry_price'] - (trade['atr']*3)
                                tg.send_msg(f"🚀 *Entrada {symbol} {trade['type']}*\nSize: `{qty}`\nSL: `{trade['stop_loss']:.2f}`\nTP: `{tp_calc:.2f}`")
                            else:
                                print("Error enviando orden")
                        else:
                            print("Saldo insuficiente")
                    else:
                        print("Nada.")

            # Fin Loop Pares
            print("💤 Esperando...")
            time.sleep(55)

        except KeyboardInterrupt:
            sys.exit()
        except Exception as e:
            print(f"❌ Error Global: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()