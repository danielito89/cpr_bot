#!/usr/bin/env python3
# backtester_v5.py
# Versión: v5.8 (Reporte con Periodo Evaluado + Filtro de Fechas)

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

# --- 1. CONFIGURACIÓN ---
SYMBOL_TO_TEST = "ETHUSDT"
START_BALANCE = 1000

# --- FILTRO DE FECHAS (Opcional) ---
# Formato: "YYYY-MM-DD" o None para usar todo el historial
TEST_START_DATE = None  # Ej: "2024-01-01"
TEST_END_DATE = None    # Ej: "2024-06-30"
# -----------------------------------

# Riesgo
LEVERAGE = 30
INVESTMENT_PCT = 0.05
COMMISSION_PCT = 0.0004
DAILY_LOSS_LIMIT_PCT = 15.0
MAX_TRADE_SIZE_USDT = 50000
MAX_DAILY_TRADES = 50

# Estrategia
EMA_PERIOD = 20
ATR_PERIOD = 14
VOLUME_FACTOR = 1.1
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 12

# Filtros
MIN_VOLATILITY_ATR_PCT = 0.5
TRAILING_STOP_TRIGGER_ATR = 1.25
TRAILING_STOP_DISTANCE_ATR = 1.0

# Multiplicadores
RANGING_SL_MULT = 0.5 
BREAKOUT_SL_MULT = 1.0 
RANGING_TP_MULT = 0.8  
BREAKOUT_TP_MULT = 1.25 

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# --- IMPORTAR LÓGICA REAL ---
try:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"Error importando bot_core: {e}")
    sys.exit(1)

# --- MOCKS ---
class MockTelegram:
    async def _send_message(self, text): pass 

class MockOrdersManager:
    def __init__(self, simulator):
        self.sim = simulator
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        self.sim.open_position(side, qty, price, sl, tps, type)
    async def move_sl_to_be(self, qty):
        self.sim.move_sl_to_be()
    async def update_sl(self, new_price, qty, reason="Trailing"): 
        self.sim.update_sl(new_price)
    async def close_position_manual(self, reason):
        self.sim.close_position(reason)

class MockBotController:
    """Provee tiempo y estado al RiskManager."""
    def __init__(self, simulator, symbol):
        self.symbol = symbol
        self.client = None 
        self.telegram_handler = MockTelegram()
        self.orders_manager = MockOrdersManager(simulator)
        self.state = simulator.state 
        self.simulator = simulator
        self.lock = asyncio.Lock()
        
        # Inyectar Configuración
        self.investment_pct = INVESTMENT_PCT
        self.leverage = LEVERAGE
        self.cpr_width_threshold = CPR_WIDTH_THRESHOLD
        self.volume_factor = VOLUME_FACTOR
        self.take_profit_levels = 3
        self.breakout_atr_sl_multiplier = 1.0
        self.breakout_tp_mult = 1.25
        self.ranging_atr_multiplier = 0.5
        self.range_tp_mult = 2.0 
        self.daily_loss_limit_pct = DAILY_LOSS_LIMIT_PCT
        
        self.min_volatility_atr_pct = MIN_VOLATILITY_ATR_PCT
        self.trailing_stop_trigger_atr = TRAILING_STOP_TRIGGER_ATR
        self.trailing_stop_distance_atr = TRAILING_STOP_DISTANCE_ATR
        
        self.MAX_TRADE_SIZE_USDT = MAX_TRADE_SIZE_USDT
        self.MAX_DAILY_TRADES = MAX_DAILY_TRADES

        # Configuración de Precisión según el Símbolo
        if "PEPE" in symbol:
            self.tick_size = 0.00000001
            self.step_size = 1.0
        elif "SHIB" in symbol or "BONK" in symbol:
            self.tick_size = 0.000001
            self.step_size = 1.0
        elif "XRP" in symbol or "DOGE" in symbol:
            self.tick_size = 0.00001
            self.step_size = 1.0
        else:
            # Default para BTC, ETH, BNB, SOL
            self.tick_size = 0.01
            self.step_size = 0.001

    async def _get_account_balance(self): return self.state.balance
    
    def get_current_timestamp(self):
        """Magia: Devuelve el tiempo simulado al RiskManager."""
        return self.simulator.current_timestamp

    async def _get_current_position(self):
        if not self.state.is_in_position: return None
        info = self.state.current_position_info
        amt = info.get('quantity', 0) if info.get('side') == SIDE_BUY else -info.get('quantity', 0)
        return {
            "positionAmt": amt, "entryPrice": info.get('entry_price'),
            "markPrice": self.state.current_price, "unRealizedProfit": 0.0 
        }

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
        self.current_time = None
        self.trades_history = []
        self.rejected_trades_log = []

    def save_state(self): pass

class BacktesterV5:
    def __init__(self):
        self.state = SimulatorState()
        self.current_timestamp = 0.0
        self.controller = MockBotController(self, SYMBOL_TO_TEST)
        self.risk_manager = RiskManager(self.controller)
        self.start_date_actual = None
        self.end_date_actual = None

    def open_position(self, side, qty, price, sl, tps, type_):
        notional = qty * price
        
        # --- Check Max Size (para reporte) ---
        is_capped = False
        if notional > MAX_TRADE_SIZE_USDT:
            is_capped = True
            qty = MAX_TRADE_SIZE_USDT / price
            notional = MAX_TRADE_SIZE_USDT

        comm = notional * COMMISSION_PCT
        self.state.balance -= comm
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": qty, "entry_price": price,
            "entry_type": type_, "tps_hit_count": 0,
            "total_pnl": -comm, "sl": sl, "tps": tps, 
            "entry_time": self.current_timestamp, # Guardamos como float
            "comm_entry": comm, "trailing_sl_price": None,
            "is_capped": is_capped
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
        
        # Smart Cooldown Simulado
        cooldown = 300
        if net_pnl > 0: cooldown = 0
        elif net_pnl < 0: cooldown = 900
        self.state.trade_cooldown_until = self.current_timestamp + cooldown

        self.state.trades_history.append({
            'entry_time': info.get('entry_time'), 'exit_time': self.state.current_time,
            'side': info['side'], 'type': info['entry_type'],
            'pnl': net_pnl, 'reason': reason,
            'is_capped': info.get('is_capped', False)
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
        print(f"Iniciando Backtest V5.8 (Reporte Completo) para {SYMBOL_TO_TEST}...")
        
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
        
        print("Calculando Indicadores 1H...")
        df_1h['EMA_1h'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        tr = pd.concat([df_1h['High']-df_1h['Low'], abs(df_1h['High']-df_1h['Close'].shift(1)), abs(df_1h['Low']-df_1h['Close'].shift(1))], axis=1).max(axis=1)
        df_1h['ATR_1h'] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

        # Shift Lookahead
        df_1h = df_1h.shift(1) 

        print("Fusionando...")
        df_merged = pd.merge_asof(df_1m, df_1h[['EMA_1h', 'ATR_1h']], left_index=True, right_index=True, direction='backward')
        df_merged.dropna(inplace=True)
        
        # --- FILTRO DE FECHAS ---
        if TEST_START_DATE:
            df_merged = df_merged.loc[TEST_START_DATE:]
        if TEST_END_DATE:
            df_merged = df_merged.loc[:TEST_END_DATE]
            
        if df_merged.empty:
            print("No hay datos en el rango seleccionado.")
            return
            
        # Guardar fechas reales para el reporte
        self.start_date_actual = df_merged.index[0]
        self.end_date_actual = df_merged.index[-1]
        
        del df_1h, df_1m
        gc.collect()

        print(f"Simulando {len(df_merged)} velas ({self.start_date_actual} - {self.end_date_actual})...")
        current_date_obj = None

        for row in df_merged.itertuples():
            self.current_timestamp = row.Index.timestamp()
            self.state.current_time = row.Index
            self.state.current_price = row.Close
            
            row_date = row.Index.date()
            if current_date_obj != row_date:
                current_date_obj = row_date
                self.state.daily_trade_stats = []
                self.state.daily_start_balance = self.state.balance
                
                yesterday_ts = pd.Timestamp(row_date - timedelta(days=1))
                if yesterday_ts in df_1d.index:
                    d_row = df_1d.loc[yesterday_ts]
                    h, l, c = float(d_row['High']), float(d_row['Low']), float(d_row['Close'])
                    self.state.daily_pivots = calculate_pivots_from_data(h, l, c,self.controller.tick_size,CPR_WIDTH_THRESHOLD)
            
            self.state.cached_atr = row.ATR_1h
            self.state.cached_ema = row.EMA_1h
            self.state.cached_median_vol = row.MedianVol
            
            if self.state.is_in_position:
                await self.risk_manager._check_trailing_stop(row.Close, self.state.current_position_info.get('quantity', 0))
                
                # Time Stop con FIX de tipo
                if self.state.current_position_info['entry_type'].startswith("Ranging"):
                     elapsed = self.current_timestamp - self.state.current_position_info['entry_time']
                     if (elapsed / 3600) > TIME_STOP_HOURS: 
                         self.close_position(f"Time-Stop ({TIME_STOP_HOURS}h)")
                
                self.check_exits(row)

            if not self.state.is_in_position and self.state.daily_pivots:
                k = {'o': row.Open, 'c': row.Close, 'h': row.High, 'l': row.Low, 'v': row.Volume, 'q': row.Quote_Asset_Volume, 'x': True}
                await self.risk_manager.seek_new_trade(k)

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

        capped_count = df['is_capped'].sum() if 'is_capped' in df.columns else 0
        days_tested = (self.end_date_actual - self.start_date_actual).days

        print("\n" + "="*50)
        print(f" RESULTADOS V5.8 FINAL: {SYMBOL_TO_TEST}")
        print("="*50)
        print(f" --- Periodo ---")
        print(f" Inicio:          {self.start_date_actual}")
        print(f" Fin:             {self.end_date_actual}")
        print(f" Duración:        {days_tested} días")
        print("-" * 50)
        print(f" --- Configuración ---")
        print(f" Saldo Inicial:   ${START_BALANCE}")
        print(f" Vol Factor:      {VOLUME_FACTOR}")
        print(f" Riesgo:          {INVESTMENT_PCT*100}% Capital x {LEVERAGE} Leverage")
        print(f" Max Trade:       ${MAX_TRADE_SIZE_USDT}")
        print("-" * 50)
        print(f" --- Rendimiento ---")
        print(f" PnL Neto:        ${total_pnl:.2f}")
        print(f" Balance Final:   ${self.state.balance:.2f}")
        print(f" Profit Factor:   {pf:.2f}")
        print(f" Win Rate:        {win_rate:.2f}% ({wins}/{len(df)})")
        print("-" * 50)
        print(f" --- Actividad ---")
        print(f" Total Trades:    {len(df)}")
        print(f" Promedio Diario: {len(df)/days_tested:.1f} trades/día")
        print(f" Trades con Techo: {capped_count}")
        print("="*50)
        df.to_csv(os.path.join(DATA_DIR, f"backtest_v5_{SYMBOL_TO_TEST}.csv"))

if __name__ == "__main__":
    asyncio.run(BacktesterV5().run())