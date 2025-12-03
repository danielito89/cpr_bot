#!/usr/bin/env python3
# backtester_v7.py
# Versión: v7.0 (Auditada y Blindada)
# Correcciones C1, C2, C3, M1, M2, M3 implementadas.

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
START_BALANCE = 10000

# Parámetros de Realismo
COMMISSION_PCT = 0.0004
BASE_SLIPPAGE = 0.0002
IMPACT_COEF = 0.0005
MAX_IMPACT = 0.01

# Estrategia
VOLUME_FACTOR = 1.2
STRICT_VOLUME_FACTOR = 1.5 # Prueba con 1.5 primero, luego 3 o 5

TEST_START_DATE = "2022-01-01"
TEST_END_DATE = "2025-12-01"

# Riesgo
LEVERAGE = 30
INVESTMENT_PCT = 0.05
DAILY_LOSS_LIMIT_PCT = 15.0
MAX_TRADE_SIZE_USDT = 50000
MAX_DAILY_TRADES = 50

# Técnica
EMA_PERIOD = 20
ATR_PERIOD = 14
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 12

# Filtros
MIN_VOLATILITY_ATR_PCT = 0.5
TRAILING_STOP_TRIGGER_ATR = 1.25
TRAILING_STOP_DISTANCE_ATR = 1.0

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

try:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"Error importando bot_core: {e}")
    sys.exit(1)

class MockTelegram:
    async def _send_message(self, text): pass 

class MockOrdersManager:
    def __init__(self, simulator): self.sim = simulator
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        self.sim.stage_order(side, qty, price, sl, tps, type)
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
        self.simulator = simulator
        self.lock = asyncio.Lock()
        
        self.investment_pct = INVESTMENT_PCT
        self.leverage = LEVERAGE
        self.cpr_width_threshold = CPR_WIDTH_THRESHOLD
        self.volume_factor = VOLUME_FACTOR
        self.strict_volume_factor = STRICT_VOLUME_FACTOR
        
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
        
        if "PEPE" in symbol or "SHIB" in symbol:
            self.tick_size = 0.00000001; self.step_size = 1.0
        else:
            self.tick_size = 0.01; self.step_size = 0.001

    async def _get_account_balance(self): return self.state.balance
    def get_current_timestamp(self): return self.simulator.current_timestamp
    async def _get_current_position(self):
        if not self.state.is_in_position: return None
        return { "positionAmt": self.state.current_position_info.get('quantity', 0), "entryPrice": 0, "markPrice": 0, "unRealizedProfit": 0 }

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
        self.pending_order = None

    def save_state(self): pass

class BacktesterV7:
    def __init__(self):
        self.state = SimulatorState()
        self.current_timestamp = 0.0
        self.controller = MockBotController(self, SYMBOL_TO_TEST)
        self.risk_manager = RiskManager(self.controller)
        self.start_date_actual = None
        self.end_date_actual = None

    def calculate_slippage_pct(self, notional, median_vol):
        if median_vol <= 0: return MAX_IMPACT
        vol_ratio = (notional / median_vol)
        impact = IMPACT_COEF * (vol_ratio ** 0.8) # Exponente suavizado
        total_slippage = BASE_SLIPPAGE + min(MAX_IMPACT, impact)
        return total_slippage

    def stage_order(self, side, qty, price, sl, tps, type_):
        self.state.pending_order = {
            "side": side, "qty": qty, "sl": sl, "tps": tps, "type": type_,
            "signal_price": price, "created_at": self.current_timestamp
        }

    def execute_pending_order(self, open_price, median_vol):
        # C3: Check Cooldown en Ejecución
        if self.current_timestamp < self.state.trade_cooldown_until:
            self.state.pending_order = None # Se descarta la orden
            return

        order = self.state.pending_order
        self.state.pending_order = None
        
        notional = order['qty'] * open_price
        slippage_pct = self.calculate_slippage_pct(notional, median_vol)
        
        # M3: Random Jitter + Slippage
        random_jitter = np.random.uniform(-0.0001, 0.0001)
        
        if order['side'] == SIDE_BUY:
            real_entry = open_price * (1 + slippage_pct + random_jitter)
        else:
            real_entry = open_price * (1 - slippage_pct + random_jitter)

        # Check Gap Mortal
        sl_hit_immediately = False
        if order['side'] == SIDE_BUY and real_entry <= order['sl']: sl_hit_immediately = True
        if order['side'] == SIDE_SELL and real_entry >= order['sl']: sl_hit_immediately = True
        
        comm = notional * COMMISSION_PCT
        self.state.balance -= comm
        
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": order['side'], "quantity": order['qty'], 
            "entry_price": real_entry, "entry_type": order['type'], 
            "tps_hit_count": 0, "total_pnl": -comm, 
            "sl": order['sl'], "tps": order['tps'], 
            "entry_time": self.current_timestamp, "comm_entry": comm, 
            "trailing_sl_price": None, 
            "slippage_impact": slippage_pct
        }
        self.state.last_known_position_qty = order['qty']
        self.state.sl_moved_to_be = False
        
        if sl_hit_immediately:
            self.state.current_price = real_entry
            self.close_position("Gap Kill", median_vol, exit_price_ref=real_entry)

    def move_sl_to_be(self):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def update_sl(self, new_price):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = new_price
            self.state.sl_moved_to_be = True

    # C1: Recibe exit_price_ref para cálculo de slippage base
    def close_position(self, reason, current_median_vol, exit_price_ref=None):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        # Si no hay precio de referencia (ej. TimeStop), usamos el actual
        if exit_price_ref is None:
            exit_price_ref = self.state.current_price

        notional = info['quantity'] * exit_price_ref
        slippage_pct = self.calculate_slippage_pct(notional, current_median_vol)
        
        # Aplicar Slippage sobre el precio de referencia (Low/High/TP)
        if info['side'] == SIDE_BUY: # Vender
            real_exit = exit_price_ref * (1 - slippage_pct)
        else: # Comprar
            real_exit = exit_price_ref * (1 + slippage_pct)

        pnl_gross = (real_exit - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        
        comm_exit = (real_exit * info['quantity']) * COMMISSION_PCT
        net_pnl = pnl_gross - comm_exit - info.get('comm_entry', 0)
        
        self.state.balance += (pnl_gross - comm_exit)
        
        cooldown = 300
        if net_pnl > 0: cooldown = 0
        elif net_pnl < 0: cooldown = 900
        self.state.trade_cooldown_until = self.current_timestamp + cooldown

        self.state.trades_history.append({
            'entry_time': info.get('entry_time'), 'exit_time': self.state.current_time,
            'side': info['side'], 'type': info['entry_type'],
            'pnl': net_pnl, 'reason': reason,
            'impact_exit': slippage_pct
        })
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.daily_trade_stats.append({'pnl': net_pnl})

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        high, low, open_p = row.High, row.Low, row.Open
        median_vol = row.MedianVol
        
        # C2: Orden Correcto -> SL -> TP -> Time -> Trailing
        
        # 1. STOP LOSS (Prioridad Máxima)
        current_sl = info['sl']
        sl_hit = False
        if info['side'] == SIDE_BUY and low <= current_sl: sl_hit = True
        if info['side'] == SIDE_SELL and high >= current_sl: sl_hit = True
        
        if sl_hit:
            self.state.current_price = current_sl
            # C1: Usamos Low/High como base del slippage (Peor caso en la vela)
            ref_price = low if info['side'] == SIDE_BUY else high
            self.close_position("Stop-Loss", median_vol, exit_price_ref=ref_price)
            return

        # 2. TAKE PROFIT
        tps = info['tps']
        if tps:
            last_tp = tps[-1]
            tp_hit = False
            if info['side'] == SIDE_BUY and high >= last_tp: tp_hit = True
            if info['side'] == SIDE_SELL and low <= last_tp: tp_hit = True
            
            if tp_hit:
                self.state.current_price = last_tp
                # C1: Usamos el TP exacto como referencia (Limit order llena)
                self.close_position("Take-Profit", median_vol, exit_price_ref=last_tp)
                return
            
            # Check TP Parcial (para mover a BE)
            if len(tps) >= 2:
                tp2 = tps[1]
                hit_tp2 = (info['side'] == SIDE_BUY and high >= tp2) or (info['side'] == SIDE_SELL and low <= tp2)
                if hit_tp2 and info['tps_hit_count'] < 2:
                    info['tps_hit_count'] = 2
                    self.move_sl_to_be()

        # 3. TIME STOP (M1: Ahora después de SL/TP)
        if info['entry_type'].startswith("Ranging"):
             elapsed = self.current_timestamp - info['entry_time']
             if (elapsed / 3600) > TIME_STOP_HOURS:
                 # Salida al Open de la vela actual (ya pasaron 12h)
                 self.close_position(f"Time-Stop ({TIME_STOP_HOURS}h)", median_vol, exit_price_ref=open_p)
                 return

        # 4. TRAILING STOP UPDATE (Al final)
        # (Se hace desde el loop principal usando await risk_manager...)
        pass

    async def run(self):
        print(f"Iniciando Backtest V7.0 (AUDITADO) para {SYMBOL_TO_TEST}...")
        print(f"Config: Vol {VOLUME_FACTOR} | Estricto {STRICT_VOLUME_FACTOR}")
        
        file_1h = f"mainnet_data_1h_{SYMBOL_TO_TEST}.csv"
        file_1m = f"mainnet_data_1m_{SYMBOL_TO_TEST}.csv"
        file_1d = f"mainnet_data_1d_{SYMBOL_TO_TEST}.csv"
        
        try:
            df_1h = pd.read_csv(os.path.join(DATA_DIR, file_1h), index_col="Open_Time", parse_dates=True)
            df_1d = pd.read_csv(os.path.join(DATA_DIR, file_1d), index_col="Open_Time", parse_dates=True)
            df_1m = pd.read_csv(os.path.join(DATA_DIR, file_1m), index_col="Open_Time", parse_dates=True, 
                                usecols=['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume'])
        except FileNotFoundError:
            print("Faltan archivos.")
            return

        print("Calculando Indicadores...")
        # M2: Rolling 180 para menos ruido
        df_1m['MedianVol'] = df_1m['Quote_Asset_Volume'].rolling(window=180).median().shift(1)
        
        df_1h['EMA_1h'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        tr = pd.concat([df_1h['High']-df_1h['Low'], abs(df_1h['High']-df_1h['Close'].shift(1)), abs(df_1h['Low']-df_1h['Close'].shift(1))], axis=1).max(axis=1)
        df_1h['ATR_1h'] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
        df_1h = df_1h.shift(1) 

        print("Fusionando...")
        df_merged = pd.merge_asof(df_1m, df_1h[['EMA_1h', 'ATR_1h']], left_index=True, right_index=True, direction='backward')
        df_merged.dropna(inplace=True)
        
        if TEST_START_DATE: df_merged = df_merged.loc[TEST_START_DATE:]
        if TEST_END_DATE: df_merged = df_merged.loc[:TEST_END_DATE]
        
        if df_merged.empty: return
        self.start_date_actual = df_merged.index[0]
        self.end_date_actual = df_merged.index[-1]
        del df_1h, df_1m
        gc.collect()

        print(f"Simulando {len(df_merged)} velas...")
        current_date_obj = None

        for row in df_merged.itertuples():
            self.current_timestamp = row.Index.timestamp()
            self.state.current_time = row.Index
            self.state.current_price = row.Close
            
            # 1. Ejecutar Pendientes
            if self.state.pending_order:
                self.execute_pending_order(row.Open, row.MedianVol)

            # 2. Gestión de Posición
            if self.state.is_in_position:
                # Trailing se actualiza con el precio actual (Close) para la SIGUIENTE vela
                await self.risk_manager._check_trailing_stop(row.Close, self.state.current_position_info.get('quantity', 0))
                # Chequeo de Salidas (SL/TP/Time)
                self.check_exits(row)

            # 3. Señal (Solo si no hay posición ni orden pendiente)
            # M3: Señal basada en Close, ejecución en Next Open (vía pending_order)
            if not self.state.is_in_position and not self.state.pending_order:
                # Actualizar datos diarios si cambió el día
                row_date = row.Index.date()
                if current_date_obj != row_date:
                    current_date_obj = row_date
                    yesterday_ts = pd.Timestamp(row_date - timedelta(days=1))
                    if yesterday_ts in df_1d.index:
                        d_row = df_1d.loc[yesterday_ts]
                        self.state.daily_pivots = calculate_pivots_from_data(float(d_row['High']), float(d_row['Low']), float(d_row['Close']), self.controller.tick_size, 0.2)
                
                self.state.cached_atr = row.ATR_1h
                self.state.cached_ema = row.EMA_1h
                self.state.cached_median_vol = row.MedianVol
                
                if self.state.daily_pivots:
                    k = {'o': row.Open, 'c': row.Close, 'h': row.High, 'l': row.Low, 'v': row.Volume, 'q': row.Quote_Asset_Volume, 'x': True}
                    await self.risk_manager.seek_new_trade(k)

        self.print_results()

    def print_results(self):
        trades = self.state.trades_history
        if not trades: return
        df = pd.DataFrame(trades)
        total_pnl = df['pnl'].sum()
        wins = len(df[df['pnl'] > 0])
        win_rate = (wins / len(df)) * 100
        gross_profit = df[df['pnl'] > 0]['pnl'].sum()
        gross_loss = abs(df[df['pnl'] < 0]['pnl'].sum())
        pf = gross_profit / gross_loss if gross_loss != 0 else 0
        
        avg_slip = df['impact_exit'].mean() * 100 if 'impact_exit' in df.columns else 0

        print("\n" + "="*50)
        print(f" RESULTADOS V7.0 (AUDITADO): {SYMBOL_TO_TEST}")
        print(f" Periodo: {self.start_date_actual} - {self.end_date_actual}")
        print(f" Avg Exit Slippage: {avg_slip:.4f}%")
        print("-" * 50)
        print(f" PnL Neto:        ${total_pnl:.2f}")
        print(f" Balance Final:   ${self.state.balance:.2f}")
        print(f" Profit Factor:   {pf:.2f}")
        print(f" Win Rate:        {win_rate:.2f}%")
        print(f" Total Trades:    {len(df)}")
        print("="*50)
        df.to_csv(os.path.join(DATA_DIR, f"backtest_v7_{SYMBOL_TO_TEST}.csv"))

if __name__ == "__main__":
    asyncio.run(BacktesterV7().run())