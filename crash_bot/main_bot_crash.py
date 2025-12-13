#!/usr/bin/env python3
import ccxt
import pandas as pd
import numpy as np
import talib
import time
import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ======================================================
#  🌪️ CONFIG V76 – THE SURGEON (MICRO-STRUCTURE FIX)
# ======================================================

API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", 
    "BNB/USDT:USDT", "ADA/USDT:USDT", "1000PEPE/USDT:USDT"
]

TIMEFRAME = "1h"
CRASH_WINDOW_24H = 24
DROP_THRESHOLD_24H = 0.08   # 8% Drop
CRASH_WINDOW_6H = 6
ACCEL_THRESHOLD_6H = 0.05   # 5% Accel

# Gestión
FIXED_RISK_PCT = 0.025      # 2.5% Riesgo
SL_ATR_MULT = 2.0           # SL = 2x ATR (Previo)
TP1_PCT = 0.06; TP1_SIZE = 0.40
TP2_PCT = 0.12; TP2_SIZE = 0.30
# El resto trailing

TRAILING_START_PCT = 0.03
TRAILING_DIST_PCT = 0.03

# Sistema
STATE_FILE = "crash_bot_state.json"
MAX_ACTIVE_TRADES = 1
COOLDOWN_HOURS = 48
DRY_RUN = False 

# ======================================================
#  UTILIDADES
# ======================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f: return json.load(f)
        except: pass
    return {"last_trade_ts": 0, "active_symbol": None, "lowest_price": 0, "last_amt": 0.0}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f: json.dump(state, f)
    except: pass

def get_exchange():
    return ccxt.binance({
        'apiKey': API_KEY, 'secret': SECRET_KEY,
        'options': {'defaultType': 'future'}, 'enableRateLimit': True
    })

# ======================================================
#  CEREBRO (DETECCIÓN)
# ======================================================

def analyze_market(exchange, symbol):
    try:
        # Traemos un poco más de datos para el ATR estable
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=300)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # --- FIX B: ATR ESTABLE (Vela Cerrada -2) ---
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)
        stable_atr = df['atr'].iloc[-2] # Usamos el de la vela anterior cerrada
        
        # Régimen Diario
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms').dt.date
        daily_close = df.groupby('date')['close'].last()
        ema50_d = talib.EMA(daily_close, 50).iloc[-1]
        ema200_d = talib.EMA(daily_close, 200).iloc[-1]
        bear_regime = ema50_d < ema200_d

        # Trigger Crash (Calculado sobre vela cerrada anterior para señal base)
        # Nota: La señal "raw" la basamos en cierres para la tendencia, 
        # pero la ejecución será por intrabar low.
        close_now = df['close'].iloc[-1] # Precio actual (vela formándose)
        close_24h = df['close'].iloc[-1 - CRASH_WINDOW_24H]
        close_6h = df['close'].iloc[-1 - CRASH_WINDOW_6H]
        
        drop_24h = (close_now - close_24h) / close_24h
        drop_6h = (close_now - close_6h) / close_6h
        
        is_crash = (drop_24h < -DROP_THRESHOLD_24H) and (drop_6h < -ACCEL_THRESHOLD_6H)
        
        # --- FIX A: ENTRADA INTRABAR ---
        # Low de la vela previa (Soporte a romper)
        prev_low = df['low'].iloc[-2]
        # Low de la vela actual (Intrabar)
        current_low = df['low'].iloc[-1]
        
        return {
            'symbol': symbol,
            'signal_ready': is_crash and bear_regime, # Señal activa, falta trigger precio
            'prev_low': prev_low,
            'current_low': current_low,
            'close': close_now,
            'atr': stable_atr
        }
    except Exception as e:
        print(f"Error analizando {symbol}: {e}")
        return None

# ======================================================
#  EJECUCIÓN & GESTIÓN
# ======================================================

def manage_position(exchange, state):
    symbol = state['active_symbol']
    if not symbol: return state

    try:
        # Estado Actual
        positions = exchange.fetch_positions([symbol])
        pos = [p for p in positions if p['symbol'] == symbol][0]
        current_amt = float(pos['contracts']) if pos['contracts'] else 0.0
        
        # --- FIX C: GESTIÓN DE CIERRE TOTAL ---
        if current_amt == 0:
            print(f"✅ Posición {symbol} cerrada. Limpiando todo.")
            try: exchange.cancel_all_orders(symbol)
            except: pass
            state['active_symbol'] = None
            state['lowest_price'] = 0
            state['last_amt'] = 0.0
            save_state(state)
            return state

        # --- FIX C: SINCRONIZACIÓN (TP HIT) ---
        # Si la cantidad bajó (se ejecutó un TP), hay que ajustar el SL
        last_amt = state.get('last_amt', current_amt)
        
        if current_amt != last_amt:
            print(f"⚠️ Cantidad cambió ({last_amt} -> {current_amt}). Sincronizando SL...")
            try:
                # Cancelar SOLO órdenes STOP (SL)
                orders = exchange.fetch_open_orders(symbol)
                for o in orders:
                    if o['type'] == 'STOP_MARKET':
                        exchange.cancel_order(o['id'], symbol)
                
                # Recrear SL con nueva cantidad (al precio actual de trailing o inicial)
                # Buscamos dónde debería estar el SL
                ticker = exchange.fetch_ticker(symbol)
                curr_price = ticker['last']
                
                # Si no hay lowest_price válido, usar entrada
                ref_price = state.get('lowest_price', curr_price)
                if ref_price == 0: ref_price = curr_price
                
                # Calculamos SL teórico actual
                sl_price = ref_price * (1 + TRAILING_DIST_PCT)
                
                exchange.create_order(symbol, 'stop_market', 'sell', current_amt, None, {'stopPrice': sl_price, 'reduceOnly': True})
                print(f"✅ SL resincronizado a {sl_price} para {current_amt} contratos")
                
                state['last_amt'] = current_amt
                save_state(state)
            except Exception as e:
                print(f"❌ Error resincronizando SL: {e}")

        # --- SMART TRAILING LOGIC (Solo si baja más) ---
        ticker = exchange.fetch_ticker(symbol)
        curr_price = ticker['last']
        
        # Actualizar lowest price visto
        if state['lowest_price'] == 0 or curr_price < state['lowest_price']:
            state['lowest_price'] = curr_price
            
            # Solo mover SL si ya estamos en ganancia activable
            # (Calculo aprox vs entry) - Si no tenemos entry exacta guardada, usamos lógica relativa
            # Para simplificar V1, trailing activo desde que lowest < entry
            
            new_sl_price = state['lowest_price'] * (1 + TRAILING_DIST_PCT)
            
            # Chequear SL actual en exchange
            orders = exchange.fetch_open_orders(symbol)
            sl_order = next((o for o in orders if o['type'] == 'STOP_MARKET'), None)
            current_sl_price = float(sl_order['stopPrice']) if sl_order else 999999
            
            if new_sl_price < current_sl_price:
                print(f"🔄 Bajando Trailing SL a {new_sl_price}")
                if sl_order: exchange.cancel_order(sl_order['id'], symbol)
                exchange.create_order(symbol, 'stop_market', 'sell', current_amt, None, {'stopPrice': new_sl_price, 'reduceOnly': True})
                
                save_state(state) # Guardar nuevo lowest

    except Exception as e:
        print(f"Error gestionando: {e}")
    
    return state

def execute_entry(exchange, data, state):
    symbol = data['symbol']
    price = data['close']     # Precio actual (market)
    atr = data['atr']
    
    # --- FIX A: ENTRADA INTRABAR ---
    # Usamos el Low actual vs Low previo
    if data['current_low'] >= data['prev_low']:
        # Aún no rompe el soporte
        return state 
    
    msg = f"🌪️ **BREAKDOWN CONFIRMADO: {symbol}**\nLow: {data['current_low']} < Prev: {data['prev_low']}\nEntrando..."
    print(msg)
    send_telegram(msg)
    
    if DRY_RUN: return state

    try:
        balance = exchange.fetch_balance()['USDT']['free']
        
        # --- FIX B & D: SL ESTABLE & SANITY CHECK ---
        sl_price = price + (atr * SL_ATR_MULT)
        dist = sl_price - price
        
        # Sanity Check: Si el ATR es 0 o la distancia es absurda (ej < 0.2%)
        if dist <= 0 or (dist / price) < 0.002:
            print(f"⚠️ Distancia SL peligrosa ({dist}). Abortando.")
            return state

        risk_amt = balance * FIXED_RISK_PCT
        qty_usdt = risk_amt / dist * price
        
        if qty_usdt > balance * 0.8: qty_usdt = balance * 0.8
        
        qty = exchange.amount_to_precision(symbol, qty_usdt / price)
        
        # EJECUCIÓN
        exchange.set_leverage(5, symbol)
        
        # 1. Market Entry
        order = exchange.create_market_sell_order(symbol, qty)
        entry_price = float(order['average'])
        
        # 2. Stop Loss (Usando ATR estable)
        sl_real = entry_price + (atr * SL_ATR_MULT)
        sl_real = float(exchange.price_to_precision(symbol, sl_real))
        exchange.create_order(symbol, 'stop_market', 'buy', qty, None, {'stopPrice': sl_real, 'reduceOnly': True})
        
        # 3. Take Profits (Limit)
        qty_float = float(qty)
        
        # TP1
        qty_tp1 = exchange.amount_to_precision(symbol, qty_float * TP1_SIZE)
        price_tp1 = entry_price * (1 - TP1_PCT)
        price_tp1 = float(exchange.price_to_precision(symbol, price_tp1))
        exchange.create_order(symbol, 'limit', 'buy', qty_tp1, price_tp1, {'reduceOnly': True})
        
        # TP2
        qty_tp2 = exchange.amount_to_precision(symbol, qty_float * TP2_SIZE)
        price_tp2 = entry_price * (1 - TP2_PCT)
        price_tp2 = float(exchange.price_to_precision(symbol, price_tp2))
        exchange.create_order(symbol, 'limit', 'buy', qty_tp2, price_tp2, {'reduceOnly': True})
        
        # State Update
        state['active_symbol'] = symbol
        state['last_trade_ts'] = int(time.time())
        state['lowest_price'] = entry_price
        state['last_amt'] = qty_float # Guardamos cantidad inicial para detectar cambios
        save_state(state)
        
        send_telegram(f"✅ Short Abierto {symbol} @ {entry_price}\nSL: {sl_real}")
        
    except Exception as e:
        print(f"❌ Error Entry: {e}")
        send_telegram(f"❌ Error Entry: {e}")
        
    return state

# ======================================================
#  MAIN LOOP
# ======================================================

def main():
    print("🌪️ INICIANDO CRASH BOT V76 (THE SURGEON FINAL)...")
    exchange = get_exchange()
    state = load_state()
    
    while True:
        try:
            # 1. Gestión (Alta Frecuencia)
            if state['active_symbol']:
                state = manage_position(exchange, state)
                time.sleep(10) # Chequear gestión cada 10s es seguro
                continue 

            # 2. Escaneo (Baja Frecuencia - Cooldown Check)
            last_ts = state.get('last_trade_ts', 0)
            if (int(time.time()) - last_ts) < (COOLDOWN_HOURS * 3600):
                print(f"❄️ Cooldown. Durmiendo 5 min...")
                time.sleep(300)
                continue

            print(f"\n📡 Scan: {datetime.now().strftime('%H:%M')}")
            
            for symbol in SYMBOLS:
                data = analyze_market(exchange, symbol)
                if data and data['signal_ready']:
                    state = execute_entry(exchange, data, state)
                    if state['active_symbol']: break 
                time.sleep(1) # Rate limit suave
            
            time.sleep(60) # Scan cada minuto para capturar breakdown rápido
            
        except Exception as e:
            print(f"❌ Error Loop: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()