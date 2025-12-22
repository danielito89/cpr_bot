# scalper_pro/main.py

import time
import sys
import os
from datetime import datetime
import pandas as pd

# --- IMPORTACIÓN DE MÓDULOS PROPIOS ---
import config
from core.binance_api import BinanceAPI
from core.data_processor import DataProcessor
from core.risk_manager import RiskManager
from core.production_controller import ProductionController # <--- NUEVO
from strategies.strategy_v6_4 import StrategyV6_4
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot

# ==========================================
# CONFIGURACIÓN DEL SISTEMA
# ==========================================
def main():
    print(f"\n🛡️ INICIANDO SCALPER PRO V6.4 (SAFE MODE) - {config.SYMBOL} 🛡️")
    print(f"🌍 Modo: {'DRY RUN' if config.DRY_RUN else 'LIVE'}")

    # 1. INICIALIZACIÓN DE CLASES
    try:
        api = BinanceAPI()
        processor = DataProcessor()
        balance = api.get_balance_usdt()
        risk_mgr = RiskManager(balance=balance, risk_per_trade=config.RISK_PER_TRADE, leverage=config.LEVERAGE)
        strategy = StrategyV6_4()
        state = StateManager()
        tg = TelegramBot(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)
        
        # Inicializar el Controlador de Producción
        controller = ProductionController(api, state, tg, config)
        
        start_msg = (
            f"🤖 *Bot Iniciado (V6.4 Safe)*\n"
            f"Par: `{config.SYMBOL}`\n"
            f"Saldo: `${balance:.2f}`\n"
            f"🛡️ *Production Controller: ACTIVE*"
        )
        tg.send_msg(start_msg)
        print("✅ Sistemas cargados correctamente.")

    except Exception as e:
        print(f"❌ Error Crítico al iniciar: {e}")
        sys.exit(1)

    # 2. VARIABLES DE CONTROL
    daily_pnl_r = 0.0
    consecutive_losses = 0
    last_reset_day = datetime.utcnow().day
    kill_switch_active = False
    last_heartbeat = time.time()

    # ==========================================
    # BUCLE PRINCIPAL (INFINITO)
    # ==========================================
    while True:
        try:
            now = datetime.utcnow()
            
            # --- A. HEARTBEAT (Cada 4 horas) ---
            if time.time() - last_heartbeat > (4 * 60 * 60):
                try:
                    curr_bal = api.get_balance_usdt()
                    hb_msg = f"💓 *Heartbeat*\nSaldo: `${curr_bal:.2f}`\nPnL Hoy: `{daily_pnl_r:.2f}R`"
                    tg.send_msg(hb_msg)
                    last_heartbeat = time.time()
                except: pass

            # --- B. RESET DIARIO ---
            if now.day != last_reset_day:
                daily_pnl_r = 0.0
                consecutive_losses = 0
                kill_switch_active = False
                controller.errors_count = 0 # Resetear errores de API
                last_reset_day = now.day
                tg.send_msg(f"🗓️ *Nuevo Día UTC* - Métricas reseteadas.")

            # --- C. VERIFICACIÓN DE KILL SWITCH ---
            # Delegamos esto al controlador, pero mantenemos el flag local para el loop
            if kill_switch_active:
                print(f"💀 Kill Switch Activo. Hora: {now.strftime('%H:%M')}")
                time.sleep(300) 
                continue

            # --- D. SINCRONIZACIÓN (Segundo 05) ---
            if now.second != 5 or now.minute % 5 != 0:
                time.sleep(1)
                continue

            print(f"\n--- 🕒 CICLO: {now.strftime('%H:%M:%S')} ---")
            
            # -------------------------------------------------
            # E. AUDITORÍA DE SEGURIDAD (Reconciliación)
            # -------------------------------------------------
            # Esto verifica Zombies y Fantasmas ANTES de operar
            is_healthy = controller.audit_positions()
            
            if not is_healthy:
                print("⚠️ Auditoría detectó discrepancias. Corrigiendo y saltando ciclo.")
                time.sleep(10)
                continue

            # -------------------------------------------------
            # F. DATOS Y LÓGICA
            # -------------------------------------------------
            df = api.fetch_ohlcv(limit=500)
            if df is None:
                controller.errors_count += 1
                time.sleep(10)
                continue
            
            # Si descargó bien, bajamos contador de errores
            if controller.errors_count > 0: controller.errors_count -= 1

            df = processor.calculate_indicators(df)
            zones = processor.get_volume_profile_zones(df)
            
            if zones is None:
                print("⚠️ Datos insuficientes para Volume Profile.")
                time.sleep(55)
                continue

            current_pos = api.get_position()
            bot_state = state.load_state()

            # --- GESTIÓN DE POSICIÓN ---
            if current_pos and bot_state.get("in_position"):
                entry_price = float(bot_state['entry_price'])
                current_price = float(df.iloc[-1]['close'])
                bars_held = state.update_bars_held()
                
                # R Calc
                sl_dist = abs(entry_price - bot_state['stop_loss'])
                if sl_dist == 0: sl_dist = entry_price * 0.01 # Evitar div/0
                
                if current_pos['side'] == 'LONG':
                    pnl_r = (current_price - entry_price) / sl_dist
                else:
                    pnl_r = (entry_price - current_price) / sl_dist
                
                print(f"📊 Gestión: {bars_held} velas. R: {pnl_r:.2f}. PnL USD: {current_pos['pnl']:.2f}")

                # REGLAS DE SALIDA (V6.4)
                should_close = False
                reason = ""

                if bars_held == 2 and pnl_r < -0.10: should_close = True; reason = "Failed FT"
                if bars_held == 4 and pnl_r < 0.25: should_close = True; reason = "Stagnant (Bar 4)"
                if bars_held == 6 and pnl_r < 0.20: should_close = True; reason = "Stagnant Late"
                if bars_held >= 11: should_close = True; reason = "Time Stop"
                if pnl_r >= 3.0: should_close = True; reason = "🎯 TP2 HIT"
                if pnl_r <= -1.1: should_close = True; reason = "🛑 Hard SL"

                if pnl_r >= 1.0 and not bot_state.get("tp1_hit"):
                    state.set_tp1_hit()
                    print("💰 TP1 alcanzado.")

                if should_close:
                    print(f"⚡ Cerrando: {reason}")
                    api.close_position(current_pos)
                    state.clear_state()
                    
                    daily_pnl_r += pnl_r
                    if pnl_r < -0.8: consecutive_losses += 1
                    elif pnl_r > 0.5: consecutive_losses = 0
                    
                    icon = "✅" if pnl_r > 0 else "❌"
                    tg.send_msg(f"{icon} *Cierre*\nRes: `{pnl_r:.2f}R`\nMotivo: {reason}\nDia: `{daily_pnl_r:.2f}R`")

                    # Chequear Kill Switch post-trade
                    if controller.check_kill_switch(daily_pnl_r, consecutive_losses):
                        kill_switch_active = True

            # --- BUSCAR ENTRADA ---
            elif not current_pos and not bot_state.get("in_position"):
                trade = strategy.get_signal(df, zones)
                
                if trade:
                    print(f"🚀 SEÑAL {trade['type']} @ {trade['entry_price']}")
                    risk_mgr.balance = api.get_balance_usdt()
                    qty_btc = risk_mgr.calculate_position_size(trade['entry_price'], trade['stop_loss'])
                    
                    if qty_btc > 0:
                        order = api.place_order('buy' if trade['type']=='LONG' else 'sell', qty_btc)
                        if order:
                            # SL Order
                            sl_side = 'sell' if trade['type']=='LONG' else 'buy'
                            api.place_order(sl_side, qty_btc, 'market', {'stopPrice': trade['stop_loss'], 'type': 'STOP_MARKET', 'reduceOnly': True})
                            
                            # TP Calculado para Telegram
                            tp_price = trade['entry_price'] + (trade['atr']*3) if trade['type']=='LONG' else trade['entry_price'] - (trade['atr']*3)

                            state.set_entry(trade['entry_price'], trade['time'], trade['stop_loss'], 0, 0)
                            
                            msg = (
                                f"🚀 *Entrada {trade['type']}*\n"
                                f"P: `{trade['entry_price']}`\n"
                                f"SL: `{trade['stop_loss']:.2f}`\n"
                                f"TP: `{tp_price:.2f}`"
                            )
                            tg.send_msg(msg)

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