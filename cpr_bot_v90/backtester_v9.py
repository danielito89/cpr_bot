#!/usr/bin/env python3
# backtester_v9.py
# Versión: v9.0 (Auditada y Corregida)
# - Slippage Realista (Volumen Dinámico)
# - Ejecución Next Open (Sin Lookahead)
# - Diagnóstico de Pivotes (Para evitar el error de 32 trades)

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- CONFIGURACIÓN ---
SYMBOL_TO_TEST = "ETHUSDT"
START_BALANCE = 10000
COMMISSION_PCT = 0.0004
# Slippage más realista (0.02% base + impacto)
BASE_SLIPPAGE = 0.0002
IMPACT_COEF = 0.0001 
MAX_IMPACT = 0.01

# Estrategia
VOLUME_FACTOR = 1.2
STRICT_VOLUME_FACTOR = 1.5
TEST_START_DATE = "2022-01-01"
TEST_END_DATE = "2025-12-01"

# Rutas
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Imports Mock
try:
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import SIDE_BUY, SIDE_SELL
except ImportError:
    print("Error importando bot_core. Ejecuta desde la carpeta correcta.")
    sys.exit(1)

# ... (Mocks de Telegram y Orders iguales a v8) ...
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
        self.state = simulator.state 
        self.simulator = simulator
        self.lock = asyncio.Lock()
        self.telegram_handler = MockTelegram()
        self.orders_manager = MockOrdersManager(simulator)
        
        # Configuración
        self.investment_pct = 0.05
        self.leverage = 30
        self.volume_factor = VOLUME_FACTOR
        self.strict_volume_factor = STRICT_VOLUME_FACTOR
        self.cpr_width_threshold = 0.2
        self.take_profit_levels = 3
        self.breakout_atr_sl_multiplier = 1.0
        self.breakout_tp_mult = 10.0
        self.ranging_atr_multiplier = 0.5
        self.range_tp_mult = 2.0 
        self.daily_loss_limit_pct = 15.0
        self.min_volatility_atr_pct = 0.5
        self.trailing_stop_trigger_atr = 1.25
        self.trailing_stop_distance_atr = 1.0
        self.MAX_TRADE_SIZE_USDT = 50000
        self.MAX_DAILY_TRADES = 50
        
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

class BacktesterV9:
    def __init__(self):
        self.state = SimulatorState()
        self.current_timestamp = 0.0
        self.controller = MockBotController(self, SYMBOL_TO_TEST)
        self.risk_manager = RiskManager(self.controller)

    def calculate_slippage(self, notional, median_vol):
        # Fix: Evitar división por cero o NaN
        if not median_vol or median_vol <= 0: return MAX_IMPACT
        vol_ratio = notional / median_vol
        impact = IMPACT_COEF * (vol_ratio ** 0.8)
        return BASE_SLIPPAGE + min(MAX_IMPACT, impact)

    def stage_order(self, side, qty, price, sl, tps, type_):
        self.state.pending_order = {
            "side": side, "qty": qty, "sl": sl, "tps": tps, "type": type_,
            "signal_price": price, "created_at": self.current_timestamp
        }

    def execute_pending_order(self, open_price, median_vol):
        if self.current_timestamp < self.state.trade_cooldown_until:
            self.state.pending_order = None
            return

        order = self.state.pending_order
        self.state.pending_order = None
        
        notional = order['qty'] * open_price
        slip = self.calculate_slippage(notional, median_vol)
        
        # Ejecución en Next Open con Slippage
        if order['side'] == SIDE_BUY: real_entry = open_price * (1 + slip)
        else: real_entry = open_price * (1 - slip)

        # Check Gap (Si abrió saltando el SL)
        sl_hit = False
        if order['side'] == SIDE_BUY and real_entry <= order['sl']: sl_hit = True
        if order['side'] == SIDE_SELL and real_entry >= order['sl']: sl_hit = True
        
        comm = notional * COMMISSION_PCT
        self.state.balance -= comm
        
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": order['side'], "quantity": order['qty'], 
            "entry_price": real_entry, "entry_type": order['type'], 
            "tps_hit_count": 0, "total_pnl": -comm, 
            "sl": order['sl'], "tps": order['tps'], 
            "entry_time": self.current_timestamp, "comm_entry": comm, 
            "trailing_sl_price": None, "slippage_impact": slip
        }
        self.state.last_known_position_qty = order['qty']
        self.state.sl_moved_to_be = False
        
        if sl_hit:
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

    def close_position(self, reason, current_median_vol, exit_price_ref=None):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        # Precio base (SL price o TP price o Current si es manual)
        base_price = exit_price_ref if exit_price_ref else self.state.current_price
        
        notional = info['quantity'] * base_price
        slip = self.calculate_slippage(notional, current_median_vol)
        
        if info['side'] == SIDE_BUY: real_exit = base_price * (1 - slip)
        else: real_exit = base_price * (1 + slip)

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
            'impact_exit': slip
        })
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.daily_trade_stats.append({'pnl': net_pnl})

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        high, low = row.High, row.Low
        current_sl = info['sl']
        
        # SL (Prioridad)
        sl_hit = False
        if info['side'] == SIDE_BUY and low <= current_sl: sl_hit = True
        if info['side'] == SIDE_SELL and high >= current_sl: sl_hit = True
        
        if sl_hit:
            self.state.current_price = current_sl
            # Fix: Usar current_sl como referencia, slippage aplicará sobre eso
            self.close_position("Stop-Loss", row.MedianVol, exit_price_ref=current_sl)
            return

        # TP (Check)
        tps = info['tps']
        if tps:
            last_tp = tps[-1]
            tp_hit = False
            if info['side'] == SIDE_BUY and high >= last_tp: tp_hit = True
            if info['side'] == SIDE_SELL and low <= last_tp: tp_hit = True
            
            if tp_hit:
                self.state.current_price = last_tp
                self.close_position("Take-Profit", row.MedianVol, exit_price_ref=last_tp)
                return
            
            if len(tps) >= 2:
                tp2 = tps[1]
                if (info['side'] == SIDE_BUY and high >= tp2) or \
                   (info['side'] == SIDE_SELL and low <= tp2):
                    if info['tps_hit_count'] < 2:
                        info['tps_hit_count'] = 2
                        self.move_sl_to_be()

    async def run(self):
        print(f"Iniciando Backtest V9.0 para {SYMBOL_TO_TEST}...")
        
        # Carga de datos (Asegúrate que los paths estén bien)
        try:
            df_1h = pd.read_csv(os.path.join(DATA_DIR, f"mainnet_data_1h_{SYMBOL_TO_TEST}.csv"), index_col="Open_Time", parse_dates=True)
            df_1d = pd.read_csv(os.path.join(DATA_DIR, f"mainnet_data_1d_{SYMBOL_TO_TEST}.csv"), index_col="Open_Time", parse_dates=True)
            df_1m = pd.read_csv(os.path.join(DATA_DIR, f"mainnet_data_1m_{SYMBOL_TO_TEST}.csv"), index_col="Open_Time", parse_dates=True, 
                                usecols=['Open_Time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Quote_Asset_Volume'])
        except FileNotFoundError:
            print(f"❌ Faltan archivos para {SYMBOL_TO_TEST}. Ejecuta download_data.py")
            return

        print("Calculando Indicadores...")
        df_1m['MedianVol'] = df_1m['Quote_Asset_Volume'].rolling(window=60).median().shift(1)
        df_1h['EMA_1h'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        tr = pd.concat([df_1h['High']-df_1h['Low'], abs(df_1h['High']-df_1h['Close'].shift(1)), abs(df_1h['Low']-df_1h['Close'].shift(1))], axis=1).max(axis=1)
        df_1h['ATR_1h'] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
        df_1h = df_1h.shift(1) 

        print("Fusionando...")
        df_merged = pd.merge_asof(df_1m, df_1h[['EMA_1h', 'ATR_1h']], left_index=True, right_index=True, direction='backward')
        df_merged.dropna(inplace=True)
        
        if TEST_START_DATE: df_merged = df_merged.loc[TEST_START_DATE:]
        if TEST_END_DATE: df_merged = df_merged.loc[:TEST_END_DATE]

        print(f"Simulando {len(df_merged)} velas...")
        
        current_date_obj = None
        pivots_missing = 0

        for row in df_merged.itertuples():
            self.current_timestamp = row.Index.timestamp()
            self.state.current_time = row.Index
            self.state.current_price = row.Close
            
            # 1. Pendientes (Next Open)
            if self.state.pending_order:
                self.execute_pending_order(row.Open, row.MedianVol)

            # 2. Gestión
            if self.state.is_in_position:
                await self.risk_manager._check_trailing_stop(row.Close, self.state.current_position_info.get('quantity', 0))
                self.check_exits(row)

            # 3. Señales
            if not self.state.is_in_position and not self.state.pending_order:
                row_date = row.Index.date()
                if current_date_obj != row_date:
                    current_date_obj = row_date
                    # IMPORTANTE: Buscar el día anterior en df_1d
                    # Ajuste de zona horaria simple: restar 1 día
                    yesterday_ts = pd.Timestamp(row_date - timedelta(days=1))
                    
                    if yesterday_ts in df_1d.index:
                        d_row = df_1d.loc[yesterday_ts]
                        self.state.daily_pivots = calculate_pivots_from_data(
                            h=float(d_row['High']), l=float(d_row['Low']), c=float(d_row['Close']), 
                            tick_size=self.controller.tick_size, cpr_width_threshold=0.2
                        )
                    else:
                        self.state.daily_pivots = None
                        pivots_missing += 1

                self.state.cached_atr = row.ATR_1h
                self.state.cached_ema = row.EMA_1h
                self.state.cached_median_vol = row.MedianVol
                
                if self.state.daily_pivots:
                    k = {'o': row.Open, 'c': row.Close, 'h': row.High, 'l': row.Low, 'v': row.Volume, 'q': row.Quote_Asset_Volume, 'x': True}
                    await self.risk_manager.seek_new_trade(k)

        if pivots_missing > 0:
            print(f"⚠️ ADVERTENCIA: Faltaron pivotes en {pivots_missing} días (Datos diarios incompletos).")
        self.print_results()

    def print_results(self):
        trades = self.state.trades_history
        if not trades:
            print("Sin trades.")
            return
        df = pd.DataFrame(trades)
        total_pnl = df['pnl'].sum()
        wins = len(df[df['pnl'] > 0])
        win_rate = (wins / len(df)) * 100
        pf = df[df['pnl'] > 0]['pnl'].sum() / abs(df[df['pnl'] < 0]['pnl'].sum())
        
        print(f"\nRESULTADOS V9.0 (Auditado): {SYMBOL_TO_TEST}")
        print(f"PnL: ${total_pnl:.2f} | PF: {pf:.2f} | WinRate: {win_rate:.2f}% | Trades: {len(df)}")

if __name__ == "__main__":
    asyncio.run(BacktesterV9().run())