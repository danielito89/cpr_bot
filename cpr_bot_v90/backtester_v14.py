#!/usr/bin/env python3
# backtester_v14.py
# NIVEL: EQUILIBRADO (REALISTA) + REPORTE PROFESIONAL
# FIXES:
# 1. Execution at Open (N+1).
# 2. Heurística de Proximidad para SL/TP.
# 3. Reporte detallado (Mensual, Drawdown, etc).
# 4. Mock save_state para evitar crash.

import os
import sys
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
TRADING_START_DATE = "2023-01-01"
BUFFER_DAYS = 25
CAPITAL_INICIAL = 1000

CONFIG_SIMULADA = {
    "symbol": SYMBOL,
    "investment_pct": 0.05,
    "leverage": 20,              
    "cpr_width_threshold": 0.2,
    "volume_factor": 1.1,
    "strict_volume_factor": 1.5,
    "take_profit_levels": 3,
    "breakout_atr_sl_multiplier": 1.0,
    "breakout_tp_mult": 1.25,
    "ranging_atr_multiplier": 0.5,
    "range_tp_mult": 2.0,
    "daily_loss_limit_pct": 15.0,
    "min_volatility_atr_pct": 0.3,
    "trailing_stop_trigger_atr": 1.25,
    "trailing_stop_distance_atr": 1.0,
    "tick_size": 0.01,
    "step_size": 0.001,
    "MAX_TRADE_SIZE_USDT": 50000,
    "MAX_DAILY_TRADES": 10
}

DATA_FOLDER = "data"

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

    def save_state(self): pass

# ==========================================
# 3. MOTOR DE BACKTEST (BALANCED)
# ==========================================
class BacktesterV14:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, CONFIG_SIMULADA)
        self.risk_manager = RiskManager(self.controller)
        self.last_date = None
        
        self.commission = 0.0004 
        self.slippage = 0.0002   

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
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def close_position(self, reason, specific_price=None):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        exit_p = specific_price if specific_price else self.state.current_price
        
        # Slippage de salida
        real_exit = exit_p * (1 - self.slippage) if info['side'] == SIDE_BUY else exit_p * (1 + self.slippage)
        
        pnl_gross = (real_exit - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        
        cost = (info['quantity'] * real_exit) * self.commission
        net_pnl = pnl_gross - cost
        
        self.state.balance += net_pnl
        self.state.equity_curve.append(self.state.balance)
        
        self.state.trades_history.append({
            'date': datetime.fromtimestamp(self.state.current_timestamp),
            'type': info['entry_type'], 'side': info['side'],
            'pnl_usd': net_pnl, 'reason': reason, 'balance': self.state.balance
        })
        self.state.daily_trade_stats.append({'pnl': net_pnl, 'timestamp': self.state.current_timestamp})
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.trade_cooldown_until = self.state.current_timestamp + (900 if net_pnl < 0 else 0)

    # --- LÓGICA DE EJECUCIÓN ---
    def execute_pending_order(self, row):
        order = self.state.pending_order
        open_price = row.open
        
        # Slippage sobre el Open
        real_entry = open_price * (1 + self.slippage) if order['side'] == SIDE_BUY else open_price * (1 - self.slippage)
        
        cost = (order['quantity'] * real_entry) * self.commission
        self.state.balance -= cost
        
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": order['side'], 
            "quantity": order['quantity'], 
            "entry_price": real_entry,
            "sl": order['sl'], 
            "tps": order['tps'], 
            "entry_type": order['type'],
            "tps_hit_count": 0, 
            "entry_time": self.state.current_timestamp
        }
        self.state.pending_order = None 

    # --- LÓGICA DE SALIDA INTRA-VELA ---
    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        open_p = row.open
        high = row.high
        low = row.low
        sl_price = info.get('sl')
        tps = info.get('tps', [])
        
        hit_sl = False
        hit_tp = False
        final_tp = tps[-1] if tps else None
        
        if sl_price:
            if (info['side'] == SIDE_BUY and low <= sl_price) or \
               (info['side'] == SIDE_SELL and high >= sl_price):
                hit_sl = True
        
        if final_tp:
            if (info['side'] == SIDE_BUY and high >= final_tp) or \
               (info['side'] == SIDE_SELL and low <= final_tp):
                hit_tp = True

        # Resolución de Conflictos (Heurística de Proximidad)
        if hit_sl and hit_tp:
            dist_sl = abs(open_p - sl_price)
            dist_tp = abs(open_p - final_tp)
            if dist_sl < dist_tp:
                self.close_position("SL (Prox)", sl_price)
            else:
                self.close_position("TP (Prox)", final_tp)
            return

        elif hit_sl:
            self.close_position("SL", sl_price)
            return

        elif hit_tp:
            self.close_position("TP", final_tp)
            return

        # TP Parcial
        if len(tps) > 1 and info['tps_hit_count'] == 0:
            tp1 = tps[0]
            hit_tp1 = (info['side'] == SIDE_BUY and high >= tp1) or (info['side'] == SIDE_SELL and low <= tp1)
            if hit_tp1:
                info['tps_hit_count'] = 1
                self.move_sl_to_be()

    # --- CARGA DE DATOS ---
    def load_data(self):
        filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
        filepath = os.path.join("data", filename)
        if not os.path.exists(filepath): return None, None
        
        print(f"📂 Cargando CSV: {filepath} ...")
        df = pd.read_csv(filepath)
        df.columns = [col.lower() for col in df.columns]
        col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
        df[col_fecha] = pd.to_datetime(df[col_fecha])
        df.set_index(col_fecha, inplace=True)
        
        target_start = pd.to_datetime(TRADING_START_DATE)
        start_buffer = target_start - timedelta(days=BUFFER_DAYS)
        df = df[df.index >= start_buffer].copy()
        
        # Pre-cálculos
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

        # Pivotes
        daily_df = df.resample('1D').agg({'high':'max','low':'min','close':'last'})
        daily_df['prev_high'] = daily_df['high'].shift(1)
        daily_df['prev_low'] = daily_df['low'].shift(1)
        daily_df['prev_close'] = daily_df['close'].shift(1)
        
        print(f"🚀 INICIANDO BACKTEST V14 (BALANCED)...")
        print("⚡ Execution at Open | Proximity Heuristic | Realistic Trailing")
        
        for current_time, row in df.iterrows():
            if current_time < target_start: continue
            
            # Reset Diario
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
            
            # 1. EJECUCIÓN PENDIENTE
            if self.state.pending_order and not self.state.is_in_position:
                self.execute_pending_order(row)

            # 2. GESTIÓN DE POSICIÓN
            if self.state.is_in_position:
                ts_check_price = row.high if self.state.current_position_info['side'] == SIDE_BUY else row.low
                await self.risk_manager._check_trailing_stop(ts_check_price, self.state.current_position_info['quantity'])
                self.check_exits(row)

            # 3. ACTUALIZACIÓN DE ESTADO
            self.state.cached_atr = row.atr
            self.state.cached_ema = row.ema
            self.state.cached_median_vol = row.median_vol

            # 4. DECISIÓN
            if not self.state.is_in_position and not self.state.pending_order:
                kline = {
                    'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low,
                    'v': row.volume, 'q': row.quote_asset_volume, 'x': True
                }
                await self.risk_manager.seek_new_trade(kline)

        self.generate_professional_report()

    # --- REPORTE PROFESIONAL RESTAURADO ---
    def generate_professional_report(self):
        trades = self.state.trades_history
        equity = self.state.equity_curve
        
        if not trades:
            print("\n⚠️ Sin operaciones.")
            return

        df_t = pd.DataFrame(trades)
        
        # 1. Métricas Generales
        total_trades = len(df_t)
        winners = df_t[df_t['pnl_usd'] > 0]
        losers = df_t[df_t['pnl_usd'] <= 0]
        
        net_pnl = df_t['pnl_usd'].sum()
        win_rate = (len(winners) / total_trades) * 100
        
        avg_win = winners['pnl_usd'].mean() if not winners.empty else 0
        avg_loss = losers['pnl_usd'].mean() if not losers.empty else 0
        risk_reward_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        
        gross_profit = winners['pnl_usd'].sum()
        gross_loss = abs(losers['pnl_usd'].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss != 0 else 0

        # 2. Drawdown Máximo (MDD)
        equity_series = pd.Series(equity)
        running_max = equity_series.cummax()
        drawdown = (equity_series - running_max) / running_max * 100
        max_drawdown = drawdown.min() 

        # 3. Retorno Total
        total_return_pct = ((self.state.balance - CAPITAL_INICIAL) / CAPITAL_INICIAL) * 100

        # 4. Tabla Mensual
        df_t['month'] = df_t['date'].dt.to_period('M')
        monthly_stats = df_t.groupby('month')['pnl_usd'].sum().reset_index()
        monthly_stats['month'] = monthly_stats['month'].astype(str)
        
        # --- IMPRESIÓN DEL REPORTE ---
        print("\n" + "="*60)
        print(f"📊 REPORTE DE BACKTEST PROFESIONAL - V14 (BALANCED)")
        print("="*60)
        
        print(f"{'Balance Inicial:':<25} ${CAPITAL_INICIAL:,.2f}")
        print(f"{'Balance Final:':<25} ${self.state.balance:,.2f}")
        print(f"{'Retorno Neto:':<25} ${net_pnl:,.2f} ({total_return_pct:.2f}%)")
        print(f"{'Max Drawdown:':<25} {max_drawdown:.2f}%")
        print("-" * 60)
        
        print(f"{'Total Trades:':<25} {total_trades}")
        print(f"{'Win Rate:':<25} {win_rate:.2f}%  (W: {len(winners)} | L: {len(losers)})")
        print(f"{'Profit Factor:':<25} {profit_factor:.2f}")
        print(f"{'Avg Win:':<25} ${avg_win:.2f}")
        print(f"{'Avg Loss:':<25} ${avg_loss:.2f}")
        print(f"{'Ratio Riesgo/Beneficio:':<25} 1 : {risk_reward_ratio:.2f}")
        print("=" * 60)
        
        print("\n📅 DESGLOSE MENSUAL:")
        print("-" * 40)
        print(f"{'Mes':<15} | {'PnL (USD)':>15}")
        print("-" * 40)
        for _, row in monthly_stats.iterrows():
            print(f"{row['month']:<15} | ${row['pnl_usd']:>14,.2f}")
        print("-" * 40)

        # Gráfico
        try:
            plt.figure(figsize=(12, 8))
            # Subplot 1: Equity
            plt.subplot(2, 1, 1)
            plt.plot([t['date'] for t in trades], [t['balance'] for t in trades], color='green')
            plt.title(f'Curva de Equidad V14 - PF: {profit_factor:.2f}')
            plt.grid(True, alpha=0.3)
            plt.yscale('log')
            
            # Subplot 2: Drawdown
            plt.subplot(2, 1, 2)
            plt.plot([t['date'] for t in trades], [0] * len(trades), color='black', alpha=0.0) # Dummy fill
            # (Generar serie de DD para plotear requeriría re-alinear índices, simplificamos ploteando equity line para rapidez)
            # Mejor solo guardamos Equity para no complicar el script con pandas indexing
            
            plt.tight_layout()
            plt.savefig('backtest_v14_balanced.png')
            print("\n📈 Gráfico guardado: backtest_v14_balanced.png")
            print("   (Usa 'python3 -m http.server 8000' para verlo)")
        except Exception as e:
            print(f"Error graficando: {e}")

if __name__ == "__main__":
    asyncio.run(BacktesterV14().run())