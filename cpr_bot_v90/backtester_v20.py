#!/usr/bin/env python3
# backtester_v20.py
# NIVEL: V220 (Supply & Demand - Price Action)
# USO: python cpr_bot_v90/backtester_v19.py --symbol ETHUSDT --start 2022-01-01

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
import argparse
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(message)s")

DEFAULT_SYMBOL = "ETHUSDT"
DEFAULT_START_DATE = "2021-01-01"
TIMEFRAME = '1h' 
BUFFER_DAYS = 200
CAPITAL_INICIAL = 1000

try:
    from bot_core.risk_pullback import RiskManager
    from bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
except ImportError as e:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from cpr_bot_v90.bot_core.risk_pullback import RiskManager
        from cpr_bot_v90.bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
    except ImportError:
        print(f"❌ Error importando bot_core: {e}")
        sys.exit(1)

class MockTelegram:
    async def _send_message(self, text): pass

class MockOrdersManager:
    def __init__(self, simulator): self.sim = simulator
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        self.sim.stage_order(side, qty, price, sl, tps, type)
    async def move_sl_to_be(self, qty): self.sim.move_sl_to_be()
    async def update_sl(self, new_price, qty, reason=""): self.sim.update_sl(new_price)
    async def close_position_manual(self, reason): self.sim.close_position(reason)

class MockBotController:
    def __init__(self, simulator, config_dict):
        self.client = None
        self.telegram_handler = MockTelegram()
        self.orders_manager = MockOrdersManager(simulator)
        self.state = simulator.state
        self.lock = asyncio.Lock()
        for k, v in config_dict.items(): setattr(self, k, v)
        
    async def _get_account_balance(self): return self.state.balance
    def get_current_timestamp(self): return self.state.current_timestamp
    async def _get_current_position(self):
        if not self.state.is_in_position: return None
        info = self.state.current_position_info
        amt = info.get('quantity', 0)
        if info.get('side') == SIDE_SELL: amt = -amt
        return {
            "positionAmt": amt, "entryPrice": info.get('entry_price'),
            "markPrice": self.state.current_price, "unRealizedProfit": 0
        }

class SimulatorState:
    def __init__(self):
        self.balance = CAPITAL_INICIAL
        self.equity_curve = [CAPITAL_INICIAL]
        self.trades_history = []
        self.is_in_position = False
        self.current_position_info = {}
        self.pending_order = None 
        self.trading_paused = False
        self.sl_moved_to_be = False
        self.last_known_position_qty = 0.0
        
        # MEMORIA DE S/D
        self.active_zones = [] # Lista de diccionarios {'type': 'SUPPLY/DEMAND', 'top': float, 'bottom': float, 'created_at': ts}
        
        # Datos row actual
        self.current_row = None
        self.current_timestamp = 0
        self.current_price = 0
    def save_state(self): pass

class BacktesterV19:
    def __init__(self, symbol, start_date, custom_file=None):
        self.symbol = symbol
        self.start_date = start_date
        self.custom_file = custom_file
        is_pepe = "PEPE" in symbol
        self.tick_size = 0.0000001 if is_pepe else 0.01
        self.step_size = 1 if is_pepe else 0.001
        self.timeframe = TIMEFRAME
        self.config = {
            "symbol": symbol, "investment_pct": 0.05, "leverage": 5,
            "tick_size": self.tick_size, "step_size": self.step_size, 
            "MAX_TRADE_SIZE_USDT": 50000
        }
        self.state = SimulatorState()
        self.controller = MockBotController(self, self.config)
        self.risk_manager = RiskManager(self.controller)
        self.commission = 0.0006
        self.base_slippage = 0.0001
        self.tp_splits = [1.0] 

    def calculate_dynamic_slippage(self, price, qty, candle_volume_usdt):
        if candle_volume_usdt <= 0: return 0.05
        # Asumimos que podemos tomar el 1% de la liquidez sin impacto masivo en S/D
        available_liquidity = candle_volume_usdt * 0.01 
        trade_size_usdt = price * qty
        impact_factor = trade_size_usdt / available_liquidity
        total_slippage = self.base_slippage + (0.001 * impact_factor)
        return min(total_slippage, 0.05)

    def stage_order(self, side, qty, price, sl, tps, type_):
        self.state.pending_order = {"side": side, "quantity": qty, "sl": sl, "tps": tps, "type": type_}

    def update_sl(self, price):
        if self.state.is_in_position: self.state.current_position_info['sl'] = price

    def move_sl_to_be(self):
        if self.state.is_in_position and not self.state.sl_moved_to_be:
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def execute_exit(self, reason, price, qty_to_close, candle_volume):
        info = self.state.current_position_info
        if qty_to_close <= 0: return 0.0
        slippage_pct = self.calculate_dynamic_slippage(price, qty_to_close, candle_volume)
        real_exit = price * (1 - slippage_pct) if info['side'] == SIDE_BUY else price * (1 + slippage_pct)
        pnl_gross = (real_exit - info['entry_price']) * qty_to_close if info['side'] == SIDE_BUY else (info['entry_price'] - real_exit) * qty_to_close
        cost = (qty_to_close * real_exit) * self.commission
        net_pnl = pnl_gross - cost
        self.state.balance += net_pnl
        self.state.equity_curve.append(self.state.balance)
        info['accumulated_pnl'] = info.get('accumulated_pnl', 0.0) + net_pnl
        self.state.trades_history.append({
            'date': datetime.fromtimestamp(self.state.current_timestamp),
            'type': info['entry_type'], 'side': info['side'],
            'pnl_usd': net_pnl, 'reason': reason, 'balance': self.state.balance,
            'slippage_pct': slippage_pct * 100
        })
        info['quantity'] -= qty_to_close
        if info['quantity'] < (self.step_size / 10): info['quantity'] = 0.0
        return net_pnl

    def close_position(self, reason, specific_price=None, candle_volume=1000000):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        price = specific_price if specific_price else self.state.current_price
        self.execute_exit(reason, price, info['quantity'], candle_volume)
        self.state.is_in_position = False
        self.state.current_position_info = {}

    def execute_pending_order(self, row):
        order = self.state.pending_order
        candle_vol = row.quote_asset_volume
        slip = self.calculate_dynamic_slippage(row.open, order['quantity'], candle_vol)
        real_entry = row.open * (1 + slip) if order['side'] == SIDE_BUY else row.open * (1 - slip)
        cost = (order['quantity'] * real_entry) * self.commission
        self.state.balance -= cost
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": order['side'], "quantity": order['quantity'], "initial_quantity": order['quantity'],
            "entry_price": real_entry, "sl": order['sl'], "tps": order['tps'], "entry_type": order['type'],
            "tps_hit_count": 0, "accumulated_pnl": -cost, "entry_time": self.state.current_timestamp
        }
        self.state.pending_order = None

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        sl, tps = info.get('sl'), info.get('tps', [])
        hit_sl = (info['side'] == SIDE_BUY and row.low <= sl) or (info['side'] == SIDE_SELL and row.high >= sl) if sl else False
        
        hit_tp = False
        if tps:
            tp_price = tps[0]
            if (info['side'] == SIDE_BUY and row.high >= tp_price) or (info['side'] == SIDE_SELL and row.low <= tp_price):
                hit_tp = True
        
        if hit_sl: self.close_position("SL", sl, row.quote_asset_volume); return
        if hit_tp: self.close_position("TP", tps[0], row.quote_asset_volume); return

    def load_data(self):
        # ... (Carga de archivos estándar)
        if self.custom_file:
            possible_files = [self.custom_file]
        else:
            print(f"🔍 Buscando datos para {self.symbol}...")
            folder_paths = ["/home/orangepi/bot_cpr/data", "data", "cpr_bot_v90/data", "."]
            prefix = f"mainnet_data_{self.timeframe}_{self.symbol}"
            possible_files = []
            for folder in folder_paths:
                if os.path.exists(folder):
                    try:
                        for f in os.listdir(folder):
                            if f.startswith(prefix) and f.endswith(".csv"):
                                possible_files.append(os.path.join(folder, f))
                    except: continue
        
        if not possible_files: return None, None
        possible_files.sort(reverse=True) 
        filepath = possible_files[0]
        print(f"📂 Cargando: {filepath}")
        
        try:
            df = pd.read_csv(filepath)
            df.columns = [col.lower() for col in df.columns]
            col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
            df[col_fecha] = pd.to_datetime(df[col_fecha])
            df.set_index(col_fecha, inplace=True)
            df = df.sort_index()

            target_start = pd.to_datetime(self.start_date)
            start_buffer = target_start - timedelta(days=BUFFER_DAYS)
            df = df[df.index >= start_buffer].copy()
            
            # --- CÁLCULO DE ESTRUCTURA (V220) ---
            
            # 1. Swing Points (Fractales de 5 velas)
            # Un High es Swing High si es mayor que 2 velas atras y 2 adelante
            # Nota: Esto introduce "lookahead" de 2 velas en backtest, pero en live es un retraso de 2 velas.
            # Para simular live, usamos shift(2)
            df['is_swing_high'] = (df['high'] > df['high'].shift(1)) & (df['high'] > df['high'].shift(2)) & \
                                  (df['high'] > df['high'].shift(-1)) & (df['high'] > df['high'].shift(-2))
            df['is_swing_low'] = (df['low'] < df['low'].shift(1)) & (df['low'] < df['low'].shift(2)) & \
                                 (df['low'] < df['low'].shift(-1)) & (df['low'] < df['low'].shift(-2))
            
            # Ajustamos para no ver el futuro: La señal se confirma 2 velas DESPUÉS
            df['swing_high_val'] = np.where(df['is_swing_high'], df['high'], np.nan)
            df['swing_low_val'] = np.where(df['is_swing_low'], df['low'], np.nan)
            
            # Forward fill para saber el último swing en cualquier momento
            # Importante: Shift(2) para simular que recien nos enteramos 2 velas despues
            df['last_swing_high'] = df['swing_high_val'].shift(2).ffill()
            df['last_swing_low'] = df['swing_low_val'].shift(2).ffill()
            
            # Penúltimo swing (para estructura HH/HL)
            df['prev_swing_high'] = df['swing_high_val'].shift(2).rolling(100).apply(lambda x: pd.Series(x).dropna().iloc[-2] if len(pd.Series(x).dropna()) >= 2 else np.nan, raw=False)
            df['prev_swing_low'] = df['swing_low_val'].shift(2).rolling(100).apply(lambda x: pd.Series(x).dropna().iloc[-2] if len(pd.Series(x).dropna()) >= 2 else np.nan, raw=False)

            # 2. Impulsos (Velas Grandes)
            df['body_size'] = (df['close'] - df['open']).abs()
            df['avg_body'] = df['body_size'].rolling(20).mean()
            df['is_impulse'] = df['body_size'] > (df['avg_body'] * 2.0)
            
            return df, target_start
        except Exception as e:
            print(f"❌ Error leyendo CSV: {e}")
            return None, None

    async def run(self):
        df, target_start = self.load_data()
        if df is None: return
        print(f"\n🛡️ INICIANDO BACKTEST V220 (Supply & Demand)")
        print(f"🎯 Par: {self.symbol} | Inicio: {self.start_date}")
        print("-" * 60)
        
        for current_time, row in df.iterrows():
            if current_time < target_start: continue
            
            self.state.current_timestamp = current_time.timestamp()
            self.state.current_price = row.close 
            self.state.current_row = row # Guardamos row para acceso fácil
            
            if self.state.pending_order and not self.state.is_in_position: self.execute_pending_order(row)
            if self.state.is_in_position:
                self.check_exits(row) # Gestión simple por ahora

            if not self.state.is_in_position and not self.state.pending_order:
                # El análisis completo ocurre en risk_pure usando state.current_row
                await self.risk_manager.seek_new_trade({})

        self.generate_report()

    def generate_report(self):
        trades = self.state.trades_history
        if not trades: print("⚠️ Sin operaciones."); return
        df_t = pd.DataFrame(trades)
        winners = df_t[df_t['pnl_usd'] > 0]
        losers = df_t[df_t['pnl_usd'] <= 0]
        gross_profit = winners['pnl_usd'].sum()
        gross_loss = abs(losers['pnl_usd'].sum())
        win_rate = (len(winners) / len(df_t)) * 100
        profit_factor = (gross_profit / gross_loss) if gross_loss != 0 else 999.0
        
        equity = pd.Series(self.state.equity_curve)
        max_dd = ((equity - equity.cummax()) / equity.cummax() * 100).min()
        
        csv_filename = f"trades_{self.symbol}_{self.start_date}.csv"
        df_t.to_csv(csv_filename, index=False)
        
        print("\n" + "="*60)
        print(f"📊 REPORTE V220 (S/D Structure) - {self.symbol}")
        print("="*60)
        print(f"💰 Balance Final:     ${self.state.balance:,.2f}")
        print(f"🚀 Retorno Total:     {((self.state.balance-CAPITAL_INICIAL)/CAPITAL_INICIAL)*100:.2f}%")
        print(f"📉 Max Drawdown:      {max_dd:.2f}%")
        print(f"🏆 Profit Factor:     {profit_factor:.2f}")
        print(f"🎲 Win Rate:          {win_rate:.2f}%")
        print(f"🔢 Total Trades:      {len(df_t)}")
        print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL)
    parser.add_argument("--start", type=str, default=DEFAULT_START_DATE)
    parser.add_argument("--file", type=str, default=None)
    args = parser.parse_args()
    try:
        bt = BacktesterV19(symbol=args.symbol, start_date=args.start, custom_file=args.file)
        asyncio.run(bt.run())
    except KeyboardInterrupt: pass