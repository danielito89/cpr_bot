#!/usr/bin/env python3
# backtester_v4.py
# Versión: v4 (Simulación Institucional)
# - Implementa Techo de Posición (Max Trade Size)
# - Respeta estrictamente el Límite de Pérdida Diaria
# - Simula Slippage por tamaño

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
import time
import gc
from datetime import datetime, timedelta

# Configurar logger
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger()

# --- 1. CONFIGURACIÓN ---
SYMBOL_TO_TEST = "ETHUSDT"
START_BALANCE = 10000

# Riesgo Avanzado
LEVERAGE = 30
INVESTMENT_PCT = 0.05
COMMISSION_PCT = 0.0004
DAILY_LOSS_LIMIT_PCT = 15.0

# --- NUEVO: REGLAS DE REALISMO ---
MAX_TRADE_SIZE_USDT = 50000  # Nunca abrir una posición mayor a 50k USDT (incluso si hay saldo)
SLIPPAGE_PCT = 0.0002        # 0.02% de deslizamiento estimado en cada orden

# Estrategia (Ganadora)
EMA_PERIOD = 20
ATR_PERIOD = 14
VOLUME_FACTOR = 1.3
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 12

# Nuevos Filtros
MIN_VOLATILITY_ATR_PCT = 0.5
TRAILING_STOP_TRIGGER_ATR = 1.5
TRAILING_STOP_DISTANCE_ATR = 1.0

# Multiplicadores
RANGING_SL_MULT = 0.5 
BREAKOUT_SL_MULT = 1.0 
RANGING_TP_MULT = 0.8  
BREAKOUT_TP_MULT = 1.25 

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# --- IMPORTAR LÓGICA REAL ---
try:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"Error importando módulos del bot: {e}")
    sys.exit(1)

# --- MOCKS ---
class MockTelegram:
    async def _send_message(self, text): pass 

class MockOrdersManager:
    def __init__(self, simulator): self.sim = simulator
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        self.sim.open_position(side, qty, price, sl, tps, type)
    async def move_sl_to_be(self, qty): self.sim.move_sl_to_be()
    async def update_sl(self, new_price, qty, reason="Trailing"): self.sim.update_sl(new_price)
    async def close_position_manual(self, reason): self.sim.close_position(reason)

class MockBotController:
    def __init__(self, simulator, symbol):
        self.symbol = symbol
        self.client = None 
        self.telegram_handler = MockTelegram()
        self.orders_manager = MockOrdersManager(simulator)
        self.state = simulator.state 
        self.lock = asyncio.Lock()
        
        # Configuración
        self.investment_pct = INVESTMENT_PCT
        self.leverage = LEVERAGE
        self.cpr_width_threshold = CPR_WIDTH_THRESHOLD
        self.volume_factor = VOLUME_FACTOR
        self.take_profit_levels = 3
        self.breakout_atr_sl_multiplier = BREAKOUT_SL_MULT
        self.breakout_tp_mult = BREAKOUT_TP_MULT
        self.ranging_atr_multiplier = RANGING_SL_MULT
        self.range_tp_mult = RANGING_TP_MULT
        self.daily_loss_limit_pct = DAILY_LOSS_LIMIT_PCT
        
        self.min_volatility_atr_pct = MIN_VOLATILITY_ATR_PCT
        self.trailing_stop_trigger_atr = TRAILING_STOP_TRIGGER_ATR
        self.trailing_stop_distance_atr = TRAILING_STOP_DISTANCE_ATR
        
        self.tick_size = 0.01
        self.step_size = 0.001

    async def _get_account_balance(self): return self.state.balance
    async def _get_current_position(self):
        if not self.state.is_in_position: return None
        info = self.state.current_position_info
        amt = info['quantity'] if info['side'] == SIDE_BUY else -info['quantity']
        return { "positionAmt": amt, "entryPrice": info['entry_price'], "markPrice": self.state.current_price, "unRealizedProfit": 0.0 }

# --- LÓGICA DE SEÑAL (Híbrida) ---
def get_trade_signal(row, atr, ema, pivots):
    current_price = row.Close
    current_volume = row.Quote_Asset_Volume
    median_vol = row.MedianVol
    is_green_candle = current_price > row.Open
    is_red_candle = current_price < row.Open
    
    if not all([atr > 0, ema > 0, median_vol > 0]): return None, None, 0.0, 0.0
    required_volume = median_vol * VOLUME_FACTOR
    volume_confirmed = current_volume > required_volume
    side, entry_type, sl_price, tp_price = None, None, 0.0, 0.0

    # 1. Breakouts
    if current_price > pivots['H4']:
        if volume_confirmed and current_price > ema and is_green_candle:
            side, entry_type = "BUY", "Breakout Long"
            sl_price = current_price - atr * BREAKOUT_SL_MULT
            tp_price = current_price + atr * BREAKOUT_TP_MULT
    elif current_price < pivots['L4']:
        if volume_confirmed and current_price < ema and is_red_candle:
            side, entry_type = "SELL", "Breakout Short"
            sl_price = current_price + atr * BREAKOUT_SL_MULT
            tp_price = current_price - atr * BREAKOUT_TP_MULT

    # 2. Rango
    if not side:
        if current_price <= pivots['L3']:
            if volume_confirmed and is_green_candle:
                side, entry_type = "BUY", "Ranging Long"
                sl_price = pivots['L4'] - atr * RANGING_SL_MULT
                tp_price = current_price + (atr * RANGING_TP_MULT)
        elif current_price >= pivots['H3']:
            if volume_confirmed and is_red_candle:
                side, entry_type = "SELL", "Ranging Short"
                sl_price = pivots['H4'] + atr * RANGING_SL_MULT
                tp_price = current_price - (atr * RANGING_TP_MULT)

    if side: return side, entry_type, sl_price, tp_price
    return None, None, 0.0, 0.0

# --- SIMULADOR ---
class SimulatorState:
    def __init__(self):
        self.trading_paused = False
        self.trade_cooldown_until = 0
        self.daily_pivots = {}
        self.last_pivots_date = None
        self.cached_atr = None
        self.cached_ema = None
        self.cached_median_vol = None
        self.is_in_position = False
        self.current_position_info = {}
        self.last_known_position_qty = 0.0
        self.sl_moved_to_be = False
        self.daily_trade_stats = []
        self.balance = START_BALANCE
        self.current_price = 0.0
        self.current_time = None
        self.trades_history = []

    def save_state(self): pass

class BacktesterV4:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, SYMBOL_TO_TEST)
        self.risk_manager = RiskManager(self.controller)

    def open_position(self, side, qty, price, sl, tps, type_):
        # --- REALISMO: Limitar tamaño máximo de posición ---
        notional_value = qty * price
        if notional_value > MAX_TRADE_SIZE_USDT:
            # Reducir cantidad para respetar el límite
            original_qty = qty
            qty = MAX_TRADE_SIZE_USDT / price
            # logging.info(f"⚠️ Posición limitada por MaxSize: {original_qty:.3f} -> {qty:.3f}")
            notional_value = qty * price # Recalcular

        # --- REALISMO: Slippage de entrada ---
        # Asumimos que entramos un poco peor que el precio teórico
        slippage = price * SLIPPAGE_PCT
        real_entry_price = price + slippage if side == "BUY" else price - slippage
        
        comm = notional_value * COMMISSION_PCT
        self.state.balance -= comm
        self.state.is_in_position = True
        
        self.state.current_position_info = {
            "side": side, "quantity": qty, "entry_price": real_entry_price,
            "entry_type": type_, "tps_hit_count": 0,
            "total_pnl": -comm, "sl": sl, "tps": tps, 
            "entry_time": self.state.current_time,
            "comm_entry": comm, "trailing_sl_price": None
        }
        self.state.last_known_position_qty = qty
        self.state.sl_moved_to_be = False

    def move_sl_to_be(self):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def update_sl(self, new_price):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = new_price
            self.state.sl_moved_to_be = True

    def close_position(self, reason):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        if not info: return

        exit_price = self.state.current_price
        
        # --- REALISMO: Slippage de salida ---
        slippage = exit_price * SLIPPAGE_PCT
        real_exit_price = exit_price - slippage if info['side'] == "BUY" else exit_price + slippage

        pnl_gross = (real_exit_price - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        
        comm_exit = (real_exit_price * info['quantity']) * COMMISSION_PCT
        comm_entry = info.get('comm_entry', 0.0)
        net_pnl = pnl_gross - comm_exit - comm_entry
        
        self.state.balance += (pnl_gross - comm_exit)
        
        self.state.trades_history.append({
            'entry_time': info['entry_time'], 'exit_time': self.state.current_time,
            'side': info['side'], 'type': info['entry_type'],
            'pnl': net_pnl, 'reason': reason,
            'max_size_limit': info['quantity'] * info['entry_price'] >= MAX_TRADE_SIZE_USDT
        })
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.daily_trade_stats.append({'pnl': net_pnl})

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        if not info: return
        high, low = row.High, row.Low
        
        # SL
        current_sl = info['sl']
        sl_hit = (info['side'] == SIDE_BUY and low <= current_sl) or \
                 (info['side'] == SIDE_SELL and high >= current_sl)
        if sl_hit:
            self.state.current_price = current_sl
            self.close_position("Stop-Loss")
            return

        # TPs
        tps = info['tps']
        if len(tps) >= 2:
            tp2 = tps[1]
            hit_tp2 = (info['side'] == SIDE_BUY and high >= tp2) or (info['side'] == SIDE_SELL and low <= tp2)
            if hit_tp2 and info['tps_hit_count'] < 2:
                info['tps_hit_count'] = 2
                self.move_sl_to_be()

        if tps:
            last_tp = tps[-1]
            hit_tp = (info['side'] == SIDE_BUY and high >= last_tp) or (info['side'] == SIDE_SELL and low <= last_tp)
            if hit_tp:
                self.state.current_price = last_tp
                self.close_position("Take-Profit Final")

    async def run(self):
        print(f"Iniciando Backtest V4 (Institucional) para {SYMBOL_TO_TEST}...")
        print(f"Reglas: MaxSize={MAX_TRADE_SIZE_USDT} USDT, Slippage={SLIPPAGE_PCT*100}%")
        
        file_1h = f"mainnet_data_1h_{SYMBOL_TO_TEST}.csv"
        file_1d = f"mainnet_data_1d_{SYMBOL_TO_TEST}.csv"
        file_1m = f"mainnet_data_1m_{SYMBOL_TO_TEST}.csv"
        
        print("Cargando datos...")
        try:
            df_1h = pd.read_csv(os.path.join(DATA_DIR, file_1h), index_col="Open_Time", parse_dates=True)
            df_1d = pd.read_csv(os.path.join(DATA_DIR, file_1d), index_col="Open_Time", parse_dates=True)
            df_1m = pd.read_csv(os.path.join(DATA_DIR, file_1m), index_col="Open_Time", parse_dates=True, 
                                usecols=['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume'])
        except FileNotFoundError:
            print("Faltan archivos de datos.")
            return

        print("Calculando Mediana...")
        df_1m['MedianVol'] = df_1m['Quote_Asset_Volume'].rolling(window=60).median().shift(1)
        
        print("Fusionando...")
        df_1h['EMA_1h'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        tr = pd.concat([df_1h['High']-df_1h['Low'], abs(df_1h['High']-df_1h['Close'].shift(1)), abs(df_1h['Low']-df_1h['Close'].shift(1))], axis=1).max(axis=1)
        df_1h['ATR_1h'] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

        df_merged = pd.merge_asof(df_1m, df_1h[['EMA_1h', 'ATR_1h']], left_index=True, right_index=True, direction='backward')
        df_merged.dropna(inplace=True)
        del df_1h, df_1m
        gc.collect()

        print(f"Simulando {len(df_merged)} velas...")
        
        for row in df_merged.itertuples():
            self.state.current_time = row.Index
            self.state.current_price = row.Close
            
            current_date = row.Index.date()
            if self.state.last_pivots_date != current_date:
                # --- RESET DIARIO REALISTA ---
                self.state.daily_trade_stats = [] # Reset PnL del día
                # -----------------------------
                yesterday_ts = pd.Timestamp(current_date - timedelta(days=1))
                if yesterday_ts in df_1d.index:
                    d_row = df_1d.loc[yesterday_ts]
                    h, l, c = float(d_row['High']), float(d_row['Low']), float(d_row['Close'])
                    self.state.daily_pivots = calculate_pivots_from_data(h, l, c, 0.01, 0.2)
                    self.state.last_pivots_date = current_date
            
            self.state.cached_atr = row.ATR_1h
            self.state.cached_ema = row.EMA_1h
            self.state.cached_median_vol = row.MedianVol
            
            if self.state.is_in_position:
                await self.risk_manager._check_trailing_stop(row.Close, self.state.current_position_info.get('quantity', 0))
                if self.state.current_position_info['entry_type'].startswith("Ranging"):
                     hours = (row.Index - self.state.current_position_info['entry_time']).total_seconds() / 3600
                     if hours > TIME_STOP_HOURS: self.close_position(f"Time-Stop ({TIME_STOP_HOURS}h)")
                self.check_exits(row)

            if not self.state.is_in_position and self.state.daily_pivots:
                atr = row.ATR_1h
                ema = row.EMA_1h
                pivots = self.state.daily_pivots
                side, type_, sl, tp = get_trade_signal(row, atr, ema, pivots)
                
                if side:
                    # Cálculo de tamaño considerando el techo
                    raw_size = (self.state.balance * INVESTMENT_PCT * LEVERAGE) / row.Close
                    # La lógica de MAX_SIZE se aplica dentro de open_position
                    
                    if raw_size == 0: continue
                    comm = (raw_size * row.Close) * COMMISSION_PCT
                    
                    # Verificar límite diario ANTES de abrir
                    # Simulamos que llamamos a _daily_loss_exceeded del RiskManager
                    daily_pnl_sum = sum(t['pnl'] for t in self.state.daily_trade_stats)
                    loss_limit = -abs(DAILY_LOSS_LIMIT_PCT / 100.0 * self.state.balance)
                    if daily_pnl_sum <= loss_limit:
                        continue # Stop trading for today

                    self.state.balance -= comm
                    in_position = True
                    pos = {
                        'entry': row.Close, 'side': side, 'size': raw_size, # Se ajustará en open_position
                        'tp': [tp], 'sl': sl, 'type': type_, 
                        'time': row.Index, 'comm_entry': comm, 'tps': [tp], 'tps_hit_count': 0, 'trailing_sl_price': None
                    }
                    # Llamada al mock que llama al simulador
                    self.open_position(side, raw_size, row.Close, sl, [tp], type_)

        self.print_results()

    def print_results(self):
        trades = self.state.trades_history
        if not trades:
            print("\n--- NO SE REALIZARON TRADES ---")
            return

        df = pd.DataFrame(trades)
        total_pnl = df['pnl'].sum()
        wins = len(df[df['pnl'] > 0])
        win_rate = (wins / len(df)) * 100
        
        gross_profit = df[df['pnl'] > 0]['pnl'].sum()
        gross_loss = abs(df[df['pnl'] < 0]['pnl'].sum())
        pf = gross_profit / gross_loss if gross_loss != 0 else 0

        print("\n" + "="*40)
        print(f" RESULTADOS INSTITUCIONALES (v4): {SYMBOL_TO_TEST}")
        print("="*40)
        print(f" PnL Neto:      ${total_pnl:.2f}")
        print(f" Balance Final: ${self.state.balance:.2f}")
        print(f" Profit Factor: {pf:.2f}")
        print(f" Win Rate:      {win_rate:.2f}% ({wins}/{len(df)})")
        print(f" Total Trades:  {len(df)}")
        
        capped_trades = len(df[df['max_size_limit'] == True])
        print(f" Trades con Techo: {capped_trades} (Límite ${MAX_TRADE_SIZE_USDT})")
        print("="*40)
        df.to_csv(os.path.join(DATA_DIR, f"backtest_v4_{SYMBOL_TO_TEST}.csv"))

if __name__ == "__main__":
    asyncio.run(BacktesterV4().run())
