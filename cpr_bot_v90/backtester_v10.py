#!/usr/bin/env python3
# backtester_v10.py
# FUSIÓN: Datos Locales Robustos + Lógica RiskManager Real + REPORTE PROFESIONAL + FIX DAILY STATS

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
from datetime import datetime, timedelta

# Configuración de Logging limpio
logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- 1. CONFIGURACIÓN ---
SYMBOL = "ETHUSDT"
TIMEFRAME = '1m'
TRADING_START_DATE = "2023-01-01"
BUFFER_DAYS = 25
CAPITAL_INICIAL = 1000

# Parámetros (Ajustados para 1m)
CONFIG_SIMULADA = {
    "symbol": SYMBOL,
    "investment_pct": 0.05,
    "leverage": 20,              
    "cpr_width_threshold": 0.2,
    "volume_factor": 1.1,        # Base
    "strict_volume_factor": 1.5, # Trap Hunter
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

# Importaciones de tu Bot Real
try:
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"❌ Error importando bot_core: {e}")
    print("Asegúrate de correr esto desde la carpeta raíz (cpr_bot_v90)")
    sys.exit(1)

# ==========================================
# 2. CLASES MOCK (Simulan Binance)
# ==========================================
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
        
        # Inyectar configuración
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
        
        # Historial general y estadísticas diarias (FIX: Agregado daily_trade_stats)
        self.trades_history = []
        self.daily_trade_stats = [] 
        
        # Variables de estado del bot real
        self.is_in_position = False
        self.current_position_info = {}
        self.last_known_position_qty = 0.0
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0
        self.trading_paused = False
        
        # Indicadores
        self.cached_atr = 0
        self.cached_ema = 0
        self.cached_median_vol = 0
        self.daily_pivots = {}
        
        # Entorno
        self.current_timestamp = 0
        self.current_price = 0

    def save_state(self): pass 

# ==========================================
# 3. CARGADOR DE DATOS
# ==========================================
def cargar_datos_locales_con_buffer(symbol, start_date_str, buffer_days):
    filename = f"mainnet_data_{TIMEFRAME}_{SYMBOL}.csv"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, DATA_FOLDER, filename)
    
    if not os.path.exists(filepath):
        print(f"❌ Archivo no encontrado: {filepath}")
        return None, None

    print(f"📂 Cargando CSV: {filepath} ...")
    df = pd.read_csv(filepath)
    df.columns = [col.lower() for col in df.columns]
    col_fecha = 'open_time' if 'open_time' in df.columns else 'timestamp'
    df[col_fecha] = pd.to_datetime(df[col_fecha])
    df.set_index(col_fecha, inplace=True)
    
    target_start = pd.to_datetime(start_date_str)
    start_buffer = target_start - timedelta(days=buffer_days)
    df_filtrado = df[df.index >= start_buffer].copy()
    
    print(f"✅ Datos cargados: {len(df_filtrado)} velas (Buffer desde {start_buffer.date()})")
    return df_filtrado, target_start

# ==========================================
# 4. MOTOR DE BACKTEST REAL
# ==========================================
class BacktesterV10:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, CONFIG_SIMULADA)
        self.risk_manager = RiskManager(self.controller)
        self.last_date = None # Para controlar cambio de día
        
        # Costos simulados
        self.commission = 0.0004 # 0.04%
        self.slippage = 0.0002   # 0.02%

    def open_position(self, side, qty, price, sl, tps, type_):
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
        
        # Registro Histórico
        self.state.trades_history.append({
            'date': datetime.fromtimestamp(self.state.current_timestamp),
            'type': info['entry_type'],
            'side': info['side'],
            'pnl_usd': net_pnl,
            'reason': reason,
            'balance': self.state.balance
        })
        
        # Registro Diario (FIX: Para que el RiskManager sepa cuánto ganamos hoy)
        self.state.daily_trade_stats.append({
            'pnl': net_pnl,
            'timestamp': self.state.current_timestamp
        })
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.trade_cooldown_until = self.state.current_timestamp + (900 if net_pnl < 0 else 0)

    def check_exits(self, row):
        if not self.state.is_in_position: return
        info = self.state.current_position_info
        high, low = row.high, row.low
        
        # SL Check
        sl = info.get('sl')
        if sl:
            hit = (info['side'] == SIDE_BUY and low <= sl) or (info['side'] == SIDE_SELL and high >= sl)
            if hit:
                self.state.current_price = sl
                self.close_position("SL")
                return

        # TP Check
        tps = info.get('tps', [])
        if tps:
            last_tp = tps[-1]
            hit = (info['side'] == SIDE_BUY and high >= last_tp) or (info['side'] == SIDE_SELL and low <= last_tp)
            if hit:
                self.state.current_price = last_tp
                self.close_position("TP Final")
                return
            
            # TP Parcial
            if len(tps) > 1:
                tp1 = tps[0]
                hit_tp1 = (info['side'] == SIDE_BUY and high >= tp1) or (info['side'] == SIDE_SELL and low <= tp1)
                if hit_tp1 and info['tps_hit_count'] == 0:
                    info['tps_hit_count'] = 1
                    self.move_sl_to_be()

    async def run(self):
        df, target_start = cargar_datos_locales_con_buffer(SYMBOL, TRADING_START_DATE, BUFFER_DAYS)
        if df is None: return

        # Pre-cálculos
        df['median_vol'] = df['quote_asset_volume'].rolling(60).median().shift(1)
        df['ema'] = df['close'].ewm(span=20).mean().shift(1)
        
        # ATR Manual vectorizado
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean().shift(1)
        
        # Pivotes Diario
        daily_df = df.resample('1D').agg({'high':'max','low':'min','close':'last'})
        daily_df['prev_high'] = daily_df['high'].shift(1)
        daily_df['prev_low'] = daily_df['low'].shift(1)
        daily_df['prev_close'] = daily_df['close'].shift(1)
        
        print(f"🚀 INICIANDO BACKTEST REAL ({len(df)} velas)...")
        print("⏳ Esto puede tardar un poco dependiendo de la velocidad de la Orange Pi...")
        
        for current_time, row in df.iterrows():
            if current_time < target_start: continue
            
            # --- FIX: GESTIÓN DE CAMBIO DE DÍA ---
            current_date = current_time.date()
            if self.last_date is None:
                self.last_date = current_date
            
            if current_date != self.last_date:
                # Nuevo día: Resetear estadísticas diarias y actualizar balance inicial del día
                self.state.daily_trade_stats = []
                self.state.daily_start_balance = self.state.balance
                self.last_date = current_date

            # Actualizar entorno
            self.state.current_timestamp = current_time.timestamp()
            self.state.current_price = row.close
            
            # Actualizar estado para RiskManager
            self.state.cached_atr = row.atr
            self.state.cached_ema = row.ema
            self.state.cached_median_vol = row.median_vol
            
            # Actualizar Pivotes
            today_str = str(current_date)
            if today_str in daily_df.index:
                d_data = daily_df.loc[today_str]
                if not pd.isna(d_data['prev_high']):
                    self.state.daily_pivots = calculate_pivots_from_data(
                        d_data['prev_high'], d_data['prev_low'], d_data['prev_close'], 
                        CONFIG_SIMULADA['tick_size'], CONFIG_SIMULADA['cpr_width_threshold']
                    )

            # --- LÓGICA DE TRADING ---
            if self.state.is_in_position:
                await self.risk_manager._check_trailing_stop(row.close, self.state.current_position_info['quantity'])
                self.check_exits(row)

            if not self.state.is_in_position:
                kline = {
                    'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low,
                    'v': row.volume, 'q': row.quote_asset_volume, 'x': True
                }
                await self.risk_manager.seek_new_trade(kline)

        self.generate_professional_report()

    # ==========================================
    # 5. GENERADOR DE REPORTES PROFESIONAL
    # ==========================================
    def generate_professional_report(self):
        trades = self.state.trades_history
        equity = self.state.equity_curve
        
        if not trades:
            print("\n⚠️ Sin operaciones realizadas. Revisa los filtros de volumen.")
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
        print(f"📊 REPORTE DE BACKTEST PROFESIONAL - {SYMBOL} ({TIMEFRAME})")
        print("="*60)
        
        # A. Resumen Ejecutivo
        print(f"{'Balance Inicial:':<25} ${CAPITAL_INICIAL:,.2f}")
        print(f"{'Balance Final:':<25} ${self.state.balance:,.2f}")
        print(f"{'Retorno Neto:':<25} ${net_pnl:,.2f} ({total_return_pct:.2f}%)")
        print(f"{'Max Drawdown:':<25} {max_drawdown:.2f}%")
        print("-" * 60)
        
        # B. Estadísticas de Trading
        print(f"{'Total Trades:':<25} {total_trades}")
        print(f"{'Win Rate:':<25} {win_rate:.2f}%  (Ganados: {len(winners)} | Perdidos: {len(losers)})")
        print(f"{'Profit Factor:':<25} {profit_factor:.2f}")
        print(f"{'Avg Win:':<25} ${avg_win:.2f}")
        print(f"{'Avg Loss:':<25} ${avg_loss:.2f}")
        print(f"{'Ratio Riesgo/Beneficio:':<25} 1 : {risk_reward_ratio:.2f}")
        print("=" * 60)
        
        # C. Desglose Mensual
        print("\n📅 DESGLOSE MENSUAL:")
        print("-" * 40)
        print(f"{'Mes':<15} | {'PnL (USD)':>15}")
        print("-" * 40)
        for _, row in monthly_stats.iterrows():
            print(f"{row['month']:<15} | ${row['pnl_usd']:>14,.2f}")
        print("-" * 40)
        
        # D. Mejores/Peores Operaciones
        best_trade = df_t.loc[df_t['pnl_usd'].idxmax()]
        worst_trade = df_t.loc[df_t['pnl_usd'].idxmin()]
        
        print("\n🏆 MEJOR TRADE:")
        print(f"   Fecha: {best_trade['date']} | PnL: ${best_trade['pnl_usd']:.2f} | Tipo: {best_trade['type']}")
        
        print("\n💀 PEOR TRADE:")
        print(f"   Fecha: {worst_trade['date']} | PnL: ${worst_trade['pnl_usd']:.2f} | Tipo: {worst_trade['type']}")
        print("=" * 60)
        print("✅ Fin del reporte.")

if __name__ == "__main__":
    asyncio.run(BacktesterV10().run())