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
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# ======================================================
#  ⚙️ CONFIGURACIÓN DE PRODUCCIÓN (V66 GOLDEN CROSS)
# ======================================================

# Credenciales (Desde .env)
API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parámetros de Estrategia
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "DOGE/USDT:USDT", "1000PEPE/USDT:USDT", "ADA/USDT:USDT"]
TIMEFRAME = "1h"       # Bajamos velas de 1H
RESAMPLE_TF = "4h"     # Operamos estructura de 4H
FAST_EMA = 50
SLOW_EMA = 200

# Gestión de Riesgo
LEVERAGE = 5
RISK_PER_TRADE = 0.05  # 5% del balance por operación (Sin compounding agresivo por seguridad inicial)
SL_ATR_MULT = 3.0      # Stop Loss de Catástrofe

# Modo Dry-Run (True = Dinero ficticio, False = Dinero Real)
DRY_RUN = False         # ¡CAMBIA A False SOLO CUANDO ESTÉS SEGURO!

# ======================================================
#  UTILIDADES
# ======================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"Error Telegram: {e}")

def get_exchange():
    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })
    # Cargar mercados para tener precisión de decimales
    exchange.load_markets()
    return exchange

# ======================================================
#  LÓGICA CORE (CEREBRO V66)
# ======================================================

def analyze_symbol(exchange, symbol):
    print(f"🔍 Analizando {symbol}...")
    
    # 1. Bajar datos (Necesitamos suficiente para EMA 200 en 4H -> 800 velas 1H mínimas)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=1000)
    except Exception as e:
        print(f"Error descargando {symbol}: {e}")
        return None

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)

    # 2. Resampling a 4H (La clave de la estrategia)
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample(RESAMPLE_TF).agg(ohlc_dict).dropna()

    # 3. Calcular Indicadores 4H
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)

    # 4. Obtener última vela CERRADA (posición -2)
    # La posición -1 es la vela actual formándose (no fiable)
    last_candle = df_4h.iloc[-2]
    prev_candle = df_4h.iloc[-3]
    
    # Precios actuales (del ticker en tiempo real, no del histórico)
    ticker = exchange.fetch_ticker(symbol)
    current_price = ticker['last']

    # 5. Señales
    # Tendencia Actual
    trend_bullish = last_candle['ema_fast'] > last_candle['ema_slow']
    
    # Cruce Dorado (Golden Cross): Fast cruza arriba de Slow
    golden_cross = (last_candle['ema_fast'] > last_candle['ema_slow']) and \
                   (prev_candle['ema_fast'] <= prev_candle['ema_slow'])
    
    # Cruce de la Muerte (Death Cross): Fast cruza abajo de Slow
    death_cross = (last_candle['ema_fast'] < last_candle['ema_slow']) and \
                  (prev_candle['ema_fast'] >= prev_candle['ema_slow'])

    return {
        "symbol": symbol,
        "price": current_price,
        "trend": "BULLISH" if trend_bullish else "BEARISH",
        "signal_buy": golden_cross,
        "signal_sell": death_cross,
        "atr": last_candle['atr']
    }

# ======================================================
#  EJECUCIÓN DE ÓRDENES
# ======================================================

def execute_logic(exchange, data):
    symbol = data['symbol']
    price = data['price']
    
    # Chequear posición actual
    positions = exchange.fetch_positions([symbol])
    pos = [p for p in positions if p['symbol'] == symbol][0]
    pos_amt = float(pos['contracts']) if pos['contracts'] else 0.0
    pos_side = pos['side'] # 'long' o 'short' (aunque en hedge mode es distinto, simplificamos)
    
    # En One-Way Mode, side suele ser implicito por el signo de contracts, pero ccxt lo normaliza.
    # Asumimos One-Way Mode para simplicidad V1.
    
    print(f"   Posición actual: {pos_amt} contratos ({pos_side})")

    # --- LÓGICA DE ENTRADA (LONG) ---
    if data['signal_buy'] and pos_amt == 0:
        msg = f"🚀 **GOLDEN CROSS DETECTADO: {symbol}**\nPrecio: {price}\nIniciando Long..."
        print(msg)
        send_telegram(msg)
        
        if not DRY_RUN:
            # Calcular tamaño
            balance = exchange.fetch_balance()['USDT']['free']
            risk_amt = balance * RISK_PER_TRADE
            
            # SL Distancia
            sl_dist = data['atr'] * SL_ATR_MULT
            sl_price = price - sl_dist
            
            # Tamaño posición: Risk / Distancia al SL
            # (Ajustado por leverage si es necesario, pero priorizamos riesgo fijo)
            qty_usdt = (risk_amt / sl_dist) * price
            
            # Cap de apalancamiento
            max_pos = balance * LEVERAGE
            qty_usdt = min(qty_usdt, max_pos)
            
            # Convertir a contratos
            qty_contracts = qty_usdt / price
            
            # Ajustar precisión
            market = exchange.market(symbol)
            qty_contracts = exchange.amount_to_precision(symbol, qty_contracts)
            
            try:
                # 1. Poner Leverage
                exchange.set_leverage(LEVERAGE, symbol)
                
                # 2. Orden de Mercado
                order = exchange.create_market_buy_order(symbol, qty_contracts)
                entry_price = float(order['average']) if order['average'] else price
                
                # 3. Poner Stop Loss
                sl_price = entry_price - sl_dist
                exchange.create_order(symbol, 'stop_market', 'sell', qty_contracts, None, {'stopPrice': sl_price, 'reduceOnly': True})
                
                send_telegram(f"✅ Orden Ejecutada: Long {symbol} @ {entry_price}\nSL: {sl_price}")
                
            except Exception as e:
                send_telegram(f"❌ Error ejecutando orden: {e}")

    # --- LÓGICA DE SALIDA (DEATH CROSS) ---
    elif data['signal_sell'] and pos_amt > 0:
        msg = f"💀 **DEATH CROSS DETECTADO: {symbol}**\nCerrando posición Long..."
        print(msg)
        send_telegram(msg)
        
        if not DRY_RUN:
            try:
                # Cerrar todo
                exchange.create_market_sell_order(symbol, pos_amt, {'reduceOnly': True})
                # Cancelar órdenes abiertas (SL pendiente)
                exchange.cancel_all_orders(symbol)
                send_telegram(f"✅ Posición cerrada exitosamente.")
            except Exception as e:
                send_telegram(f"❌ Error cerrando posición: {e}")

    else:
        print(f"   💤 Nada que hacer. Tendencia: {data['trend']}")

# ======================================================
#  BUCLE PRINCIPAL
# ======================================================

def main():
    print("🤖 INICIANDO CPR_BOT V1 (GOLDEN CROSS PRODUCTION)...")
    send_telegram("🤖 **Bot Iniciado**\nEstrategia: V66 Golden Cross 4H\nModo: " + ("SIMULACIÓN" if DRY_RUN else "DINERO REAL"))
    
    exchange = get_exchange()
    
    while True:
        try:
            print(f"\n🕒 Revisión: {datetime.now().strftime('%H:%M')}")
            
            for symbol in SYMBOLS:
                data = analyze_symbol(exchange, symbol)
                if data:
                    execute_logic(exchange, data)
                time.sleep(1) # Pequeña pausa entre monedas
            
            print("😴 Durmiendo 1 hora...")
            
            # Sincronización exacta con la próxima hora
            # (Para operar justo al cierre de la vela de 1H)
            now = datetime.now()
            sleep_sec = 3600 - (now.minute * 60 + now.second) + 5 # 5 seg extra de buffer
            time.sleep(sleep_sec)
            
        except KeyboardInterrupt:
            print("\n🛑 Bot detenido por usuario.")
            break
        except Exception as e:
            print(f"❌ Error en bucle principal: {e}")
            send_telegram(f"⚠️ Error del Bot: {e}")
            time.sleep(60) # Reintentar en 1 min si hay error

if __name__ == "__main__":
    main()