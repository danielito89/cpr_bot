#!/usr/bin/env python3
# v10_optimizer.py
# Optimizador Bayesiano para el Motor V10 (RiskManager Real + Datos Locales)

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
from datetime import datetime, timedelta
from hyperopt import fmin, tpe, hp, Trials, STATUS_OK
import warnings

# Filtrar advertencias de librerías
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.CRITICAL)

# --- CONFIGURACIÓN ---
SYMBOL = "ETHUSDT"
TIMEFRAME = '1h'
DATA_FOLDER = "data"
TRADING_START_DATE = "2023-01-01" 
BUFFER_DAYS = 25
CAPITAL_INICIAL = 1000

# Importaciones del Bot Real
try:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"❌ Error importando bot_core: {e}")
    sys.exit(1)

# ==========================================
# 1. CLASES MOCK Y SESIÓN
# ==========================================

class SimulatorState:
    def __init__(self):
        self.balance = CAPITAL_INICIAL
        # --- FIX: Variables requeridas por RiskManager ---
        self.daily_start_balance = CAPITAL_INICIAL  # <--- FALTABA ESTO
        self.daily_trade_stats = []                 # <--- Y ESTO
        # ---------------------------------------------
        self.trades_history = []
        self.is_in_position = False
        self.current_position_info = {}
        self.last_known_position_qty = 0.0
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0
        self.trading_paused = False
        self.cached_atr = 0
        self.cached_ema = 0
        self.cached_median_vol = 0
        self.daily_pivots = {}
        self.current_timestamp = 0
        self.current_price = 0
    def save_state(self): pass

class BacktestSession:
    """
    Clase que actúa como 'Simulador' para una sola corrida de optimización.
    """
    def __init__(self):
        self.state = SimulatorState()
        self.commission = 0.0004
        self.slippage = 0.0002

    def open_position(self, side, qty, price, sl, tps, type_):
        # Simular entrada
        real_price = price * (1 + self.slippage) if side == SIDE_BUY else price * (1 - self.slippage)
        cost = (qty * real_price) * self.commission
        
        self.state.balance -= cost
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": qty, "entry_price": real_price,
            "sl": sl, "tps": tps, "entry_type": type_,
            "tps_hit_count": 0, "trailing_sl_price": None,
            "entry_time": self.state.current_timestamp
        }
        self.state.last_known_position_qty = qty
        self.state.sl_moved_to_be = False

    def close_position(self, reason, exit_price_override=None):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        # Precio de salida
        base_price = exit_price_override if exit_price_override else self.state.current_price
        
        # Slippage salida
        real_exit = base_price * (1 - self.slippage) if info['side'] == SIDE_BUY else base_price * (1 + self.slippage)
        
        pnl_gross = (real_exit - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        
        cost = (info['quantity'] * real_exit) * self.commission
        net_pnl = pnl_gross - cost
        
        self.state.balance += net_pnl
        # Registro para métricas y para RiskManager (daily limits)
        self.state.trades_history.append({'pnl': net_pnl})
        self.state.daily_trade_stats.append({'pnl': net_pnl}) 
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        # Cooldown simple
        self.state.trade_cooldown_until = self.state.current_timestamp + (900 if net_pnl < 0 else 0)

    def move_sl_to_be(self):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def update_sl(self, new_price):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = new_price

class MockTelegram:
    async def _send_message(self, text): pass

class MockOrdersManager:
    def __init__(self, simulator): self.sim = simulator
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        self.sim.open_position(side, qty, price, sl, tps, type)
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
        for k, v in config_dict.items():
            setattr(self, k, v)

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

# ==========================================
# 2. CARGADOR DE DATOS (Global)
# ==========================================
GLOBAL_DF = None
GLOBAL_START_DATE = None
GLOBAL_DAILY_DF = None

def cargar_datos_memoria():
    global GLOBAL_DF, GLOBAL_START_DATE, GLOBAL_DAILY_DF
    
    filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, DATA_FOLDER, filename)
    
    print(f"📂 Cargando dataset en memoria: {filepath}...")
    df = pd.read_csv(filepath)
    df.columns = [col.lower() for col in df.columns]
    col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
    df[col_fecha] = pd.to_datetime(df[col_fecha])
    df.set_index(col_fecha, inplace=True)
    
    # Pre-cálculos masivos
    df['median_vol'] = df['quote_asset_volume'].rolling(60).median().shift(1)
    df['ema'] = df['close'].ewm(span=20).mean().shift(1)
    
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean().shift(1)

    target_start = pd.to_datetime(TRADING_START_DATE)
    start_buffer = target_start - timedelta(days=BUFFER_DAYS)
    GLOBAL_DF = df[df.index >= start_buffer].copy()
    GLOBAL_START_DATE = target_start

    daily_df = df.resample('1D').agg({'high':'max','low':'min','close':'last'})
    daily_df['prev_high'] = daily_df['high'].shift(1)
    daily_df['prev_low'] = daily_df['low'].shift(1)
    daily_df['prev_close'] = daily_df['close'].shift(1)
    GLOBAL_DAILY_DF = daily_df
    
    print(f"✅ Datos en RAM: {len(GLOBAL_DF)} velas.")

# ==========================================
# 3. EJECUCIÓN ASÍNCRONA
# ==========================================
async def run_backtest_async(params):
    config = {
        "symbol": SYMBOL,
        "investment_pct": 0.05,
        "leverage": 20,
        "cpr_width_threshold": 0.2,
        "take_profit_levels": 3,
        "breakout_atr_sl_multiplier": 1.0,
        "ranging_atr_multiplier": 0.5,
        "range_tp_mult": 2.0,
        "daily_loss_limit_pct": 15.0,
        "min_volatility_atr_pct": 0.3,
        "trailing_stop_distance_atr": 1.0,
        "tick_size": 0.01,
        "step_size": 0.001,
        "MAX_TRADE_SIZE_USDT": 50000,
        "MAX_DAILY_TRADES": 50,
        # Params optimizados
        "volume_factor": params['volume_factor'],
        "strict_volume_factor": params['strict_volume_factor'],
        "breakout_tp_mult": params['breakout_tp_mult'],
        "trailing_stop_trigger_atr": params['trailing_stop_trigger_atr']
    }

    # Instanciamos la sesión (Simulador)
    session = BacktestSession()
    
    # El controlador recibe la sesión, no solo el estado
    controller = MockBotController(session, config)
    
    risk_manager = RiskManager(controller)
    
    df = GLOBAL_DF
    target_start = GLOBAL_START_DATE
    daily_df = GLOBAL_DAILY_DF

    for current_time, row in df.iterrows():
        if current_time < target_start: continue
        
        session.state.current_timestamp = current_time.timestamp()
        session.state.current_price = row.close
        session.state.cached_atr = row.atr
        session.state.cached_ema = row.ema
        session.state.cached_median_vol = row.median_vol
        
        # Resetear estadísticas diarias si cambia el día (Simulado simple)
        if session.state.daily_start_balance:
             # En una simulación real exacta deberíamos resetear daily_trade_stats a las 00:00
             # Para optimización rápida, podemos omitirlo o implementarlo si afecta mucho el Daily Limit.
             pass

        today_str = str(current_time.date())
        if today_str in daily_df.index:
             d_data = daily_df.loc[today_str]
             if not pd.isna(d_data['prev_high']):
                 session.state.daily_pivots = calculate_pivots_from_data(
                     d_data['prev_high'], d_data['prev_low'], d_data['prev_close'], 
                     0.01, 0.2
                 )

        # 1. Gestión Salidas
        if session.state.is_in_position:
            await risk_manager._check_trailing_stop(row.close, session.state.current_position_info.get('quantity', 0))
            
            info = session.state.current_position_info
            high, low = row.high, row.low
            
            # SL
            if info.get('sl'):
                hit = (info['side'] == SIDE_BUY and low <= info['sl']) or (info['side'] == SIDE_SELL and high >= info['sl'])
                if hit: session.close_position("SL", exit_price_override=info['sl'])

            # TP
            if session.state.is_in_position and info.get('tps'):
                last_tp = info['tps'][-1]
                hit = (info['side'] == SIDE_BUY and high >= last_tp) or (info['side'] == SIDE_SELL and low <= last_tp)
                if hit: session.close_position("TP", exit_price_override=last_tp)

        # 2. Entradas
        if not session.state.is_in_position and session.state.daily_pivots:
            kline = {'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low, 'v': row.volume, 'q': row.quote_asset_volume, 'x': True}
            await risk_manager.seek_new_trade(kline)

    return _calc_metrics(session.state)

def _calc_metrics(state):
    trades = state.trades_history
    if not trades: return {'loss': 999, 'pf': 0, 'pnl': 0, 'winrate': 0, 'trades': 0}
    
    df = pd.DataFrame(trades)
    pnl = df['pnl'].sum()
    wins = df[df['pnl'] > 0]
    losses = df[df['pnl'] <= 0]
    
    winrate = len(wins) / len(df) * 100
    gross_win = wins['pnl'].sum()
    gross_loss = abs(losses['pnl'].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else 999
    
    # Score compuesto
    score = -(pnl * 0.7 + (pf * 1000) * 0.3) 
    
    return {
        'loss': score,
        'status': STATUS_OK,
        'pf': pf,
        'pnl': pnl,
        'winrate': winrate,
        'trades': len(df)
    }

# ==========================================
# 4. WRAPPER HYPEROPT
# ==========================================
def objective(params):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    res = loop.run_until_complete(run_backtest_async(params))
    loop.close()
    
    if res['trades'] > 10:
        print(f"Vol:{params['strict_volume_factor']:.1f} | TP:{params['breakout_tp_mult']:.1f} | Trail:{params['trailing_stop_trigger_atr']:.1f} -> PF:{res['pf']:.2f} | PnL:${res['pnl']:.0f} | WR:{res['winrate']:.1f}%")
    
    return res

def run_optimizer():
    cargar_datos_memoria()
    
    print("\n🧠 INICIANDO OPTIMIZACIÓN BAYESIANA (V10)...")
    print(f"   Objetivo: Maximizar PnL y Profit Factor")
    print("-" * 60)
    
    space = {
        'volume_factor': hp.uniform('volume_factor', 1.0, 1.3),
        'strict_volume_factor': hp.quniform('strict_volume_factor', 1.5, 25.0, 0.5),
        'breakout_tp_mult': hp.uniform('breakout_tp_mult', 1.25, 15.0),
        'trailing_stop_trigger_atr': hp.uniform('trailing_stop_trigger_atr', 1.0, 6.0)
    }
    
    trials = Trials()
    best = fmin(
        fn=objective,
        space=space,
        algo=tpe.suggest,
        max_evals=100,
        trials=trials
    )
    
    print("\n" + "="*60)
    print("🏆 MEJOR CONFIGURACIÓN ENCONTRADA")
    print("="*60)
    print(best)
    
    results = []
    for t in trials.trials:
        r = t['result']
        p = t['misc']['vals']
        params_clean = {k: v[0] for k, v in p.items()}
        row = {**params_clean, 'pnl': r['pnl'], 'pf': r['pf'], 'winrate': r['winrate'], 'trades': r['trades']}
        results.append(row)
        
    df_res = pd.DataFrame(results).sort_values(by='pnl', ascending=False)
    df_res.to_csv("optimization_v10_results.csv", index=False)
    print("\nResultados guardados en 'optimization_v10_results.csv'")

if __name__ == "__main__":
    run_optimizer()