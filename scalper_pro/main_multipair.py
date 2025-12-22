import time
import sys
import os
from datetime import datetime
import pandas as pd

import config
from core.binance_api import BinanceAPI
from core.data_processor import DataProcessor
from core.risk_manager import RiskManager
from strategies.strategy_v6_4 import StrategyV6_4
from addons.state_manager import StateManager
from addons.telegram_bot import TelegramBot

def main():
    print(f"\n🐲 INICIANDO SCALPER PRO MULTIPAIR (HYDRA V1) 🐲")
    print(f"Pares: {config.PAIRS}")
    
    try:
        api = BinanceAPI()
        processor = DataProcessor()
        # Risk Manager se actualiza dinámicamente con el saldo
        risk_mgr = RiskManager(balance=0, risk_per_trade=config.RISK_PER_TRADE, leverage=config.LEVERAGE)
        strategy = StrategyV6_4()
        state = StateManager()
        tg = TelegramBot(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)
        
        tg.send_msg(f"🐲 *Hydra Multipair Iniciado*\nActivos: `{len(config.PAIRS)}`\nModo: `{'DRY RUN' if config.DRY_RUN else 'LIVE'}`")

    except Exception as e:
        print(f"❌ Error Init: {e}")
        sys.exit(1)

    # Variables Globales
    last_reset_day = datetime.utcnow().day
    # PnL diario acumulado global
    daily_pnl_r = 0.0 
    
    # Cache para no spamear API de telegram
    last_heartbeat = time.time()

    while True:
        try:
            now = datetime.utcnow()
            
            # --- 1. HEARTBEAT & RESET ---
            if time.time() - last_heartbeat > (4 * 60 * 60):
                bal = api.get_balance_usdt()
                tg.send_msg(f"💓 *Heartbeat Multipair*\nSaldo: `${bal:.2f}`\nPnL Hoy: `{daily_pnl_r:.2f}R`")
                last_heartbeat = time.time()

            if now.day != last_reset_day:
                daily_pnl_r = 0.0
                last_reset_day = now.day
                tg.send_msg("🗓️ *Nuevo Día UTC* - Métricas reseteadas.")

            # Sincronización (Segundo 05)
            if now.second != 5 or now.minute % 5 != 0:
                time.sleep(1)
                continue

            print(f"\n--- 🕒 CICLO: {now.strftime('%H:%M:%S')} ---")
            
            # Actualizar Saldo Global para Risk Manager
            current_balance = api.get_balance_usdt()
            risk_mgr.balance = current_balance

            # ==========================================
            # BUCLE POR PAR (La Hidra ataca)
            # ==========================================
            for symbol in config.PAIRS:
                print(f"🔍 Analizando {symbol}...")
                
                # A. Descargar Datos
                # Nota: Necesitamos actualizar binance_api para aceptar symbol como argumento
                # Hack temporal: inyectamos symbol en config dinámicamente o pasamos argumento
                # La forma limpia es pasar symbol a fetch_ohlcv
                
                # -> REQUIERE PEQUEÑO CAMBIO EN binance_api.py: def fetch_ohlcv(self, symbol, limit=500)
                # Asumiremos que hiciste ese cambio o usamos api.exchange.fetch_ohlcv directo aqui
                try:
                    ohlcv = api.exchange.fetch_ohlcv(symbol, config.TIMEFRAME, limit=500)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    cols = ['open', 'high', 'low', 'close', 'volume']
                    df[cols] = df[cols].astype(float)
                except Exception as e:
                    print(f"⚠️ Error data {symbol}: {e}")
                    continue

                # B. Indicadores
                df = processor.calculate_indicators(df)
                zones = processor.get_volume_profile_zones(df)
                
                if zones is None: continue

                # C. Estado
                bot_state = state.get_pair_state(symbol)
                
                # Buscar posición en Binance para este símbolo
                # Nota: get_position debe filtrar por símbolo ahora
                all_positions = api.exchange.fetch_positions([symbol])
                current_pos = None
                for p in all_positions:
                    if float(p['contracts']) > 0:
                        current_pos = {
                            'side': p['side'].upper(),
                            'amount': float(p['contracts']),
                            'entry_price': float(p['entryPrice']),
                            'pnl': float(p['unrealizedPnl']),
                            'symbol': symbol
                        }
                        break

                # --- GESTIÓN ---
                if current_pos and bot_state.get("in_position"):
                    entry_price = float(bot_state['entry_price'])
                    current_price = float(df.iloc[-1]['close'])
                    bars_held = state.update_bars_held(symbol)
                    
                    sl_dist = abs(entry_price - bot_state['stop_loss'])
                    if sl_dist == 0: sl_dist = entry_price * 0.01
                    
                    if current_pos['side'] == 'LONG':
                        pnl_r = (current_price - entry_price) / sl_dist
                    else:
                        pnl_r = (entry_price - current_price) / sl_dist
                    
                    print(f"   📊 {symbol}: {bars_held} velas. R: {pnl_r:.2f}")

                    should_close = False
                    reason = ""
                    
                    # Reglas V6.4
                    if bars_held == 2 and pnl_r < -0.10: should_close = True; reason = "Failed FT"
                    if bars_held == 4 and pnl_r < 0.25: should_close = True; reason = "Stagnant (Bar 4)"
                    if bars_held == 6 and pnl_r < 0.20: should_close = True; reason = "Stagnant Late"
                    if bars_held >= 11: should_close = True; reason = "Time Stop"
                    if pnl_r >= 3.0: should_close = True; reason = "🎯 TP2 HIT"
                    if pnl_r <= -1.1: should_close = True; reason = "🛑 Hard SL"

                    # TP1 BE Move
                    if pnl_r >= 1.0 and not bot_state.get("tp1_hit"):
                        state.set_tp1_hit(symbol)
                        # Cancelar SL y mover a BE
                        api.exchange.cancel_all_orders(symbol)
                        be_price = entry_price * (1.001 if current_pos['side']=='LONG' else 0.999)
                        sl_side = 'sell' if current_pos['side']=='LONG' else 'buy'
                        api.place_order(sl_side, current_pos['amount'], 'market', 
                            {'stopPrice': be_price, 'type': 'STOP_MARKET', 'closePosition': True, 'symbol': symbol})
                        tg.send_msg(f"🛡️ *{symbol} TP1 Hit*: SL a BE")

                    if should_close:
                        print(f"   ⚡ Cerrando {symbol}: {reason}")
                        # Cerrar (Market)
                        side_close = 'sell' if current_pos['side'] == 'LONG' else 'buy'
                        api.place_order(side_close, current_pos['amount'], 'market', params={'symbol': symbol})
                        state.clear_pair_state(symbol)
                        
                        daily_pnl_r += (pnl_r - 0.05)
                        icon = "✅" if pnl_r > 0 else "❌"
                        tg.send_msg(f"{icon} *Cierre {symbol}*\nRes: `{pnl_r:.2f}R`\nMotivo: {reason}")

                # --- ENTRADA ---
                elif not current_pos and not bot_state.get("in_position"):
                    trade = strategy.get_signal(df, zones)
                    
                    if trade:
                        print(f"   🚀 SEÑAL {symbol} {trade['type']}")
                        qty = risk_mgr.calculate_position_size(trade['entry_price'], trade['stop_loss'])
                        
                        if qty > 0:
                            # 1. Market Order
                            params = {'symbol': symbol} # CCXT necesita el simbolo a veces en params o arg
                            # Nota: update place_order to accept symbol arg or pass in params
                            # Hack rápido: usar exchange directo
                            try:
                                api.exchange.create_market_order(symbol, 'buy' if trade['type']=='LONG' else 'sell', qty)
                                
                                # 2. SL Order
                                sl_side = 'sell' if trade['type']=='LONG' else 'buy'
                                api.exchange.create_order(symbol, 'STOP_MARKET', sl_side, qty, None, 
                                    {'stopPrice': trade['stop_loss'], 'closePosition': True})
                                
                                state.set_entry(symbol, trade['entry_price'], trade['time'], trade['stop_loss'], trade['type'])
                                tg.send_msg(f"🚀 *Entrada {symbol} {trade['type']}*\nSize: `{qty}`\nSL: `{trade['stop_loss']}`")
                            except Exception as e:
                                print(f"❌ Error ejecutando orden {symbol}: {e}")

            # Fin del loop de pares
            print("💤 Ciclo terminado. Esperando...")
            time.sleep(55)

        except KeyboardInterrupt:
            sys.exit()
        except Exception as e:
            print(f"❌ Error Loop Global: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()