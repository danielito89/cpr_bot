#!/usr/bin/env python3
import ccxt
import pandas as pd
import numpy as np
import talib
import time
import os
import requests
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# ======================================================
#  ⚙️ CONFIGURACIÓN DE PRODUCCIÓN (V66 AUDITED)
# ======================================================

API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- LISTA DE SÍMBOLOS DEFINITIVA ---
SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    "1000PEPE/USDT:USDT"
]

TIMEFRAME = "1h"       
RESAMPLE_TF = "4h"     
FAST_EMA = 50
SLOW_EMA = 200

# --- GESTIÓN DE RIESGO (FIX 3: 3% Conservador) ---
LEVERAGE = 5
RISK_PER_TRADE = 0.03  # 3% riesgo real por operación
SL_ATR_MULT = 3.0      

# --- SISTEMA ---
DRY_RUN = False        # ¡DINERO REAL!
STATE_FILE = "bot_state.json"

# ======================================================
#  UTILIDADES & PERSISTENCIA (FIX 5)
# ======================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try: requests.post(url, data=data, timeout=5)
    except Exception as e: print(f"Error Telegram: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f: return json.load(f)
        except: return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f: json.dump(state, f)
    except Exception as e: print(f"Error guardando estado: {e}")

def get_exchange():
    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })
    exchange.load_markets()
    return exchange

# ======================================================
#  LÓGICA CORE (CEREBRO)
# ======================================================

def analyze_symbol(exchange, symbol):
    print(f"🔍 Analizando {symbol}...")
    try:
        # Descarga con margen suficiente
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=1000)
    except Exception as e:
        print(f"Error descargando {symbol}: {e}")
        return None

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)

    # Resampling 4H
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample(RESAMPLE_TF).agg(ohlc_dict).dropna()

    # Indicadores
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)

    # Última vela CERRADA (-2)
    last_candle = df_4h.iloc[-2]
    prev_candle = df_4h.iloc[-3]
    
    # Timestamp de la vela de señal (para evitar duplicados)
    signal_timestamp = str(last_candle.name)

    ticker = exchange.fetch_ticker(symbol)
    current_price = ticker['last']

    # Señales
    golden_cross = (last_candle['ema_fast'] > last_candle['ema_slow']) and \
                   (prev_candle['ema_fast'] <= prev_candle['ema_slow'])
    
    death_cross = (last_candle['ema_fast'] < last_candle['ema_slow']) and \
                  (prev_candle['ema_fast'] >= prev_candle['ema_slow'])

    return {
        "symbol": symbol,
        "price": current_price,
        "signal_buy": golden_cross,
        "signal_sell": death_cross,
        "atr": last_candle['atr'],
        "candle_ts": signal_timestamp
    }

# ======================================================
#  EJECUCIÓN DE ÓRDENES (AUDITED)
# ======================================================

def execute_logic(exchange, data):
    symbol = data['symbol']
    price = data['price']
    
    # --- FIX 2: LECTURA ROBUSTA DE POSICIÓN ---
    pos_amt = 0.0
    try:
        positions = exchange.fetch_positions([symbol])
        target_pos = None
        for p in positions:
            if p['symbol'] == symbol:
                target_pos = p
                break
        
        # Lectura segura: Si es None o vacío, devuelve 0
        if target_pos:
            pos_amt = float(target_pos.get('contracts', 0) or 0)
            
    except Exception as e:
        print(f"⚠️ Error leyendo posición {symbol}: {e}")
        return # Abortar por seguridad

    print(f"   Posición: {pos_amt} contratos")

    # --- FIX 1: GARBAGE COLLECTOR (LIMPIEZA DE SL HUÉRFANOS) ---
    # Si no tenemos posición, nos aseguramos de que no haya órdenes basura
    #if pos_amt == 0:
    #    try:
    #        open_orders = exchange.fetch_open_orders(symbol)
    #        if len(open_orders) > 0:
    #            print(f"   🧹 Limpiando {len(open_orders)} órdenes huérfanas en {symbol}...")
    #            exchange.cancel_all_orders(symbol)
    #    except Exception as e:
    #        print(f"⚠️ Error limpiando órdenes: {e}")

    # --- LÓGICA DE ENTRADA (LONG) ---
    if data['signal_buy'] and pos_amt == 0:
        
        # --- FIX 5: ANTI-DUPLICADO DE SEÑAL ---
        state = load_state()
        last_trade_ts = state.get(symbol)
        current_signal_ts = data['candle_ts']
        
        if last_trade_ts == current_signal_ts:
            print(f"   🚫 Señal duplicada (Ya operada en vela {current_signal_ts}). Ignorando.")
            return

        msg = f"🚀 **GOLDEN CROSS: {symbol}**\nPrecio: {price}\nIniciando..."
        print(msg)
        send_telegram(msg)
        
        if not DRY_RUN:
            try:
                balance = exchange.fetch_balance()['USDT']['free']
                risk_amt = balance * RISK_PER_TRADE
                
                sl_dist = data['atr'] * SL_ATR_MULT
                if sl_dist == 0: sl_dist = price * 0.02 # Fallback

                # Sizing
                qty_usdt = (risk_amt / sl_dist) * price
                max_pos = balance * LEVERAGE
                qty_usdt = min(qty_usdt, max_pos)
                
                qty_contracts = qty_usdt / price
                
                # Precisión y Mínimo
                market = exchange.market(symbol)
                qty_contracts = exchange.amount_to_precision(symbol, qty_contracts)
                
                if (float(qty_contracts) * price) < 6:
                    print("   ⚠️ Orden muy pequeña (<6 USDT).")
                    return

                # Ejecución
                try: exchange.set_leverage(LEVERAGE, symbol)
                except: pass
                
                # 1. MARKET BUY
                print(f"   🛒 Enviando Market Buy: {qty_contracts}")
                order = exchange.create_market_buy_order(symbol, qty_contracts)
                
                # --- FIX 4: CÁLCULO DE SL POST-ENTRY ---
                # Usamos el precio real de ejecución ('average'), no el del ticker
                real_entry = float(order['average']) if order.get('average') else price
                
                sl_price = real_entry - sl_dist
                sl_price = float(exchange.price_to_precision(symbol, sl_price))

                # 2. STOP LOSS
                exchange.create_order(symbol, 'stop_market', 'sell', qty_contracts, None, {'stopPrice': sl_price, 'reduceOnly': True})
                
                # Guardar Estado (Persistencia)
                state[symbol] = current_signal_ts
                save_state(state)

                send_telegram(f"✅ **Entrada Confirmada**\nSymbol: {symbol}\nEntry: {real_entry}\nSL: {sl_price}\nSize: {qty_contracts}")
                
            except Exception as e:
                print(f"❌ Error Entry: {e}")
                send_telegram(f"❌ Error Entry {symbol}: {e}")

    # --- LÓGICA DE SALIDA (DEATH CROSS) ---
    elif data['signal_sell'] and pos_amt > 0:
        msg = f"💀 **DEATH CROSS: {symbol}**\nCerrando..."
        print(msg)
        send_telegram(msg)
        
        if not DRY_RUN:
            try:
                # 1. Close Position
                exchange.create_market_sell_order(symbol, pos_amt, {'reduceOnly': True})
                # 2. Cancelar SL pendiente
                exchange.cancel_all_orders(symbol)
                
                send_telegram(f"✅ Salida Exitosa: {symbol}")
            except Exception as e:
                print(f"❌ Error Exit: {e}")
                send_telegram(f"❌ Error Exit {symbol}: {e}")

# ======================================================
#  BUCLE PRINCIPAL
# ======================================================

def main():
    print("🤖 INICIANDO CPR_BOT V1 (AUDITED)...")
    send_telegram("🤖 **Bot Iniciado (Audit Version)**\nModo: DINERO REAL")
    
    exchange = get_exchange()
    
    while True:
        try:
            print(f"\n🕒 Scan: {datetime.now().strftime('%H:%M')}")
            
            for symbol in SYMBOLS:
                data = analyze_symbol(exchange, symbol)
                if data:
                    execute_logic(exchange, data)
                time.sleep(2) # Respetar rate limits
            
            print("😴 Durmiendo...")
            
            # Sincronización precisa con la vela de 1H
            now = datetime.now()
            sleep_sec = 3600 - (now.minute * 60 + now.second) + 10 # +10s buffer
            time.sleep(sleep_sec)
            
        except KeyboardInterrupt:
            print("\n🛑 Detenido.")
            break
        except Exception as e:
            print(f"❌ Error Loop: {e}")
            send_telegram(f"⚠️ Error Loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()