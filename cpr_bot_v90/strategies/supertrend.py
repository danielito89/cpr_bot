#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V51 – TREND SURFER (4H PULLBACKS)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: SWING EN 4H ----
# Tendencia Macro
FAST_EMA = 50
SLOW_EMA = 200
# Gatillo de Pullback (EMA más rápida para detectar el dip)
TRIGGER_EMA = 20        

# ---- Salidas ----
# Stop Loss Inicial (Debajo del swing)
SL_ATR_MULT = 2.0       
# Trailing Stop: Si cierra bajo la EMA 50 (Soporte dinámico)
TRAILING_EMA_EXIT = True

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
# Arriesgamos un poco menos por trade porque haremos más operaciones
FIXED_RISK_PCT = 0.03   
MAX_LEVER = 10          

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. CARGA Y RESAMPLING (4H)
# ======================================================

def load_and_resample(symbol):
    print(f"🔍 Cargando datos 1H para {symbol}...")
    candidates = [f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", f"{symbol}_{TIMEFRAME_STR}.csv"]
    paths = ["data", ".", "cpr_bot_v90/data"]
    
    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                df = pd.read_csv(path)
                break
        if df is not None: break

    if df is None: return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- RESAMPLING A 4H ---
    print("🔄 Generando estructura de 4H...")
    ohlc_dict = {'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    # INDICADORES EN 4H
    # Tendencia
    df_4h['ema_50'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_200'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    # Pullback zone
    df_4h['ema_20'] = talib.EMA(df_4h['close'], timeperiod=TRIGGER_EMA)
    
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # LÓGICA DE SEÑAL (PULLBACK)
    # 1. Tendencia Alcista: EMA 50 > EMA 200
    # 2. Pullback: El precio (Low) tocó la EMA 20
    # 3. Confirmación: La vela cerró verde (Close > Open) demostrando rechazo
    
    trend_up = df_4h['ema_50'] > df_4h['ema_200']
    dip_touch = df_4h['low'] <= df_4h['ema_20']
    green_candle = df_4h['close'] > df_4h['open']
    
    # Señal de Compra
    df_4h['signal_buy'] = np.where(trend_up & dip_touch & green_candle, 1, 0)
    
    # Señal de Salida por Tendencia (Cierre bajo EMA 50)
    df_4h['trend_broken'] = df_4h['close'] < df_4h['ema_50']

    # Mapeo a 1H
    print("🔄 Sincronizando señales con timeframe operativo (1H)...")
    df_1h = df.join(df_4h[['ema_50', 'ema_200', 'atr', 'signal_buy', 'trend_broken']], rsuffix='_4h')
    df_1h.fillna(method='ffill', inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE V51
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V51 (4H Trend Surfer) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance

    position = None 
    entry_price = 0; quantity = 0; sl = 0
    entry_comm = 0
    
    trades = []
    
    # Control para no entrar en la misma señal 4 veces (ya que 1 vela 4H son 4 de 1H)
    last_signal_time = None 

    for i in range(len(df)):
        row = df.iloc[i]
        
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        
        # Datos de 4H proyectados
        atr_4h = row.atr
        signal_buy = row.signal_buy == 1
        trend_broken = row.trend_broken
        ema_50_4h = row.ema_50
        
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ----------------------------------------------------
        # 1. ENTRADA (BUY THE DIP)
        # ----------------------------------------------------
        # Verificamos si es una señal nueva (ha pasado al menos 4 horas desde la anterior usada)
        # O simplemente si no tenemos posición.
        
        if position is None and signal_buy:
            
            # Evitar re-entrar en la misma vela de 4H repetidamente
            # Usamos una lógica simple: Si signal_buy está activa, entramos.
            # Como signal_buy viene de una vela 4H CERRADA, es una señal estable por 4 horas.
            # Tomamos la primera oportunidad.
            
            entry_price = o * (1 + friction)
            
            # SL Técnico: Debajo de la EMA 50 (Soporte Estructural) o ATR
            # Usamos EMA 50 - un margen, porque si rompe la 50, la tesis de "surf" se rompe
            technical_sl = ema_50_4h - (atr_4h * 0.5) 
            
            # SL de Volatilidad (Respaldo)
            atr_sl = entry_price - (atr_4h * SL_ATR_MULT)
            
            # Usamos el más lejano para dar espacio? No, el más lógico.
            # Si estamos surfeando sobre la 20/50, romper la 50 es malo.
            sl_price = min(technical_sl, atr_sl)
            
            risk_dist = entry_price - sl_price
            
            if risk_dist > 0:
                risk_usd = balance * FIXED_RISK_PCT
                qty = risk_usd / risk_dist
                max_qty = (balance * MAX_LEVER) / entry_price
                qty = min(qty, max_qty)
                
                if qty >= MIN_QTY:
                    entry_comm = qty * entry_price * COMMISSION
                    balance -= entry_comm
                    
                    position = "long"
                    quantity = qty
                    sl = sl_price
                    entry = entry_price
                    
                    # Intra-candle
                    if l <= sl:
                        exit_p = sl * (1 - SLIPPAGE_PCT)
                        pnl = (exit_p - entry_price) * qty
                        fee = exit_p * qty * COMMISSION
                        balance += (pnl - fee)
                        trades.append({'year': ts.year, 'pnl': pnl - entry_comm - fee, 'type': 'SL Intra'})
                        position = None

        # ----------------------------------------------------
        # 2. GESTIÓN
        # ----------------------------------------------------
        elif position == "long":
            exit_p = None
            reason = None
            
            # A) Salida Técnica: Cierre de 4H bajo la EMA 50
            # trend_broken es True si Close_4H < EMA_50_4H
            if trend_broken:
                exit_p = o * (1 - SLIPPAGE_PCT)
                reason = "Trend Break (EMA 50)"
            
            # B) Stop Loss Hard
            elif l <= sl:
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "Stop Loss"
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                exit_comm = exit_p * quantity * COMMISSION
                balance += (pnl - exit_comm)
                
                net_pnl = pnl - entry_comm - exit_comm
                trades.append({'year': ts.year, 'pnl': net_pnl, 'type': reason})
                position = None
        
        equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V51 – TREND SURFER (4H): {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_ret:.2f}%")
    
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        print(f"📉 Max DD:          {dd.min()*100:.2f}%")

    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}")
        print("\n📅 Rendimiento Anual:")
        print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)