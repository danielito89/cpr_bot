#!/usr/bin/env python3
import pandas as pd
import numpy as np
import os
import talib

# ======================================================
#  🔥 CONFIG V50 – CLEAN SLATE (GOLDEN CROSS 4H)
# ======================================================

SYMBOL = "ETHUSDT"
TIMEFRAME_STR = "1h"

# ---- Estrategia: TENDENCIA PURA EN 4H ----
# No operamos ruido de 1H. Operamos estructura de 4H.
FAST_EMA = 50
SLOW_EMA = 200

# ---- Salidas ----
# No hay TP. Dejamos correr hasta que la tendencia se invierta (Cruce a la baja).
# SL de emergencia por si el cruce fue falso.
SL_ATR_MULT = 3.0       

# ---- Risk & Microestructura ----
INITIAL_BALANCE = 10000
FIXED_RISK_PCT = 0.05   # 5% por trade (Pocos trades = Mayor convicción)
MAX_LEVER = 5           # Apalancamiento suave (Swing Trading)

COMMISSION = 0.0004         
SPREAD_PCT = 0.0004         
SLIPPAGE_PCT = 0.0006       
BASE_LATENCY = 0.0001
MIN_QTY = 0.01

# ======================================================
#  1. CARGA Y RESAMPLING (EL SECRETO)
# ======================================================

def load_and_resample(symbol):
    print(f"🔍 Cargando datos 1H para {symbol}...")
    # Rutas estándar
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

    if df is None: return None, None

    # Limpieza
    df.columns = [c.lower() for c in df.columns]
    col_map = {'open_time': 'timestamp', 'date': 'timestamp'}
    df.rename(columns=col_map, inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None: df['timestamp'] = df['timestamp'].dt.tz_localize("UTC")
    else: df['timestamp'] = df['timestamp'].dt.tz_convert("UTC")
    
    df.sort_values("timestamp", inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- MAGIC TRICK: RESAMPLING A 4H ---
    # Convertimos el ruido de 1H en velas sólidas de 4H
    print("🔄 Convirtiendo datos: 1H -> 4H para eliminar ruido...")
    
    ohlc_dict = {
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }
    
    df_4h = df.resample('4h').apply(ohlc_dict).dropna()
    
    # Calculamos indicadores sobre datos de 4H (Mucho más fiables)
    df_4h['ema_fast'] = talib.EMA(df_4h['close'], timeperiod=FAST_EMA)
    df_4h['ema_slow'] = talib.EMA(df_4h['close'], timeperiod=SLOW_EMA)
    df_4h['atr'] = talib.ATR(df_4h['high'], df_4h['low'], df_4h['close'], timeperiod=14)
    
    # Señal de Cruce (Golden Cross)
    # 1 = Alcista (Fast > Slow), -1 = Bajista
    df_4h['trend'] = np.where(df_4h['ema_fast'] > df_4h['ema_slow'], 1, -1)
    
    # Detectar el momento exacto del cruce (Shift 1 para no mirar futuro)
    # Si ayer era -1 y hoy es 1 -> COMPRA
    df_4h['prev_trend'] = df_4h['trend'].shift(1)
    df_4h['signal'] = np.where((df_4h['trend'] == 1) & (df_4h['prev_trend'] == -1), 1, 0)
    
    # Salida: Cruce de la muerte (Fast < Slow)
    df_4h['exit_signal'] = np.where((df_4h['trend'] == -1) & (df_4h['prev_trend'] == 1), 1, 0)

    # Volvemos a mapear esto a las velas de 1H para simular la ejecución precisa
    # (Usamos forward fill para que la señal de 4H persista en las velas de 1H correspondientes)
    print("🔄 Mapeando señales 4H de vuelta a 1H para ejecución precisa...")
    df_1h = df.join(df_4h[['ema_fast', 'ema_slow', 'atr', 'trend', 'signal', 'exit_signal']], rsuffix='_4h')
    df_1h.fillna(method='ffill', inplace=True)
    df_1h.dropna(inplace=True)
    df_1h.reset_index(inplace=True)
    
    return df_1h

# ======================================================
#  🚀 BACKTEST ENGINE (SIMPLIFICADO & ROBUSTO)
# ======================================================

def run_backtest(symbol):
    df = load_and_resample(symbol)
    if df is None: return

    print(f"🚀 Iniciando Backtest V50 (Golden Cross 4H) para {symbol}\n")

    balance = INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance # Para cálculo de Drawdown

    position = None 
    entry_price = 0; quantity = 0; sl = 0
    entry_comm = 0
    
    trades = []

    for i in range(len(df)):
        row = df.iloc[i]
        
        # Datos (Vienen de 1H, pero los indicadores son de 4H)
        ts = row.timestamp
        o, h, l, c = row.open, row.high, row.low, row.close
        atr_4h = row.atr
        
        # Señales (Calculadas en 4H)
        signal_buy = row.signal == 1
        signal_sell = row.exit_signal == 1 # Cruce bajista
        
        # Costos
        friction = SLIPPAGE_PCT + SPREAD_PCT + BASE_LATENCY

        # ----------------------------------------------------
        # 1. ENTRADA (Solo si hay Golden Cross confirmado en 4H)
        # ----------------------------------------------------
        # Nota: Al usar ffill, la señal '1' se repite 4 veces. 
        # position is None evita entrar multiples veces.
        if position is None and signal_buy:
            
            entry_price = o * (1 + friction)
            
            # SL Amplio (Basado en ATR de 4H)
            sl_price = entry_price - (atr_4h * SL_ATR_MULT)
            risk_dist = entry_price - sl_price
            
            if risk_dist > 0:
                # Sizing Conservador
                risk_usd = balance * FIXED_RISK_PCT
                qty = risk_usd / risk_dist
                
                # Cap de Leverage
                max_qty = (balance * MAX_LEVER) / entry_price
                qty = min(qty, max_qty)
                
                if qty >= MIN_QTY:
                    entry_comm = qty * entry_price * COMMISSION
                    balance -= entry_comm
                    
                    position = "long"
                    quantity = qty
                    sl = sl_price
                    entry = entry_price # Guardar para calculo PnL
                    
                    # Check Intra-candle crash
                    if l <= sl:
                        exit_p = sl * (1 - SLIPPAGE_PCT)
                        pnl = (exit_p - entry_price) * qty
                        fee = exit_p * qty * COMMISSION
                        balance += (pnl - fee)
                        trades.append({'year': ts.year, 'pnl': pnl - entry_comm - fee})
                        position = None

        # ----------------------------------------------------
        # 2. GESTIÓN (HOLD hasta Death Cross)
        # ----------------------------------------------------
        elif position == "long":
            exit_p = None
            reason = None
            
            # A) Stop Loss de Emergencia
            if l <= sl:
                exit_p = sl * (1 - SLIPPAGE_PCT)
                reason = "Stop Loss"
            
            # B) Salida Técnica: Cruce de la Muerte (EMA 50 < 200 en 4H)
            elif signal_sell:
                exit_p = o * (1 - SLIPPAGE_PCT) # Salimos al Open de la vela que confirma el cruce
                reason = "Death Cross"
            
            if exit_p:
                pnl = (exit_p - entry) * quantity
                exit_comm = exit_p * quantity * COMMISSION
                balance += (pnl - exit_comm)
                
                net_pnl = pnl - entry_comm - exit_comm # entry_comm aprox
                trades.append({'year': ts.year, 'pnl': net_pnl})
                position = None
        
        equity_curve.append(balance)

    # REPORTING
    trades_df = pd.DataFrame(trades)
    total_ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    
    print("\n" + "="*55)
    print(f"📊 RESULTADOS V50 – CLEAN SLATE (4H TREND): {symbol}")
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
        print("⚠️ No hubo trades (Mercado muy lateral o falta de datos).")

if __name__ == "__main__":
    run_backtest(SYMBOL)