# scalper_pro/main.py

import time
import sys
import os
from datetime import datetime
import pandas as pd

import config
from core.binance_api import BinanceAPI
from core.data_processor import DataProcessor
from core.risk_manager import RiskManager
from core.production_controller import ProductionController
from strategies.strategy_v6_4 import StrategyV6_4
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot

def main():
    print(f"\n🛡️ INICIANDO SCALPER PRO V6.4 (AUDITED) - {config.SYMBOL} 🛡️")
    
    # 1. INICIALIZACIÓN
    try:
        api = BinanceAPI()
        processor = DataProcessor()
        balance = api.get_balance_usdt()
        risk_mgr = RiskManager(balance=balance, risk_per_trade=config.RISK_PER_TRADE, leverage=config.LEVERAGE)
        strategy = StrategyV6_4()
        state = StateManager()
        tg = TelegramBot(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)
        controller = ProductionController(api, state, tg, config)
        
        tg.send_msg(f"🤖 *Bot V6.4 Audited Iniciado*\nSaldo: `${balance:.2f}`\nController: `ACTIVE`")
    except Exception as e:
        print(f"❌ Error Init: {e}")
        sys.exit(1)

    # VARIABLES DE CONTROL
    daily_pnl_r = 0.0
    consecutive_losses = 0
    last_reset_day = datetime.utcnow().day
    kill_switch_active = False
    last_heartbeat = time.time()

    # CONSTANTES DE FEES (Estimado slippage + comisión)
    ESTIMATED_FEE_R = 0.05 

    while True:
        try:
            now = datetime.utcnow()
            
            # --- HEARTBEAT & RESET ---
            if time.time() - last_heartbeat > (4 * 60 * 60):
                try:
                    curr = api.get_balance_usdt()
                    tg.send_msg(f"💓 *Heartbeat*\nSaldo: `${curr:.2f}`\nPnL Hoy: `{daily_pnl_r:.2f}R`")
                    last_heartbeat = time.time()
                except: pass

            if now.day != last_reset_day:
                daily_pnl_r = 0.0
                consecutive_losses = 0
                kill_switch_active = False
                controller.errors_count = 0 
                last_reset_day = now.day
                tg.send_msg("🗓️ *Nuevo Día UTC* - Reset.")

            if kill_switch_active:
                print(f"💀 Kill Switch Activo. {now.strftime('%H:%M')}")
                time.sleep(300)
                continue

            if now.second != 5 or now.minute % 5 != 0:
                time.sleep(1)
                continue

            print(f"\n--- 🕒 CICLO: {now.strftime('%H:%M:%S')} ---")
            
            # --- AUDITORÍA ---
            if not controller.audit_positions():
                print("⚠️ Auditoría falló. Saltando ciclo.")
                time.sleep(10)
                continue

            # --- DATOS ---
            df = api.fetch_ohlcv(limit=500)
            if df is None:
                controller.errors_count += 1
                time.sleep(10)
                continue
            
            if controller.errors_count > 0: controller.errors_count -= 1

            df = processor.calculate_indicators(df)
            zones = processor.get_volume_profile_zones(df)
            
            if zones is None:
                print("⚠️ Zonas insuficientes.")
                time.sleep(55)
                continue

            current_pos = api.get_position()
            bot_state = state.load_state()

            # --- GESTIÓN ---
            if current_pos and bot_state.get("in_position"):
                entry_price = float(bot_state['entry_price'])
                current_price = float(df.iloc[-1]['close'])
                bars_held = state.update_bars_held()
                
                sl_dist = abs(entry_price - bot_state['stop_loss'])
                if sl_dist == 0: sl_dist = entry_price * 0.01
                
                if current_pos['side'] == 'LONG':
                    pnl_r = (current_price - entry_price) / sl_dist
                else:
                    pnl_r = (entry_price - current_price) / sl_dist
                
                print(f"📊 {bars_held} velas. R: {pnl_r:.2f}")

                # REGLAS DE SALIDA
                should_close = False
                reason = ""

                if bars_held == 2 and pnl_r < -0.10: should_close = True; reason = "Failed FT"
                if bars_held == 4 and pnl_r < 0.25: should_close = True; reason = "Stagnant (Bar 4)"
                if bars_held == 6 and pnl_r < 0.20: should_close = True; reason = "Stagnant Late"
                if bars_held >= 11: should_close = True; reason = "Time Stop"
                if pnl_r >= 3.0: should_close = True; reason = "🎯 TP2 HIT"
                if pnl_r <= -1.1: should_close = True; reason = "🛑 Hard SL"

                # FIX #4: Gestión TP1 Activa (Mover SL Físico)
                if pnl_r >= 1.0 and not bot_state.get("tp1_hit"):
                    state.set_tp1_hit()
                    print("💰 TP1 alcanzado. Moviendo SL a BE...")
                    
                    # Cancelar SL anterior
                    api.exchange.cancel_all_orders(config.SYMBOL)
                    
                    # Nuevo SL en Entrada + un poquito para pagar fees
                    be_price = entry_price * (1.001 if current_pos['side'] == 'LONG' else 0.999)
                    sl_side = 'sell' if current_pos['side'] == 'LONG' else 'buy'
                    
                    # FIX #3: Usamos closePosition=True para el nuevo SL de protección
                    api.place_order(sl_side, current_pos['amount'], 'market', 
                        {'stopPrice': be_price, 'type': 'STOP_MARKET', 'closePosition': True})
                    
                    tg.send_msg(f"🛡️ *TP1 Hit*: SL movido a Break Even ({be_price})")

                if should_close:
                    print(f"⚡ Cerrando: {reason}")
                    api.close_position(current_pos)
                    state.clear_state()
                    
                    # FIX #2: Ajuste de PnL real (Fee Penalty)
                    realized_r_estimate = pnl_r - ESTIMATED_FEE_R
                    daily_pnl_r += realized_r_estimate
                    
                    if realized_r_estimate < -0.8: consecutive_losses += 1
                    elif realized_r_estimate > 0.5: consecutive_losses = 0
                    
                    icon = "✅" if realized_r_estimate > 0 else "❌"
                    tg.send_msg(f"{icon} *Cierre*\nRes: `{realized_r_estimate:.2f}R`\nMotivo: {reason}\nDia: `{daily_pnl_r:.2f}R`")

                    if controller.check_kill_switch(daily_pnl_r, consecutive_losses):
                        kill_switch_active = True

            # --- ENTRADA ---
            elif not current_pos and not bot_state.get("in_position"):
                trade = strategy.get_signal(df, zones)
                
                if trade:
                    print(f"🚀 SEÑAL {trade['type']} @ {trade['entry_price']}")
                    risk_mgr.balance = api.get_balance_usdt()
                    qty_btc = risk_mgr.calculate_position_size(trade['entry_price'], trade['stop_loss'])
                    
                    if qty_btc > 0:
                        order = api.place_order('buy' if trade['type']=='LONG' else 'sell', qty_btc)
                        if order:
                            sl_side = 'sell' if trade['type']=='LONG' else 'buy'
                            
                            # FIX #3: SL Inicial con closePosition=True
                            # Nota: stopPrice es obligatorio. closePosition=True indica "cerrar todo lo que haya"
                            api.place_order(
                                sl_side, 
                                qty_btc, # Cantidad es ignorada si closePosition=True, pero ccxt la pide
                                'market', 
                                {
                                    'stopPrice': trade['stop_loss'], 
                                    'type': 'STOP_MARKET', 
                                    'closePosition': True 
                                }
                            )
                            
                            # FIX #1: Guardar side explícitamente
                            state.set_entry(trade['entry_price'], trade['time'], trade['stop_loss'], trade['type'])
                            
                            tp_price = trade['entry_price'] + (trade['atr']*3) if trade['type']=='LONG' else trade['entry_price'] - (trade['atr']*3)
                            
                            tg.send_msg(f"🚀 *Entrada {trade['type']}*\nP: `{trade['entry_price']}`\nSL: `{trade['stop_loss']:.2f}`\nTP: `{tp_price:.2f}`")

            print("💤 Esperando...")
            time.sleep(55)

        except KeyboardInterrupt:
            sys.exit()
        except Exception as e:
            print(f"❌ Error Loop: {e}")
            controller.errors_count += 1
            if controller.check_kill_switch(daily_pnl_r, consecutive_losses):
                kill_switch_active = True
            time.sleep(10)

if __name__ == "__main__":
    main()