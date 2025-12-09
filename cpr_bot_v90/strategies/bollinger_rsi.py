#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
from datetime import timedelta

try:
    import talib
    HAS_TALIB = True
except:
    HAS_TALIB = False
    print("❌ TA-Lib no está instalado. Instálalo para usar V44.")

# ======================================================
#  🔥 CONFIG V44 – SMART SNIPER (PATCHED)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia Core ----
SAR_ACCEL = 0.01       
SAR_MAX = 0.1          
EMA_TREND_PERIOD = 200 
ATR_PERIOD = 50        

# ---- Filtros ----
MIN_VOLATILITY_RATIO = 0.7  
ENTRY_BUFFER = 1.0003       

# ---- Salidas ----
SL_ATR_MULT = 1.2      
TP_ATR_MULT = 1.8      
EXIT_HOURS = 48        

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
TARGET_VOL = 0.015
BASE_VAR = 0.02
COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001

# FIX: Cantidad Mínima y Precisión (Binance standard para ETH)
MIN_QTY = 0.01
QTY_PRECISION = 3 

DD_LIMIT = 0.15
DD_FACTOR = 0.5
MAX_LEVER = 20              

MAX_TRADES_MONTH = 15     
BAD_HOURS = [3,4,5]

# ======================================================
#  🧩 DATA LOADING
# ======================================================

def load_data(symbol):
    print(f"🔍 Cargando {symbol} ...")
    candidates = [f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv", f"{symbol}_{TIMEFRAME_STR}.csv"]
    paths = ["data", ".", "cpr_bot_v90/data"]
    
    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                print(f"📁 Archivo encontrado: {path}")
                df = pd.read_csv(path)
                break
        if df is not None: break

    if df is None:
        print("❌ No se encontró archivo.")
        return None

    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert("UTC") 

    df.sort_values("timestamp", inplace=True)
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
    if 'volume' not in df.columns: df['volume'] = 1.0

    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  📐 INDICADORES
# ======================================================

def calc_indicators(df):
    print("📐 Calculando indicadores V44...")

    if not HAS_TALIB: raise Exception("TA-Lib requerido.")

    # 1. ATR Lento
    df['atr_raw'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=ATR_PERIOD)
    df['atr'] = df['atr_raw'].ewm(span=20).mean() 
    df['atr_prev'] = df['atr'].shift(1)

    # 2. SAR Lento
    df['sar'] = talib.SAR(df['high'], df['low'], acceleration=SAR_ACCEL, maximum=SAR_MAX)
    df['sar_prev'] = df['sar'].shift(1)

    # 3. EMA Tendencia
    df['ema_trend'] = talib.EMA(df['close'], timeperiod=EMA_TREND_PERIOD)

    # 4. Volatility Ratio
    df['range'] = df['high'] - df['low']
    df['avg_range'] = df['range'].rolling(window=20).mean()
    # FIX: Guard contra división por cero
    df['avg_range'] = df['avg_range'].replace(0, np.nan) 
    df['vr'] = df['range'] / df['avg_range']

    # Gap Detection
    jump = abs(df['open'] - df['close'].shift(1))
    atr_thr = df['atr'].shift(1) * 3
    gap = (df['time_diff'] > 9000) | (jump > atr_thr)
    df['gap'] = gap
    
    df['prev_close'] = df['close'].shift(1)

    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ======================================================
#  🚀 BACKTEST ENGINE – V44
# ======================================================

def run_backtest(symbol):
    df = load_data(symbol)
    if df is None: return
    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V44 (Patched) para {symbol}\n")

    balance = INITIAL_BALANCE
    peak = balance
    equity_curve = [balance]

    # Estado
    position = None
    entry = 0
    quantity = 0
    sl = 0
    tp = 0
    entry_time = None
    
    # FIX: Variable persistente para comisión de entrada
    position_comm_paid = 0.0 
    
    month = -1; trades_month = 0; cooldown = 0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        
        trade_active_this_candle = False

        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr = row.atr
        atr_prev = row.atr_prev
        sar = row.sar
        sar_prev = row.sar_prev
        
        # Costos Fijos
        total_friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # Gestión Mensual & Cooldown
        if ts.month != month:
            month = ts.month
            trades_month = 0

        if row.gap: cooldown = 24
        if cooldown > 0: cooldown -= 1

        # ============================================================
        # 1) BÚSQUEDA DE ENTRADA
        # ============================================================
        if position is None and cooldown == 0:
            if trades_month < MAX_TRADES_MONTH and ts.hour not in BAD_HOURS:
                
                # Filtros
                trend_ok = c > row.ema_trend
                vol_ok = row.vr > MIN_VOLATILITY_RATIO
                
                if trend_ok and vol_ok:
                    
                    sar_was_bearish = sar_prev > row.prev_close 
                    price_breaks_sar = h > sar_prev
                    
                    if sar_was_bearish and price_breaks_sar:
                        
                        # Trigger
                        trigger_price = sar_prev * ENTRY_BUFFER
                        entry_price = max(o, trigger_price)
                        entry_price = entry_price * (1 + total_friction)
                        
                        # SL / TP
                        sl_dist = atr_prev * SL_ATR_MULT
                        tp_dist = atr_prev * TP_ATR_MULT
                        sl_price = entry_price - sl_dist
                        tp_price = entry_price + tp_dist
                        
                        risk_dist = entry_price - sl_price

                        if risk_dist > 0:
                            # FIX: Safety Math para división por precio
                            safe_c = c if c > 0 else 1e-6
                            vol_smooth = atr_prev / safe_c
                            
                            var_factor = min(1.0, TARGET_VOL / max(vol_smooth, 1e-6))
                            dd = (peak - balance) / peak
                            dd_adj = DD_FACTOR if dd > DD_LIMIT else 1.0
                            
                            final_risk_pct = BASE_VAR * var_factor * dd_adj
                            risk_usd = peak * final_risk_pct

                            max_contracts = (balance * MAX_LEVER) / entry_price
                            qty = min(risk_usd / risk_dist, max_contracts)
                            
                            # FIX: Cantidad mínima y redondeo
                            if qty < MIN_QTY:
                                qty = 0 # No trade si no alcanza el mínimo
                            else:
                                qty = round(qty, QTY_PRECISION)

                            if qty > 0:
                                entry_comm = qty * entry_price * COMMISSION
                                balance -= entry_comm

                                position = "long"
                                entry = entry_price
                                sl = sl_price
                                tp = tp_price
                                quantity = qty
                                entry_time = ts
                                
                                # FIX: Persistir la comisión
                                position_comm_paid = entry_comm
                                
                                trades_month += 1
                                trade_active_this_candle = True

                                # INTRA-CANDLE CHECK
                                if l <= sl:
                                    exit_price = sl * (1 - SLIPPAGE_PCT)
                                    pnl = (exit_price - entry) * qty
                                    fee = exit_price * qty * COMMISSION
                                    
                                    balance += (pnl - fee)
                                    
                                    # FIX: Usar variable persistida
                                    net_pnl = pnl - position_comm_paid - fee
                                    
                                    trades.append({
                                        "year": ts.year, "month": ts.month,
                                        "pnl": net_pnl, "type": "SL Intra"
                                    })
                                    position = None
                                    position_comm_paid = 0.0 # Reset
                                
                                elif h >= tp: 
                                    exit_price = tp * (1 - SLIPPAGE_PCT)
                                    pnl = (exit_price - entry) * qty
                                    fee = exit_price * qty * COMMISSION
                                    
                                    balance += (pnl - fee)
                                    if balance > peak: peak = balance
                                    
                                    net_pnl = pnl - position_comm_paid - fee
                                    
                                    trades.append({
                                        "year": ts.year, "month": ts.month,
                                        "pnl": net_pnl, "type": "TP Intra"
                                    })
                                    position = None
                                    position_comm_paid = 0.0 # Reset

        # ============================================================
        # 2) GESTIÓN DE POSICIÓN ABIERTA
        # ============================================================
        if position == "long" and not trade_active_this_candle:
            
            exit_price = None
            reason = None

            # SL Check
            if l <= sl:
                exit_raw = o if o < sl else sl 
                exit_price = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "SL Trail"

            # TP Check (Faltaba en V43 fuera de intra-candle)
            elif h >= tp:
                exit_raw = o if o > tp else tp # Gap up favor
                exit_price = exit_raw * (1 - SLIPPAGE_PCT)
                reason = "TP Target"

            # Time Exit
            elif (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_price = c * (1 - SLIPPAGE_PCT)
                reason = "Time"

            if exit_price:
                pnl = (exit_price - entry) * quantity
                exit_comm = exit_price * quantity * COMMISSION
                
                balance += (pnl - exit_comm)
                if balance > peak: peak = balance
                
                # FIX: Usar variable persistida
                net = pnl - position_comm_paid - exit_comm

                trades.append({
                    "year": entry_time.year, "month": entry_time.month,
                    "pnl": net, "type": reason
                })
                
                position = None
                position_comm_paid = 0.0 # Reset
                trade_active_this_candle = True

        # ============================================================
        # 3) EQUITY UPDATE
        # ============================================================
        if not trade_active_this_candle:
            curr_eq = balance
            if position == "long":
                unrealized = (c - entry) * quantity
                curr_eq += unrealized
            equity_curve.append(curr_eq)
        else:
            equity_curve.append(balance)

    # REPORT
    eq_series = pd.Series(equity_curve)
    if len(eq_series) > 0:
        dd = (eq_series - eq_series.cummax()) / eq_series.cummax()
        max_dd = dd.min() * 100
    else: max_dd = 0

    total_return = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    trades_df = pd.DataFrame(trades)

    print("\n" + "="*55)
    print(f"📊 RESULTADOS FINALES V44 – SMART SNIPER (PATCHED): {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_return:.2f}%")
    print(f"📉 Max DD:          {max_dd:.2f}%\n")

    if not trades_df.empty:
        win = (trades_df.pnl > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        try:
            print("📅 RENDIMIENTO POR AÑO:")
            print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        except: pass
        print("="*55)
    else:
        print("⚠️ No hubo trades.")

if __name__ == "__main__":
    run_backtest(SYMBOL)