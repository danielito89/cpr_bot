#!/usr/bin/env python3
# backtester_v18.py
# NIVEL: AUDIT-READY / ZERO-BUG / FULL EXPORT
# ACTUALIZADO: Incluye exportación CSV y Rotación de Gráficos

import os
import sys
import glob
import pandas as pd
import numpy as np
import asyncio
import logging
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- 1. CONFIGURACIÓN ---
SYMBOL = "ETHUSDT" 
TIMEFRAME = '1m'
TRADING_START_DATE = "2020-01-01"
BUFFER_DAYS = 25
CAPITAL_INICIAL = 1000

EXECUTION_MODE = "SMART" # "SMART" vs "WORST"

# CONFIGURACIÓN DINÁMICA DE LIQUIDEZ
if "PEPE" in SYMBOL:
    TICK_SIZE = 0.0000001
    STEP_SIZE = 1
    MAX_TRADE_CAP = 20000 
    PARTICIPATION_RATE = 0.02 
else: # ETH, BTC
    TICK_SIZE = 0.01
    STEP_SIZE = 0.001
    MAX_TRADE_CAP = 50000
    PARTICIPATION_RATE = 0.10 

CONFIG_SIMULADA = {
    "symbol": SYMBOL,
    "investment_pct": 0.05,
    "leverage": 15,
    "cpr_width_threshold": 0.2,
    "volume_factor": 1.1,
    "strict_volume_factor": 20.0,
    "take_profit_levels": 3,
    "breakout_atr_sl_multiplier": 1.0,
    "breakout_tp_mult": 1.25,
    "indicator_update_interval_minutes": 3, # ¡IMPORTANTE! Simular el lag de 3 min
    "ranging_atr_multiplier": 0.5,
    "range_tp_mult": 2.0,
    "daily_loss_limit_pct": 15.0,
    "min_volatility_atr_pct": 0.3,
    "trailing_stop_trigger_atr": 1.25,
    "trailing_stop_distance_atr": 1.0,
    "tick_size": TICK_SIZE,
    "step_size": STEP_SIZE,
    "MAX_TRADE_SIZE_USDT": MAX_TRADE_CAP, 
    "MAX_DAILY_TRADES": 5
}

TP_SPLITS = [0.30, 0.30, 0.40] 

try:
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"❌ Error importando bot_core: {e}")
    sys.exit(1)

# ==========================================
# 2. MOCKS
# ==========================================
class MockTelegram:
    async def _send_message(self, text): pass

class MockOrdersManager:
    def __init__(self, simulator): self.sim = simulator
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        self.sim.stage_order(side, qty, price, sl, tps, type)
    async def move_sl_to_be(self, qty): 
        self.sim.move_sl_to_be()
    async def update_sl(self, new_price, qty, reason=""): 
        self.sim.update_sl(new_price)
    async def close_position_manual(self, reason): 
        self.sim.close_position(reason)

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
            "positionAmt": amt,
            "entryPrice": info.get('entry_price'),
            "markPrice": self.state.current_price,
            "unRealizedProfit": 0
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
        
        self.cached_atr = 0
        self.cached_ema = 0
        self.cached_median_vol = 0
        self.daily_pivots = {}
        self.current_timestamp = 0
        self.current_price = 0
    
    def save_state(self): 
        pass

# ==========================================
# 3. MOTOR V18 (AUDIT-READY)
# ==========================================
class BacktesterV18:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, CONFIG_SIMULADA)
        self.risk_manager = RiskManager(self.controller)
        self.last_date = None
        
        self.commission = 0.0006  
        self.base_slippage = 0.0001 

    def calculate_dynamic_slippage(self, price, qty, candle_volume_usdt):
        if candle_volume_usdt <= 0: return 0.05
        available_liquidity = candle_volume_usdt * PARTICIPATION_RATE
        trade_size_usdt = price * qty
        impact_factor = trade_size_usdt / available_liquidity
        total_slippage = self.base_slippage + (0.001 * impact_factor)
        return min(total_slippage, 0.10)

    # --- ORDER MANAGEMENT ---
    def stage_order(self, side, qty, price, sl, tps, type_):
        self.state.pending_order = {
            "side": side, "quantity": qty, "sl": sl, "tps": tps, "type": type_
        }

    def update_sl(self, price):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = price

    def move_sl_to_be(self):
        if self.state.is_in_position:
            if not self.state.sl_moved_to_be:
                self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
                self.state.sl_moved_to_be = True

    # --- EXECUTE EXIT ---
    def execute_exit(self, reason, price, qty_to_close, candle_volume):
        info = self.state.current_position_info
        if qty_to_close <= 0: return 0.0

        slippage_pct = self.calculate_dynamic_slippage(price, qty_to_close, candle_volume)
        
        if info['side'] == SIDE_BUY:
            real_exit = price * (1 - slippage_pct) 
            pnl_gross = (real_exit - info['entry_price']) * qty_to_close
        else: # SELL
            real_exit = price * (1 + slippage_pct) 
            pnl_gross = (info['entry_price'] - real_exit) * qty_to_close
        
        notional = qty_to_close * real_exit
        cost = notional * self.commission
        net_pnl = pnl_gross - cost
        
        self.state.balance += net_pnl
        self.state.equity_curve.append(self.state.balance)
        
        info['accumulated_pnl'] = info.get('accumulated_pnl', 0.0) + net_pnl

        self.state.trades_history.append({
            'date': datetime.fromtimestamp(self.state.current_timestamp),
            'type': info['entry_type'], 
            'side': info['side'],
            'pnl_usd': net_pnl, 
            'reason': reason, 
            'balance': self.state.balance,
            'slippage_pct': slippage_pct * 100
        })
        self.state.daily_trade_stats.append({'pnl': net_pnl, 'timestamp': self.state.current_timestamp})

        info['quantity'] -= qty_to_close
        if info['quantity'] < (STEP_SIZE / 10): 
            info['quantity'] = 0.0
            
        return net_pnl

    def close_position(self, reason, specific_price=None, candle_volume=1000000):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        price = specific_price if specific_price else self.state.current_price
        qty = info['quantity']
        
        self.execute_exit(reason, price, qty, candle_volume)
        
        total_trade_pnl = info.get('accumulated_pnl', 0.0)
        self.state.is_in_position = False
        self.state.current_position_info = {}
        
        if total_trade_pnl < 0:
            self.state.trade_cooldown_until = self.state.current_timestamp + 900
        else:
            self.state.trade_cooldown_until = self.state.current_timestamp

    # --- ENTRADA ---
    def execute_pending_order(self, row):
        order = self.state.pending_order
        open_price = row.open
        candle_vol_usdt = row.quote_asset_volume
        
        slippage_pct = self.calculate_dynamic_slippage(open_price, order['quantity'], candle_vol_usdt)
        
        if order['side'] == SIDE_BUY:
            real_entry = open_price * (1 + slippage_pct) 
        else:
            real_entry = open_price * (1 - slippage_pct) 
        
        notional = order['quantity'] * real_entry
        cost = notional * self.commission
        self.state.balance -= cost
        
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": order['side'], 
            "quantity": order['quantity'], 
            "initial_quantity": order['quantity'], 
            "entry_price": real_entry,
            "sl": order['sl'], 
            "tps": order['tps'], 
            "entry_type": order['type'],
            "tps_hit_count": 0, 
            "accumulated_pnl": -cost, 
            "entry_time": self.state.current_timestamp
        }
        self.state.pending_order = None 

    # --- CHECK EXITS ---
    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        open_p = row.open
        high = row.high
        low = row.low
        vol_usdt = row.quote_asset_volume
        
        sl_price = info.get('sl')
        tps = info.get('tps', [])
        
        hit_sl = False
        if sl_price:
            if (info['side'] == SIDE_BUY and low <= sl_price) or \
               (info['side'] == SIDE_SELL and high >= sl_price):
                hit_sl = True

        tps_hit_in_candle = []
        for i, tp in enumerate(tps):
            if i < info['tps_hit_count']: continue 
            
            is_hit = False
            if info['side'] == SIDE_BUY and high >= tp: is_hit = True
            if info['side'] == SIDE_SELL and low <= tp: is_hit = True
            
            if is_hit: tps_hit_in_candle.append((i, tp))

        # CONFLICT RESOLUTION
        if hit_sl and tps_hit_in_candle:
            first_tp_idx, first_tp_price = tps_hit_in_candle[0]
            
            if EXECUTION_MODE == "WORST":
                self.close_position("SL (Worst)", sl_price, vol_usdt)
                return
            
            elif EXECUTION_MODE == "SMART":
                dist_sl = abs(open_p - sl_price)
                dist_tp = abs(open_p - first_tp_price)
                
                if dist_tp < dist_sl:
                    self._process_partial_tp(first_tp_idx, first_tp_price, vol_usdt)
                    
                    new_sl = info.get('sl') 
                    if new_sl:
                        hit_new_sl = False
                        if (info['side'] == SIDE_BUY and low <= new_sl) or \
                           (info['side'] == SIDE_SELL and high >= new_sl):
                            hit_new_sl = True
                        
                        if hit_new_sl:
                            self.close_position("SL/BE (Post-TP)", new_sl, vol_usdt)
                    return
                else:
                    self.close_position("SL (Smart)", sl_price, vol_usdt)
                    return

        if hit_sl:
            self.close_position("SL", sl_price, vol_usdt)
            return

        for idx, tp_price in tps_hit_in_candle:
            if info['quantity'] <= 0: break
            if idx == len(tps) - 1:
                self.close_position("TP Final", tp_price, vol_usdt)
                return
            else:
                self._process_partial_tp(idx, tp_price, vol_usdt)

    def _process_partial_tp(self, tp_idx, price, vol_usdt):
        info = self.state.current_position_info
        total_initial = info.get('initial_quantity', info['quantity']) 
        
        split_pct = TP_SPLITS[tp_idx] if tp_idx < len(TP_SPLITS) else 0.0
        qty_to_close = total_initial * split_pct
        
        if qty_to_close > info['quantity']: qty_to_close = info['quantity']
        
        self.execute_exit(f"TP{tp_idx+1} Partial", price, qty_to_close, vol_usdt)
        
        info['tps_hit_count'] = tp_idx + 1
        if tp_idx == 0:
            self.move_sl_to_be()

    def load_data(self):
        #filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
        filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}_2020-2021.csv"
        filepath = os.path.join("data", filename)
        if not os.path.exists(filepath):
             print(f"⚠️ No encontré {filename}, buscando el normal...")
             filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
             filepath = os.path.join("data", filename)
             all_files = os.listdir("data")
             csvs = [f for f in all_files if f.endswith(".csv")]
             if csvs: filepath = os.path.join("data", csvs[0])
             else: return None, None
        
        print(f"📂 Cargando: {filepath}")
        df = pd.read_csv(filepath)
        df.columns = [col.lower() for col in df.columns]
        col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
        df[col_fecha] = pd.to_datetime(df[col_fecha])
        df.set_index(col_fecha, inplace=True)
        
        target_start = pd.to_datetime(TRADING_START_DATE)
        start_buffer = target_start - timedelta(days=BUFFER_DAYS)
        df = df[df.index >= start_buffer].copy()
        
        df['median_vol'] = df['quote_asset_volume'].rolling(60).median().shift(1)
        df['ema'] = df['close'].ewm(span=20).mean().shift(1)
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift(1)).abs(),
            (df['low'] - df['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean().shift(1)
        return df, target_start

    async def run(self):
        df, target_start = self.load_data()
        if df is None: return

        daily_df = df.resample('1D').agg({'high':'max','low':'min','close':'last'})
        daily_df['prev_high'] = daily_df['high'].shift(1)
        daily_df['prev_low'] = daily_df['low'].shift(1)
        daily_df['prev_close'] = daily_df['close'].shift(1)
        
        print(f"\n🛡️ INICIANDO V18 AUDIT-READY ({SYMBOL})")
        print(f"⚙️ MODE: {EXECUTION_MODE} | Liquidity: {PARTICIPATION_RATE*100}% | Splits: {TP_SPLITS}")
        
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
                            CONFIG_SIMULADA['tick_size'], CONFIG_SIMULADA['cpr_width_threshold']
                        )

            self.state.current_timestamp = current_time.timestamp()
            self.state.current_price = row.close 
            
            if self.state.pending_order and not self.state.is_in_position:
                self.execute_pending_order(row)

            if self.state.is_in_position:
                ts_check_price = row.high if self.state.current_position_info['side'] == SIDE_BUY else row.low
                await self.risk_manager._check_trailing_stop(ts_check_price, self.state.current_position_info['quantity'])
                self.check_exits(row)

            self.state.cached_atr = row.atr
            self.state.cached_ema = row.ema
            self.state.cached_median_vol = row.median_vol

            if not self.state.is_in_position and not self.state.pending_order:
                kline = {
                    'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low,
                    'v': row.volume, 'q': row.quote_asset_volume, 'x': True
                }
                await self.risk_manager.seek_new_trade(kline)

        self.generate_report()

    def generate_report(self):
        trades = self.state.trades_history
        if not trades:
            print("⚠️ Sin operaciones.")
            return

        df_t = pd.DataFrame(trades)
        
        # --- CÁLCULOS PROFESIONALES ---
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
        
        # [EXPORTACIÓN PARA MONTE CARLO]
        df_t['prev_balance'] = df_t['balance'] - df_t['pnl_usd']
        df_t['return_pct'] = df_t['pnl_usd'] / df_t['prev_balance']
        csv_filename = f"trades_v18_{SYMBOL}.csv"
        df_t.to_csv(csv_filename, index=False)
        print(f"💾 Datos guardados para Monte Carlo: {csv_filename}")
        
        print("\n" + "="*60)
        print(f"📊 REPORTE V18 (FULL METRICS) - {SYMBOL}")
        print("="*60)
        print(f"⚙️  CONFIG: Lev x{CONFIG_SIMULADA['leverage']} | Vol {CONFIG_SIMULADA['volume_factor']} | Strict {CONFIG_SIMULADA['strict_volume_factor']}")
        print(f"🛠️  MODE: {EXECUTION_MODE} | TP Mult: {CONFIG_SIMULADA['breakout_tp_mult']} | Liq: {PARTICIPATION_RATE*100}%")
        print("-" * 60)
        print(f"💰 Balance Inicial: ${CAPITAL_INICIAL:,.2f}")
        print(f"💰 Balance Final:   ${self.state.balance:,.2f}")
        print(f"🚀 Retorno Total:   {((self.state.balance-CAPITAL_INICIAL)/CAPITAL_INICIAL)*100:.2f}%")
        print(f"📉 Max Drawdown:    {max_dd:.2f}%")
        print("-" * 60)
        print(f"🎲 Win Rate:        {win_rate:.2f}%")
        print(f"🏆 Profit Factor:   {profit_factor:.2f}")
        print(f"⚖️ Risk/Reward:     1 : {payoff_ratio:.2f}")
        print(f"🧠 Expectancy:      ${expectancy:.2f} por ejecución")
        print("-" * 60)
        print(f"🔢 Total Legs:      {total_legs}")
        print(f"✅ Winning Legs:    {len(winners)} (Avg: ${avg_win:.2f})")
        print(f"❌ Losing Legs:     {len(losers)}  (Avg: ${avg_loss:.2f})")
        print(f"💧 Avg Slippage:    {df_t['slippage_pct'].mean()*100:.4f}%")
        print("=" * 60)
        
        # --- SISTEMA DE GRÁFICOS ---
        output_folder = "backtest_results"
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{output_folder}/bt_{SYMBOL}_{timestamp}_PF{profit_factor:.2f}.png"

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [3, 1]})
        
        ax1.plot(pd.to_datetime(df_t['date']), df_t['balance'], label='Equity', color='green')
        ax1.set_title(f"V18 {SYMBOL} | PF: {profit_factor:.2f} | DD: {max_dd:.2f}% | Net: ${net_pnl:,.0f}")
        ax1.set_yscale('log')
        ax1.grid(True, alpha=0.3)
        ax1.legend()
        
        ax2.plot(drawdown.values, color='red', linewidth=1)
        ax2.set_title("Drawdown %")
        ax2.fill_between(range(len(drawdown)), drawdown, 0, color='red', alpha=0.3)
        ax2.grid(True, alpha=0.3)
        
        plt.tight_layout()
        plt.savefig(filename)
        print(f"📈 Gráfico guardado: {filename}")
        plt.close(fig)

        # Rotación de logs (Keep last 10)
        list_of_files = glob.glob(f"{output_folder}/*.png")
        list_of_files.sort(key=os.path.getctime)
        while len(list_of_files) > 10:
            oldest = list_of_files.pop(0)
            os.remove(oldest)
            print(f"🗑️ Limpieza: {oldest}")

if __name__ == "__main__":
    try:
        backtester = BacktesterV18()
        asyncio.run(backtester.run())
    except KeyboardInterrupt:
        print("\n🛑 Backtest detenido por el usuario.")
    except Exception as e:
        print(f"\n❌ Error fatal: {e}")