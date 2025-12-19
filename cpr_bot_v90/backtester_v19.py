#!/usr/bin/env python3
# backtester_v19.py
# NIVEL: V212 (Slope Filter + Dynamic Trailing)
# USO: python cpr_bot_v90/backtester_v19.py --symbol ETHUSDT --start 2022-01-01

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
import argparse
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

# Valores por defecto
DEFAULT_SYMBOL = "ETHUSDT"
DEFAULT_START_DATE = "2022-01-01"
TIMEFRAME = '15m'
BUFFER_DAYS = 200
CAPITAL_INICIAL = 1000
EXECUTION_MODE = "SMART"

# --- IMPORTS DEL BOT CORE ---
try:
    from bot_core.risk_pure import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, SIDE_BUY, SIDE_SELL
except ImportError as e:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from cpr_bot_v90.bot_core.risk_pure import RiskManager
        from cpr_bot_v90.bot_core.pivots import calculate_pivots_from_data
        from cpr_bot_v90.bot_core.utils import format_price, SIDE_BUY, SIDE_SELL
    except ImportError:
        print(f"❌ Error importando bot_core: {e}")
        sys.exit(1)

# ==========================================
# 1. MOCKS Y CLASES AUXILIARES
# ==========================================
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
        self.daily_start_balance = CAPITAL_INICIAL
        self.trades_history = []
        self.daily_trade_stats = [] 
        self.is_in_position = False
        self.current_position_info = {}
        self.pending_order = None 
        self.trading_paused = False
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0
        self.last_known_position_qty = 0.0
        
        # Indicadores cacheados
        self.cached_atr = 0
        self.cached_ema = 0
        self.cached_ema50 = 0
        self.cached_ema_slope = 0 # <--- NUEVO V212
        self.cached_median_vol = 0
        self.cached_adx = 0
        self.cached_rsi = 50.0
        
        self.daily_pivots = {}
        self.current_timestamp = 0
        self.current_price = 0
    
    def save_state(self): pass

# ==========================================
# 2. MOTOR V19
# ==========================================
class BacktesterV19:
    def __init__(self, symbol, start_date, custom_file=None):
        self.symbol = symbol
        self.start_date = start_date
        self.custom_file = custom_file
        
        is_pepe = "PEPE" in symbol
        self.tick_size = 0.0000001 if is_pepe else 0.01
        self.step_size = 1 if is_pepe else 0.001
        self.participation_rate = 0.02 if is_pepe else 0.10
        self.timeframe = TIMEFRAME
        
        # Configuración V212
        self.config = {
            "symbol": symbol,
            "investment_pct": 0.05,
            "leverage": 8, 
            "cpr_width_threshold": 0.2,
            "volume_factor": 1.1,
            "strict_volume_factor": 2.5,
            "breakout_atr_sl_multiplier": 1.2,  
            "breakout_tp_mult": 3.0,
            "indicator_update_interval_minutes": 15,
            "ranging_atr_multiplier": 0.5,
            "daily_loss_limit_pct": 15.0,
            "trailing_stop_trigger_atr": 1.5,
            "trailing_stop_distance_atr": 1.5,
            "tick_size": self.tick_size,
            "step_size": self.step_size,
            "MAX_TRADE_SIZE_USDT": 20000 if is_pepe else 50000, 
            "MAX_DAILY_TRADES": 50
        }
        
        self.state = SimulatorState()
        self.controller = MockBotController(self, self.config)
        self.risk_manager = RiskManager(self.controller)
        self.last_date = None
        self.commission = 0.0006
        self.base_slippage = 0.0001
        self.tp_splits = [0.30, 0.30, 0.40]

    def calculate_dynamic_slippage(self, price, qty, candle_volume_usdt):
        if candle_volume_usdt <= 0: return 0.05
        available_liquidity = candle_volume_usdt * self.participation_rate
        trade_size_usdt = price * qty
        impact_factor = trade_size_usdt / available_liquidity
        total_slippage = self.base_slippage + (0.001 * impact_factor)
        return min(total_slippage, 0.10)

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
        total_pnl = info.get('accumulated_pnl', 0.0)
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.trade_cooldown_until = self.state.current_timestamp + (900 if total_pnl < 0 else 0)

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
        
        tps_hit = []
        for i, tp in enumerate(tps):
            if i >= info['tps_hit_count']:
                if (info['side'] == SIDE_BUY and row.high >= tp) or (info['side'] == SIDE_SELL and row.low <= tp):
                    tps_hit.append((i, tp))
        
        if hit_sl and tps_hit: 
            dist_sl = abs(row.open - sl)
            dist_tp = abs(row.open - tps_hit[0][1])
            if dist_tp < dist_sl and EXECUTION_MODE == "SMART":
                self._process_partial_tp(tps_hit[0][0], tps_hit[0][1], row.quote_asset_volume)
                return
        
        if hit_sl: self.close_position("SL", sl, row.quote_asset_volume); return
        
        for idx, tp_price in tps_hit:
            if info['quantity'] <= 0: break
            if idx == len(tps) - 1: self.close_position("TP Final", tp_price, row.quote_asset_volume); return
            else: self._process_partial_tp(idx, tp_price, row.quote_asset_volume)

    def _process_partial_tp(self, tp_idx, price, vol_usdt):
        info = self.state.current_position_info
        total_initial = info.get('initial_quantity', info['quantity'])
        split_pct = self.tp_splits[tp_idx] if tp_idx < len(self.tp_splits) else 0.0
        qty = min(total_initial * split_pct, info['quantity'])
        self.execute_exit(f"TP{tp_idx+1} Partial", price, qty, vol_usdt)
        info['tps_hit_count'] = tp_idx + 1
        # El trailing se maneja en risk_pure ahora

    def load_data(self):
        if self.custom_file:
            print(f"📂 Usando archivo personalizado: {self.custom_file}")
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

        if not possible_files:
            print(f"❌ No se encontraron datos CSV (Patrón: {prefix})")
            return None, None

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
            
            if df.index[-1] < target_start:
                print(f"❌ El archivo termina en {df.index[-1]}")
                return None, None

            df = df[df.index >= start_buffer].copy()
            
            # --- INDICADORES ---
            df['median_vol'] = df['quote_asset_volume'].rolling(60).median().shift(1)
            df['ema'] = df['close'].ewm(span=200).mean().shift(1)
            df['ema50'] = df['close'].ewm(span=50).mean().shift(1)
            
            # EMA Slope (Variación absoluta de la EMA 200)
            df['ema_slope'] = df['ema'].diff() # <--- NUEVO V212
            
            tr = pd.concat([
                df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()
            ], axis=1).max(axis=1)
            df['atr'] = tr.rolling(14).mean().shift(1)
            
            adx_period = 14
            df['up_move'] = df['high'] - df['high'].shift(1)
            df['down_move'] = df['low'].shift(1) - df['low']
            df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
            df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
            df['tr'] = df['atr'] 
            df['tr_smooth'] = df['tr'].ewm(alpha=1/adx_period, adjust=False).mean()
            df['plus_dm_smooth'] = df['plus_dm'].ewm(alpha=1/adx_period, adjust=False).mean()
            df['minus_dm_smooth'] = df['minus_dm'].ewm(alpha=1/adx_period, adjust=False).mean()
            df['di_plus'] = 100 * (df['plus_dm_smooth'] / df['tr_smooth'])
            df['di_minus'] = 100 * (df['minus_dm_smooth'] / df['tr_smooth'])
            df['dx'] = 100 * abs(df['di_plus'] - df['di_minus']) / (df['di_plus'] + df['di_minus'])
            df['adx'] = df['dx'].ewm(alpha=1/adx_period, adjust=False).mean()
            
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).fillna(0)
            loss = (-delta.where(delta < 0, 0)).fillna(0)
            avg_gain = gain.rolling(window=14, min_periods=14).mean()
            avg_loss = loss.rolling(window=14, min_periods=14).mean()
            rs = avg_gain / avg_loss
            df['rsi'] = 100 - (100 / (1 + rs))

            return df, target_start
        except Exception as e:
            print(f"❌ Error leyendo CSV: {e}")
            return None, None

    async def run(self):
        df, target_start = self.load_data()
        if df is None: return

        daily_df = df.resample('1D').agg({'high':'max','low':'min','close':'last'})
        daily_df['prev_high'] = daily_df['high'].shift(1)
        daily_df['prev_low'] = daily_df['low'].shift(1)
        daily_df['prev_close'] = daily_df['close'].shift(1)
        
        print(f"\n🛡️ INICIANDO BACKTEST V212 (Professional Edge)")
        print(f"🎯 Par: {self.symbol} | Inicio: {self.start_date}")
        print("-" * 60)
        
        for current_time, row in df.iterrows():
            if current_time < target_start: continue
            
            current_date = current_time.date()
            if self.last_date != current_date:
                self.state.daily_trade_stats = []
                self.state.daily_start_balance = self.state.balance
                self.last_date = current_date
                today_str = str(current_date)
                if today_str in daily_df.index:
                    d_data = daily_df.loc[today_str]
                    if not pd.isna(d_data['prev_high']):
                        self.state.daily_pivots = calculate_pivots_from_data(
                            d_data['prev_high'], d_data['prev_low'], d_data['prev_close'], 
                            self.tick_size, self.config['cpr_width_threshold']
                        )

            self.state.current_timestamp = current_time.timestamp()
            self.state.current_price = row.close 
            
            if self.state.pending_order and not self.state.is_in_position: self.execute_pending_order(row)
            if self.state.is_in_position:
                await self.risk_manager.check_position_state()
                self.check_exits(row)

            self.state.cached_atr = row.atr
            self.state.cached_ema = row.ema
            self.state.cached_ema50 = row.ema50
            self.state.cached_ema_slope = row.ema_slope # <--- PASO AL ESTADO
            self.state.cached_median_vol = row.median_vol
            self.state.cached_adx = row.adx
            self.state.cached_rsi = row.rsi

            if not self.state.is_in_position and not self.state.pending_order:
                kline = {'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low, 'v': row.volume, 'q': row.quote_asset_volume, 'x': True}
                await self.risk_manager.seek_new_trade(kline)

        self.generate_report()

    def generate_report(self):
        trades = self.state.trades_history
        if not trades:
            print("⚠️ Sin operaciones.")
            return

        df_t = pd.DataFrame(trades)
        winners = df_t[df_t['pnl_usd'] > 0]
        losers = df_t[df_t['pnl_usd'] <= 0]
        gross_profit = winners['pnl_usd'].sum()
        gross_loss = abs(losers['pnl_usd'].sum())
        net_pnl = df_t['pnl_usd'].sum()
        total_legs = len(df_t)
        win_rate = (len(winners) / total_legs) * 100
        profit_factor = (gross_profit / gross_loss) if gross_loss != 0 else 999.0
        avg_win = winners['pnl_usd'].mean() if not winners.empty else 0
        avg_loss = losers['pnl_usd'].mean() if not losers.empty else 0
        payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        expectancy = (len(winners)/total_legs * avg_win) + (len(losers)/total_legs * avg_loss)

        equity = pd.Series(self.state.equity_curve)
        running_max = equity.cummax()
        drawdown = (equity - running_max) / running_max * 100
        max_dd = drawdown.min()
        avg_slippage = df_t['slippage_pct'].mean()
        
        csv_filename = f"trades_{self.symbol}_{self.start_date}.csv"
        df_t.to_csv(csv_filename, index=False)
        
        print("\n" + "="*60)
        print(f"📊 REPORTE PROFESIONAL (V212) - {self.symbol}")
        print("="*60)
        print(f"💰 Balance Final:     ${self.state.balance:,.2f}")
        print(f"🚀 Retorno Total:     {((self.state.balance-CAPITAL_INICIAL)/CAPITAL_INICIAL)*100:.2f}%")
        print(f"📉 Max Drawdown:      {max_dd:.2f}%")
        print("-" * 60)
        print(f"🎲 Win Rate:          {win_rate:.2f}%")
        print(f"🏆 Profit Factor:     {profit_factor:.2f}")
        print(f"⚖️ Risk/Reward:       1 : {payoff_ratio:.2f}")
        print(f"🧠 Expectancy:        ${expectancy:.2f} por trade")
        print("-" * 60)
        print(f"🔢 Total Trades:      {total_legs}")
        print(f"✅ Ganadores:         {len(winners)} (Avg: ${avg_win:.2f})")
        print(f"❌ Perdedores:        {len(losers)}  (Avg: ${avg_loss:.2f})")
        print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CPR Bot Backtester V212")
    parser.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL, help="Par a operar")
    parser.add_argument("--start", type=str, default=DEFAULT_START_DATE, help="Fecha inicio")
    parser.add_argument("--file", type=str, default=None, help="Archivo CSV específico")
    args = parser.parse_args()
    
    try:
        bt = BacktesterV19(symbol=args.symbol, start_date=args.start, custom_file=args.file)
        asyncio.run(bt.run())
    except KeyboardInterrupt:
        print("\n🛑 Interrumpido.")
    except Exception as e:
        print(f"\n❌ Error: {e}")