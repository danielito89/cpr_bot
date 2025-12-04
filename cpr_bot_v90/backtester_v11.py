#!/usr/bin/env python3
# backtester_v11_realist.py
# FIX CRÍTICO: Eliminado Lookahead Bias.
# Lógica: Decisión en Cierre (N) -> Ejecución en Apertura (N+1)

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
from datetime import datetime, timedelta
import matplotlib.pyplot as plt

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
    "trailing_stop_trigger_atr": 1.5,
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
# 2. MOCKS (MODIFICADOS PARA EXECUTION DELAY)
# ==========================================
class MockTelegram:
    async def _send_message(self, text): pass

class MockOrdersManager:
    def __init__(self, simulator): self.sim = simulator
    
    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        # FIX: No ejecutar inmediatamente. Encolar para la siguiente vela.
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
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0
        
        # FIX: Variable para orden pendiente (Next Open execution)
        self.pending_order = None 
        
        self.cached_atr = 0
        self.cached_ema = 0
        self.cached_median_vol = 0
        self.daily_pivots = {}
        self.current_timestamp = 0
        self.current_price = 0

# ==========================================
# 3. CARGADOR DE DATOS
# ==========================================
def cargar_datos_locales_con_buffer(symbol, start_date_str, buffer_days):
    filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, DATA_FOLDER, filename)
    if not os.path.exists(filepath): return None, None

    print(f"📂 Cargando CSV: {filepath} ...")
    df = pd.read_csv(filepath)
    df.columns = [col.lower() for col in df.columns]
    col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
    df[col_fecha] = pd.to_datetime(df[col_fecha])
    df.set_index(col_fecha, inplace=True)
    
    target_start = pd.to_datetime(start_date_str)
    start_buffer = target_start - timedelta(days=buffer_days)
    df_filtrado = df[df.index >= start_buffer].copy()
    print(f"✅ Datos cargados: {len(df_filtrado)} velas")
    return df_filtrado, target_start

# ==========================================
# 4. MOTOR DE BACKTEST (LOGICA RIGUROSA)
# ==========================================
class BacktesterV11:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, CONFIG_SIMULADA)
        self.risk_manager = RiskManager(self.controller)
        self.last_date = None
        
        self.commission = 0.0004 
        self.slippage = 0.0002   

    # --- NUEVA LÓGICA DE ORDEN PENDIENTE ---
    def stage_order(self, side, qty, price, sl, tps, type_):
        """Guarda la orden para ejecutarla en la APERTURA de la siguiente vela"""
        self.state.pending_order = {
            "side": side, "quantity": qty, "sl": sl, "tps": tps, "type": type_,
            "signal_price": price # Precio al que se generó la señal (referencia)
        }

    def execute_pending_order(self, open_price):
        """Ejecuta la orden pendiente usando el precio de APERTURA real"""
        order = self.state.pending_order
        if not order: return

        # Ejecutamos al OPEN de la vela actual (N+1), simulando slippage sobre ese Open
        real_entry = open_price * (1 + self.slippage) if order['side'] == SIDE_BUY else open_price * (1 - self.slippage)
        
        # Costo
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
        self.state.sl_moved_to_be = False
        self.state.pending_order = None # Limpiar orden

    # --- UTILS STANDARD ---
    def move_sl_to_be(self):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def update_sl(self, price):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = price

    def close_position(self, reason):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        
        exit_price = self.state.current_price
        real_exit = exit_price * (1 - self.slippage) if info['side'] == SIDE_BUY else exit_price * (1 + self.slippage)
        
        pnl_gross = (real_exit - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        
        cost = (info['quantity'] * real_exit) * self.commission
        net_pnl = pnl_gross - cost
        
        self.state.balance += (pnl_gross - cost)
        self.state.equity_curve.append(self.state.balance)
        
        self.state.trades_history.append({
            'date': datetime.fromtimestamp(self.state.current_timestamp),
            'type': info['entry_type'],
            'side': info['side'],
            'pnl_usd': net_pnl,
            'reason': reason,
            'balance': self.state.balance
        })
        self.state.daily_trade_stats.append({'pnl': net_pnl, 'timestamp': self.state.current_timestamp})
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.trade_cooldown_until = self.state.current_timestamp + (900 if net_pnl < 0 else 0)

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        high, low = row.high, row.low
        
        # Primero revisamos SL (Pesimista: asumimos que SL toca antes que TP en la misma vela)
        sl = info.get('sl')
        if sl:
            hit = (info['side'] == SIDE_BUY and low <= sl) or (info['side'] == SIDE_SELL and high >= sl)
            if hit:
                self.state.current_price = sl
                self.close_position("SL")
                return

        # Luego revisamos TP
        tps = info.get('tps', [])
        if tps:
            last_tp = tps[-1]
            hit = (info['side'] == SIDE_BUY and high >= last_tp) or (info['side'] == SIDE_SELL and low <= last_tp)
            if hit:
                self.state.current_price = last_tp
                self.close_position("TP Final")
                return
            
            if len(tps) > 1:
                tp1 = tps[0]
                hit_tp1 = (info['side'] == SIDE_BUY and high >= tp1) or (info['side'] == SIDE_SELL and low <= tp1)
                if hit_tp1 and info['tps_hit_count'] == 0:
                    info['tps_hit_count'] = 1
                    self.move_sl_to_be()

    async def run(self):
        df, target_start = cargar_datos_locales_con_buffer(SYMBOL, TRADING_START_DATE, BUFFER_DAYS)
        if df is None: return

        # Indicadores vectorizados
        df['median_vol'] = df['quote_asset_volume'].rolling(60).median().shift(1)
        df['ema'] = df['close'].ewm(span=20).mean().shift(1)
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean().shift(1)
        
        daily_df = df.resample('1D').agg({'high':'max','low':'min','close':'last'})
        daily_df['prev_high'] = daily_df['high'].shift(1)
        daily_df['prev_low'] = daily_df['low'].shift(1)
        daily_df['prev_close'] = daily_df['close'].shift(1)
        
        print(f"🚀 INICIANDO BACKTEST REALISTA V11 (Fix Lookahead Bias)...")
        
        for current_time, row in df.iterrows():
            if current_time < target_start: continue
            
            # --- 1. GESTIÓN DE DÍA ---
            current_date = current_time.date()
            if self.last_date is None: self.last_date = current_date
            if current_date != self.last_date:
                self.state.daily_trade_stats = []
                self.state.daily_start_balance = self.state.balance
                self.last_date = current_date

            self.state.current_timestamp = current_time.timestamp()
            self.state.current_price = row.close
            
            # --- 2. GESTIÓN DE ORDEN PENDIENTE (DE LA VELA ANTERIOR) ---
            # Si hubo señal en la vela N, ejecutamos AHORA en el OPEN de la vela N+1
            if self.state.pending_order and not self.state.is_in_position:
                self.execute_pending_order(row.open) # Usamos OPEN para entrar

            # --- 3. GESTIÓN DE SALIDAS (SL/TP) ---
            # Ahora que (potencialmente) entramos en el Open, revisamos si el High/Low de ESTA vela nos saca
            if self.state.is_in_position:
                await self.risk_manager._check_trailing_stop(row.close, self.state.current_position_info['quantity'])
                self.check_exits(row) # Revisa High/Low de la vela actual

            # --- 4. ACTUALIZACIÓN DE ESTADO ---
            self.state.cached_atr = row.atr
            self.state.cached_ema = row.ema
            self.state.cached_median_vol = row.median_vol
            today_str = str(current_date)
            if today_str in daily_df.index:
                d_data = daily_df.loc[today_str]
                if not pd.isna(d_data['prev_high']):
                    self.state.daily_pivots = calculate_pivots_from_data(
                        d_data['prev_high'], d_data['prev_low'], d_data['prev_close'], 
                        CONFIG_SIMULADA['tick_size'], CONFIG_SIMULADA['cpr_width_threshold']
                    )

            # --- 5. BÚSQUEDA DE NUEVA SEÑAL (PARA LA SIGUIENTE VELA) ---
            # Usamos los datos COMPLETOS de la vela actual (Close, High, Low) para decidir.
            # Pero si decidimos entrar, la orden quedará como "Pending" y se ejecutará en el Open de N+1 (Paso 2 del siguiente loop)
            if not self.state.is_in_position and not self.state.pending_order:
                kline = {
                    'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low,
                    'v': row.volume, 'q': row.quote_asset_volume, 'x': True
                }
                await self.risk_manager.seek_new_trade(kline)

        self.generate_professional_report()

    def generate_professional_report(self):
        trades = self.state.trades_history
        equity = self.state.equity_curve
        
        if not trades:
            print("\n⚠️ Sin operaciones realizadas.")
            return

        df_t = pd.DataFrame(trades)
        net_pnl = df_t['pnl_usd'].sum()
        total_trades = len(df_t)
        winners = df_t[df_t['pnl_usd'] > 0]
        losers = df_t[df_t['pnl_usd'] <= 0]
        win_rate = (len(winners) / total_trades) * 100
        profit_factor = (winners['pnl_usd'].sum() / abs(losers['pnl_usd'].sum())) if not losers.empty else 0
        
        equity_series = pd.Series(equity)
        max_drawdown = ((equity_series - equity_series.cummax()) / equity_series.cummax() * 100).min()

        print("\n" + "="*60)
        print(f"📊 REPORTE REALISTA (V11) - {SYMBOL} ({TIMEFRAME})")
        print("="*60)
        print(f"{'Balance Final:':<25} ${self.state.balance:,.2f}")
        print(f"{'PnL Neto:':<25} ${net_pnl:,.2f}")
        print(f"{'Max Drawdown:':<25} {max_drawdown:.2f}%")
        print(f"{'Trades:':<25} {total_trades}")
        print(f"{'Win Rate:':<25} {win_rate:.2f}%")
        print(f"{'Profit Factor:':<25} {profit_factor:.2f}")
        print("="*60)

        # Graficar
        try:
            plt.figure(figsize=(12, 6))
            plt.plot([t['date'] for t in trades], [t['balance'] for t in trades])
            plt.title('Curva de Equidad (Realista)')
            plt.savefig('backtest_v11_realist.png')
            print("📈 Gráfico guardado: backtest_v11_realist.png")
        except: pass

if __name__ == "__main__":
    asyncio.run(BacktesterV11().run())