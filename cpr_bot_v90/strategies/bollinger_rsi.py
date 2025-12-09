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
    print("❌ TA-Lib no está instalado. Instálalo para usar V41.")

# ======================================================
#  🔥 CONFIG V41 – BLACK MAMBA
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia Core ----
BB_PERIOD = 20
BB_STD = 2.0
KC_MULT = 1.5
MOM_PERIOD = 20

SL_ATR_MULT = 1.5
TRAIL_ATR_MULT = 2.5
EXIT_HOURS = 96

# ---- Filtros ----
MAX_TRADES_MONTH = 15
BAD_HOURS = [3,4,5]
MIN_CLOSE_STRENGTH = 0.35

# ---- Risk ----
INITIAL_BALANCE = 10000
TARGET_VOL = 0.015
BASE_VAR = 0.02
COMMISSION = 0.0006
BASE_LATENCY = 0.0001
DD_LIMIT = 0.15
DD_FACTOR = 0.5
MAX_LEVER = 30

# ---- Microestructura ----
SLIPPAGE_K = 0.05
EXIT_SLIPP_MULT = 1.0
SPREAD_MIN_USD = 0.10
SPREAD_FACTOR = 0.002


# ======================================================
#  🧩 DATA LOADING
# ======================================================

def load_data(symbol):
    print(f"🔍 Cargando {symbol} ...")

    candidates = [
        f"mainnet_data_{TIMEFRAME_STR}_{symbol}.csv",
        f"{symbol}_{TIMEFRAME_STR}.csv"
    ]

    paths = ["data", ".", "cpr_bot_v90/data"]

    df = None
    for name in candidates:
        for p in paths:
            path = os.path.join(p, name)
            if os.path.exists(path):
                print(f"📁 Archivo encontrado: {path}")
                df = pd.read_csv(path)
                break
        if df is not None:
            break

    if df is None:
        print("❌ No se encontró archivo.")
        return None

    df.columns = [c.lower() for c in df.columns]

    if 'timestamp' not in df.columns:
        if 'open_time' in df.columns:
            df.rename(columns={'open_time': 'timestamp'}, inplace=True)
        else:
            print("❌ No existe timestamp.")
            return None

    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")

    df.sort_values("timestamp", inplace=True)
    df['time_diff'] = df['timestamp'].diff().dt.total_seconds()

    df['volume'] = df.get('volume', 1.0)

    df.reset_index(drop=True, inplace=True)
    return df


# ======================================================
#  📐 INDICADORES
# ======================================================

def calc_indicators(df):
    print("📐 Calculando indicadores...")

    if not HAS_TALIB:
        raise Exception("TA-Lib requerido.")

    df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)

    # Bollinger
    up, mid, low = talib.BBANDS(df['close'], timeperiod=BB_PERIOD,
                                nbdevup=BB_STD, nbdevdn=BB_STD)

    df['bb_u'] = up
    df['bb_l'] = low
    df['sma20'] = mid

    # Keltner
    df['kc_u'] = mid + df['atr'] * KC_MULT
    df['kc_l'] = mid - df['atr'] * KC_MULT

    # Squeeze ON
    df['squeeze'] = (df['bb_u'] < df['kc_u']) & (df['bb_l'] > df['kc_l'])

    # TTM Hist
    df['hl2'] = (df['high'] + df['low']) / 2
    mom = df['hl2'] - df['hl2'].shift(MOM_PERIOD)
    df['ttm_hist'] = mom.ewm(span=3).mean() - mom.ewm(span=20).mean()

    # Volumen relativo
    df['vol_ma'] = df['volume'].rolling(100).mean()
    df['vol_factor'] = df['volume'] / df['vol_ma']
    df['vol_factor'].fillna(1.0, inplace=True)

    # Gap Detection
    jump = abs(df['open'] - df['close'].shift(1))
    atr_thr = df['atr'].shift(1) * 4
    gap = (df['time_diff'] > 9000) | (jump > atr_thr)
    df['gap'] = gap

    df['atr_prev'] = df['atr'].shift(1)
    df['prev_low'] = df['low'].shift(1)

    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ======================================================
#  🚀 BACKTEST ENGINE – V41
# ======================================================

def run_backtest(symbol):

    df = load_data(symbol)
    if df is None:
        return

    df = calc_indicators(df)

    print(f"🚀 Iniciando Backtest V41 para {symbol}\n")

    balance = INITIAL_BALANCE
    peak = balance
    equity_curve = []

    # Estado
    position = None
    entry = 0
    quantity = 0
    sl = 0
    entry_comm = 0
    entry_time = None

    month = -1
    trades_month = 0
    cooldown = 0
    trigger = False

    trades = []

    for i in range(len(df)):
        row = df.iloc[i]

        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr = row.atr
        atr_prev = row.atr_prev

        # ---------------------------
        #  COSTOS
        # ---------------------------

        rel_vol = atr / c
        slippage_pct = SLIPPAGE_K * rel_vol
        spread_pct = max(SPREAD_MIN_USD, atr * SPREAD_FACTOR) / c
        latency = BASE_LATENCY + rel_vol * 0.1
        total_cost = slippage_pct + spread_pct + latency

        # ---------------------------
        #  GESTIÓN MENSUAL
        # ---------------------------

        if ts.month != month:
            month = ts.month
            trades_month = 0

        if row.gap:
            cooldown = 24
            trigger = False

        if cooldown > 0:
            cooldown -= 1

        # ============================================================
        # 1) EJECUCIÓN DE ENTRADA (si trigger estuvo activado)
        # ============================================================
        if trigger and position is None:
            if trades_month < MAX_TRADES_MONTH and ts.hour not in BAD_HOURS:
                entry_price = o * (1 + total_cost)

                sl_price = entry_price - atr_prev * SL_ATR_MULT
                risk_dist = entry_price - sl_price

                if risk_dist > 0:

                    vol_smooth = df.at[i, 'atr_prev'] / c
                    var_factor = min(1.0, TARGET_VOL / vol_smooth)

                    dd = (peak - balance) / peak
                    dd_adj = DD_FACTOR if dd > DD_LIMIT else 1.0

                    final_risk_pct = BASE_VAR * var_factor * dd_adj
                    risk_usd = balance * final_risk_pct

                    max_contracts = (balance * MAX_LEVER) / entry_price
                    qty = min(risk_usd / risk_dist, max_contracts)

                    entry_comm = qty * entry_price * COMMISSION
                    balance -= entry_comm

                    position = "long"
                    entry = entry_price
                    sl = sl_price
                    quantity = qty
                    entry_time = ts
                    trades_month += 1

                    # Intra-candle SL
                    if l <= sl:
                        exit_price = sl * (1 - slippage_pct)
                        pnl = (exit_price - entry) * qty
                        fee = exit_price * qty * COMMISSION
                        balance += pnl - fee

                        trades.append({
                            "year": ts.year,
                            "month": ts.month,
                            "pnl": pnl - entry_comm - fee,
                            "type": "SL Intra"
                        })

                        position = None
                        entry_comm = 0

            trigger = False

        # ============================================================
        # 2) GESTIÓN DE POSICIÓN ABIERTA
        # ============================================================
        if position == "long":

            # Trailing
            new_sl = min(h, c) - atr * TRAIL_ATR_MULT
            if new_sl > sl:
                sl = new_sl

            # SL hit
            exit_price = None
            reason = None

            if l <= sl:
                exit_raw = o if o < sl else sl
                exit_price = exit_raw * (1 - slippage_pct)
                reason = "SL Trail"

            # Time exit
            elif (ts - entry_time).total_seconds() >= EXIT_HOURS * 3600:
                exit_price = c * (1 - slippage_pct)
                reason = "Time"

            if exit_price:
                pnl = (exit_price - entry) * quantity
                exit_comm = exit_price * quantity * COMMISSION
                balance += pnl - exit_comm

                net = pnl - entry_comm - exit_comm

                trades.append({
                    "year": entry_time.year,
                    "month": entry_time.month,
                    "pnl": net,
                    "type": reason
                })

                entry_comm = 0
                position = None

        # ============================================================
        # 3) BÚSQUEDA DE ENTRADAS NUEVAS
        # ============================================================

        if position is None and cooldown == 0:

            if trades_month < MAX_TRADES_MONTH and ts.hour not in BAD_HOURS:

                squeeze_now = row.squeeze
                squeeze_prev = df.at[i-1, 'squeeze'] if i > 0 else False
                squeeze_ok = squeeze_now or squeeze_prev

                trig_level = row.bb_u
                breakout = (o < trig_level) and (h >= trig_level)

                hist = row.ttm_hist
                prev_hist = df.at[i-1, 'ttm_hist'] if i > 0 else 0
                bull_mom = hist > 0 and hist > prev_hist

                if squeeze_ok and breakout and bull_mom:

                    rng = h - l
                    if rng > 0:
                        close_strength = (c - l) / rng
                    else:
                        close_strength = 0

                    reject = close_strength < MIN_CLOSE_STRENGTH
                    fake_gap = o < row.prev_low

                    if not reject and not fake_gap:
                        trigger = True

        # ============================================================
        # 4) EQUITY MARK-TO-MARKET
        # ============================================================

        eq = balance
        if position == "long":
            eq += (c - entry) * quantity

        equity_curve.append(eq)
        peak = max(peak, eq)

    # ======================================================
    #  REPORT
    # ======================================================

    eq_series = pd.Series(equity_curve)
    
    # Prevenir error si equity_curve está vacía (raro, pero posible si data falla)
    if len(eq_series) > 0:
        peak_series = eq_series.cummax()
        dd = (eq_series - peak_series) / peak_series
        max_dd = dd.min() * 100
    else:
        max_dd = 0.0

    total_return = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    trades_df = pd.DataFrame(trades)

    print("\n" + "="*55)
    print(f"📊 RESULTADOS FINALES V41 – BLACK MAMBA: {symbol}")
    print("="*55)
    print(f"💰 Balance Final:   ${balance:.2f}")
    print(f"📈 Retorno Total:   {total_return:.2f}%")
    print(f"📉 Max DD:          {max_dd:.2f}%\n")

    if not trades_df.empty:
        # Corrección del typo .pn -> .pnl
        win = (trades_df['pnl'] > 0).mean() * 100
        print(f"🏆 Win Rate:        {win:.2f}%")
        print(f"🧮 Total Trades:    {len(trades_df)}\n")
        
        print("📅 RENDIMIENTO POR AÑO:")
        # Agrupación segura
        try:
            print(trades_df.groupby("year")["pnl"].agg(["sum","count"]))
        except Exception as e:
            print(f"Error en reporte anual: {e}")
            
        print("="*55)
        
        # Guardar CSV para análisis post-mortem
        trades_df.to_csv(f"logs_v41_{symbol}.csv", index=False)
        print(f"💾 Logs guardados en logs_v41_{symbol}.csv")
        
    else:
        print("⚠️ No hubo trades. Revisa filtros o datos.")

# ======================================================
# RUN
# ======================================================

if __name__ == "__main__":
    run_backtest(SYMBOL)