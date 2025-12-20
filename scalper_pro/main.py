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
from strategies.strategy_v6_4 import StrategyV6_4
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot

# ==========================================
# CONFIGURACIÓN DEL SISTEMA
# ==========================================
def main():
    print(f"\n🔥🔥 INICIANDO SCALPER PRO V6.4 - {config.SYMBOL} 🔥🔥")
    print(f"🌍 Modo: {'DRY RUN (Simulacro)' if config.DRY_RUN else 'LIVE TRADING (Dinero Real)'}")
    print(f"🎰 Leverage: {config.LEVERAGE}x | Riesgo por Trade: {config.RISK_PER_TRADE*100}%")

    # 1. INICIALIZACIÓN DE CLASES
    try:
        api = BinanceAPI()
        processor = DataProcessor()
        # Inicializamos Risk Manager con saldo actual
        balance = api.get_balance_usdt()
        risk_mgr = RiskManager(balance=balance, risk_per_trade=config.RISK_PER_TRADE, leverage=config.LEVERAGE)
        strategy = StrategyV6_4()
        state = StateManager()
        tg = TelegramBot(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)
        
        # Mensaje de inicio a Telegram
        start_msg = (
            f"🤖 *Bot Iniciado (V6.4)*\n"
            f"Par: `{config.SYMBOL}`\n"
            f"Saldo: `${balance:.2f}`\n"
            f"Modo: `{'SIMULADO' if config.DRY_RUN else 'REAL'}`"
        )
        tg.send_msg(start_msg)
        print("✅ Sistemas cargados correctamente.")

    except Exception as e:
        print(f"❌ Error Crítico al iniciar: {e}")
        sys.exit(1)

    # 2. VARIABLES DE CONTROL (KILL SWITCH)
    daily_pnl_r = 0.0
    consecutive_losses = 0
    last_reset_day = datetime.utcnow().day
    kill_switch_active = False

    # ==========================================
    # BUCLE PRINCIPAL (INFINITO)
    # ==========================================
    while True:
        try:
            # -------------------------------------------------
            # A. SINCRONIZACIÓN DE TIEMPO
            # -------------------------------------------------
            now = datetime.utcnow()
            
            # Reset diario de métricas (00:00 UTC)
            if now.day != last_reset_day:
                daily_pnl_r = 0.0
                consecutive_losses = 0
                kill_switch_active = False
                last_reset_day = now.day
                tg.send_msg(f"🗓️ *Nuevo Día UTC* - Contadores de riesgo reseteados.")

            # Chequeo de Kill Switch
            if kill_switch_active:
                print(f"💀 Kill Switch Activo. Esperando reset diario. Hora: {now.strftime('%H:%M')}")
                time.sleep(300) # Dormir 5 minutos
                continue

            # Sincronización con velas de 5 minutos (esperar al segundo 05)
            if now.second != 5 or now.minute % 5 != 0:
                time.sleep(1)
                continue

            print(f"\n--- 🕒 CICLO: {now.strftime('%H:%M:%S')} ---")

            # -------------------------------------------------
            # B. OBTENCIÓN Y PROCESAMIENTO DE DATOS
            # -------------------------------------------------
            df = api.fetch_ohlcv(limit=500)
            if df is None:
                print("⚠️ Error descargando datos. Reintentando...")
                time.sleep(10)
                continue

            # Calcular Indicadores y Zonas
            df = processor.calculate_indicators(df)
            zones = processor.get_volume_profile_zones(df)
            
            if zones is None:
                print("⚠️ Datos insuficientes para Volume Profile.")
                time.sleep(55)
                continue

            # -------------------------------------------------
            # C. GESTIÓN DE ESTADO Y POSICIÓN
            # -------------------------------------------------
            current_pos = api.get_position()
            bot_state = state.load_state()

            # CASO 1: GESTIÓN DE POSICIÓN ABIERTA
            if current_pos:
                if not bot_state.get("in_position"):
                    print("⚠️ Detectada posición huérfana en Binance. Ignorando por seguridad.")
                else:
                    # Datos de la posición
                    entry_price = float(bot_state['entry_price'])
                    current_price = float(df.iloc[-1]['close']) # Cierre de la vela actual
                    bars_held = state.update_bars_held()
                    
                    # Calcular R (Riesgo Actual)
                    sl_dist = abs(entry_price - bot_state['stop_loss'])
                    if current_pos['side'] == 'LONG':
                        pnl_r = (current_price - entry_price) / sl_dist
                    else:
                        pnl_r = (entry_price - current_price) / sl_dist
                    
                    print(f"📊 Gestión: {bars_held} velas. R Actual: {pnl_r:.2f} R. PnL USD: {current_pos['pnl']:.2f}")

                    # --- LÓGICA DE SALIDA V6.4 ---
                    should_close = False
                    reason = ""

                    # 1. Failed Follow-Through (Barra 2)
                    if bars_held == 2 and pnl_r < -0.10:
                        should_close = True; reason = "Failed FT (V6.1)"

                    # 2. Aggressive Stagnant (Barra 4) - Si no gana +0.25R, fuera.
                    if bars_held == 4 and pnl_r < 0.25:
                        should_close = True; reason = "Stagnant (V6.4)"

                    # 3. Standard Stagnant (Barra 6)
                    if bars_held == 6 and pnl_r < 0.20:
                        should_close = True; reason = "Stagnant Late"

                    # 4. Time Stop (Barra 11)
                    if bars_held >= 11:
                        should_close = True; reason = "Time Stop"

                    # 5. Take Profit 2 (Home Run)
                    if pnl_r >= 3.0:
                        should_close = True; reason = "🎯 TP2 HIT (+3R)"

                    # 6. Stop Loss (Protección final)
                    if pnl_r <= -1.1:
                        should_close = True; reason = "🛑 Hard SL"

                    # Check TP1 Mental (Solo notificar)
                    if pnl_r >= 1.0 and not bot_state.get("tp1_hit"):
                        state.set_tp1_hit()
                        print("💰 TP1 alcanzado (1R).")

                    # EJECUTAR CIERRE
                    if should_close:
                        print(f"⚡ Cerrando posición: {reason}")
                        api.close_position(current_pos)
                        state.clear_state()
                        
                        # Actualizar Métricas de Riesgo
                        daily_pnl_r += pnl_r
                        
                        # Lógica de Rachas (Riesgo Secuencial)
                        if pnl_r < -0.8: # Consideramos pérdida real peor que -0.8R
                            consecutive_losses += 1
                        elif pnl_r > 0.5: # Si gana bien, reseteamos racha
                            consecutive_losses = 0
                        
                        # Notificar Telegram
                        icon = "✅" if pnl_r > 0 else "❌"
                        if "Stagnant" in reason or "Failed" in reason: icon = "⚠️"
                        
                        close_msg = (
                            f"{icon} *Trade Cerrado*\n"
                            f"Res: `{pnl_r:.2f} R` (${current_pos['pnl']:.2f})\n"
                            f"Motivo: _{reason}_\n"
                            f"Acumulado Diario: `{daily_pnl_r:.2f} R`"
                        )
                        tg.send_msg(close_msg)

                        # VERIFICAR KILL SWITCH
                        if daily_pnl_r <= -3.0 or consecutive_losses >= 3:
                            kill_switch_active = True
                            kill_msg = (
                                f"💀 *KILL SWITCH ACTIVADO*\n"
                                f"PnL Diario: {daily_pnl_r:.2f}R\n"
                                f"Racha Perdidas: {consecutive_losses}\n"
                                f"⛔ Trading detenido hasta mañana."
                            )
                            tg.send_msg(kill_msg)
                            print("💀 KILL SWITCH ACTIVADO.")

            # CASO 2: BÚSQUEDA DE ENTRADA
            else:
                # Si el bot cree que tiene posición pero no hay nada en Binance (SL saltó o cierre manual)
                if bot_state.get("in_position"):
                    print("ℹ️ Sincronizando estado: Posición cerrada externamente.")
                    state.clear_state()

                # Buscar Señal V6.4
                trade = strategy.get_signal(df, zones)
                
                if trade:
                    print(f"🚀 SEÑAL {trade['type']} detectada @ {trade['entry_price']}")
                    
                    # Recalcular saldo para tamaño correcto
                    current_balance = api.get_balance_usdt()
                    risk_mgr.balance = current_balance 
                    
                    qty_btc = risk_mgr.calculate_position_size(trade['entry_price'], trade['stop_loss'])
                    
                    if qty_btc > 0:
                        # 1. Enviar Orden de Mercado
                        order = api.place_order(
                            side='buy' if trade['type'] == 'LONG' else 'sell',
                            amount=qty_btc,
                            order_type='market'
                        )
                        
                        if order:
                            # 2. Enviar Stop Loss (Reduce Only)
                            sl_side = 'sell' if trade['type'] == 'LONG' else 'buy'
                            api.place_order(
                                sl_side, 
                                qty_btc, 
                                'market', # Stop Market
                                params={'stopPrice': trade['stop_loss'], 'type': 'STOP_MARKET', 'reduceOnly': True}
                            )
                            
                            # 3. Guardar Estado
                            state.set_entry(
                                price=trade['entry_price'],
                                time_str=trade['time'],
                                sl=trade['stop_loss'],
                                tp1=0, # No usamos orders limit para TP
                                tp2=0
                            )
                            
                            # 4. Notificar Telegram
                            entry_msg = (
                                f"🚀 *Entrada {trade['type']} Ejecutada*\n"
                                f"Precio: `{trade['entry_price']}`\n"
                                f"Size: `{qty_btc} BTC`\n"
                                f"SL: `{trade['stop_loss']:.2f}`"
                            )
                            tg.send_msg(entry_msg)
                    else:
                        print("⚠️ Saldo insuficiente para abrir posición mínima.")

            # Dormir hasta el siguiente ciclo (aprox 55 seg para no spamear CPU)
            print("💤 Esperando siguiente vela...")
            time.sleep(55)

        except KeyboardInterrupt:
            print("\n👋 Bot detenido manualmante.")
            sys.exit()
        except Exception as e:
            print(f"❌ Error en bucle principal: {e}")
            tg.send_msg(f"⚠️ *Error del Sistema*: {str(e)}")
            time.sleep(10) # Espera de seguridad ante errores

if __name__ == "__main__":
    main()