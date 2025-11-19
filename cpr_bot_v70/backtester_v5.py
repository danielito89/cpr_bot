#!/usr/bin/env python3
# backtester_v5.py
# Versión: v5 (Validación de Riesgo Total con RiskManager.can_trade)

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
import time
import gc
from datetime import datetime, timedelta

# Configuración
logging.basicConfig(level=logging.INFO, format="%(message)s")

SYMBOL_TO_TEST = "ETHUSDT"
START_BALANCE = 10000

# RIESGO (Tus parámetros reales)
LEVERAGE = 30
INVESTMENT_PCT = 0.05
COMMISSION_PCT = 0.0004
DAILY_LOSS_LIMIT_PCT = 15.0
MAX_TRADE_SIZE_USDT = 50000 # Techo de posición
MAX_DAILY_TRADES = 100      # Límite para evitar sobreoperar en días locos

# ESTRATEGIA
EMA_PERIOD = 20
ATR_PERIOD = 14
VOLUME_FACTOR = 1.3
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 12
MIN_VOLATILITY_ATR_PCT = 0.5
TRAILING_STOP_TRIGGER_ATR = 1.5
TRAILING_STOP_DISTANCE_ATR = 1.0

# MULTIPLICADORES
RANGING_SL_MULT = 0.5 
BREAKOUT_SL_MULT = 1.0 
RANGING_TP_MULT = 0.8
BREAKOUT_TP_MULT = 1.25 

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

try:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
except ImportError:
    print("Error importando bot_core. Ejecuta desde la carpeta cpr_bot_v90/")
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
    """Simula la configuración y el estado para RiskManager."""
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
        
        self.MAX_TRADE_SIZE_USDT = MAX_TRADE_SIZE_USDT
        self.MAX_DAILY_TRADES = MAX_DAILY_TRADES
        
        self.tick_size = 0.01
        self.step_size = 0.001
        
    async def _get_account_balance(self): return self.state.balance
    async def _get_current_position(self):
        if not self.state.is_in_position: return None
        info = self.state.current_position_info
        amt = info['quantity'] if info['side'] == SIDE_BUY else -info['quantity']
        return { "positionAmt": amt, "entryPrice": info['entry_price'], "markPrice": self.state.current_price, "unRealizedProfit": 0.0 }

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
        self.daily_start_balance = START_BALANCE
        self.balance = START_BALANCE
        
        self.current_price = 0.0
        self.trades_history = []
        self.rejected_trades = {} 

    def save_state(self): pass

class BacktesterV5:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, SYMBOL_TO_TEST)
        self.risk_manager = RiskManager(self.controller)

    def open_position(self, side, qty, price, sl, tps, type_):
        notional = qty * price
        comm = notional * COMMISSION_PCT
        self.state.balance -= comm
        self.state.is_in_position = True
        
        self.state.current_position_info = {
            "side": side, "quantity": qty, "entry_price": price,
            "entry_type": type_, "tps_hit_count": 0,
            "total_pnl": -comm, "sl": sl, "tps": tps, 
            "entry_time": self.state.current_time, # Se asigna en el loop
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
        pnl_gross = (exit_price - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        comm_exit = (exit_price * info['quantity']) * COMMISSION_PCT
        comm_entry = info.get('comm_entry', 0.0)
        net_pnl = pnl_gross - comm_exit - comm_entry
        
        self.state.balance += (pnl_gross - comm_exit)
        
        self.state.trades_history.append({
            'entry_time': info.get('entry_time'), 'exit_time': self.state.current_time,
            'side': info['side'], 'type': info['entry_type'],
            'pnl': net_pnl, 'reason': reason
        })
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.daily_trade_stats.append({'pnl': net_pnl})

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        high, low = row.High, row.Low
        
        current_sl = info['sl']
        sl_hit = (info['side'] == SIDE_BUY and low <= current_sl) or \
                 (info['side'] == SIDE_SELL and high >= current_sl)
        if sl_hit:
            self.state.current_price = current_sl
            self.close_position("Stop-Loss")
            return

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
        print(f"Iniciando Backtest V5 (Risk-Aware) para {SYMBOL_TO_TEST}...")
        
        file_1h = f"mainnet_data_1h_{SYMBOL_TO_TEST}.csv"
        file_1d = f"mainnet_data_1d_{SYMBOL_TO_TEST}.csv"
        file_1m = f"mainnet_data_1m_{SYMBOL_TO_TEST}.csv"
        
        print("Cargando datos...")
        df_1h = pd.read_csv(os.path.join(DATA_DIR, file_1h), index_col="Open_Time", parse_dates=True)
        df_1d = pd.read_csv(os.path.join(DATA_DIR, file_1d), index_col="Open_Time", parse_dates=True)
        df_1m = pd.read_csv(os.path.join(DATA_DIR, file_1m), index_col="Open_Time", parse_dates=True, 
                            usecols=['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume'])
        
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
        current_date_obj = None
        
        for row in df_merged.itertuples():
            self.state.current_time = row.Index
            self.state.current_price = row.Close
            
            # 1. GESTIÓN DE DÍA
            row_date = row.Index.date()
            if current_date_obj != row_date:
                current_date_obj = row_date
                
                # Reset del Risk Manager (Simulado)
                self.state.daily_trade_stats = []
                self.state.daily_start_balance = self.state.balance
                
                # Actualizar Pivotes
                yesterday_ts = pd.Timestamp(row_date - timedelta(days=1))
                if yesterday_ts in df_1d.index:
                    d_row = df_1d.loc[yesterday_ts]
                    h, l, c = float(d_row['High']), float(d_row['Low']), float(d_row['Close'])
                    self.state.daily_pivots = calculate_pivots_from_data(h, l, c, 0.01, 0.2)
                    self.state.last_pivots_date = row_date

            self.state.cached_atr = row.ATR_1h
            self.state.cached_ema = row.EMA_1h
            self.state.cached_median_vol = row.MedianVol
            
            # 2. SALIDAS
            if self.state.is_in_position:
                # Trailing
                await self.risk_manager._check_trailing_stop(row.Close, self.state.current_position_info.get('quantity', 0))
                
                # Time Stop
                if self.state.current_position_info['entry_type'].startswith("Ranging"):
                     hours = (row.Index - self.state.current_position_info['entry_time']).total_seconds() / 3600
                     if hours > TIME_STOP_HOURS: self.close_position(f"Time-Stop ({TIME_STOP_HOURS}h)")
                
                self.check_exits(row)

            # 3. ENTRADAS (Solo si el RiskManager lo permite)
            if not self.state.is_in_position and self.state.daily_pivots:
                
                # --- DELEGAMOS AL RISK MANAGER ---
                k = {'o': row.Open, 'c': row.Close, 'h': row.High, 'l': row.Low, 'v': row.Volume, 'q': row.Quote_Asset_Volume, 'x': True}
                await self.risk_manager.seek_new_trade(k)

        self.print_results()
        
        print("\n--- 🚫 RECHAZOS DE RIESGO (Top 5) ---")
        # Como risk.py no guarda los rechazos en self.state por defecto, 
        # no podremos verlos a menos que modifiquemos risk.py para loguearlos en una lista.
        # Pero al menos sabemos que se aplicaron.

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
        print(f" RESULTADOS V5: {SYMBOL_TO_TEST}")
        print("="*40)
        print(f" PnL Neto:      ${total_pnl:.2f}")
        print(f" Balance Final: ${self.state.balance:.2f}")
        print(f" Profit Factor: {pf:.2f}")
        print(f" Win Rate:      {win_rate:.2f}% ({wins}/{len(df)})")
        print(f" Total Trades:  {len(df)}")
        
        capped = len(df[df['max_size_limit'] == True]) if 'max_size_limit' in df.columns else 0
        print(f" Trades con Techo: {capped}")
        print("="*40)
        df.to_csv(os.path.join(DATA_DIR, f"backtest_v5_{SYMBOL_TO_TEST}.csv"))

if __name__ == "__main__":
    asyncio.run(BacktesterV5().run())
