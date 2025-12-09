#!/usr/bin/env python3
# backtester_v19_hybrid.py
# NIVEL: HYBRID CLI / SMART LOAD
# USO: python backtester_v19.py --start 2022-01-01 --symbol ETHUSDT

import os
import sys
import glob
import pandas as pd
import numpy as np
import asyncio
import logging
import argparse
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

# Valores por defecto (si no usas argumentos)
DEFAULT_SYMBOL = "ETHUSDT"
DEFAULT_START_DATE = "2023-01-01"
TIMEFRAME = '1m'
BUFFER_DAYS = 25
CAPITAL_INICIAL = 1000
EXECUTION_MODE = "SMART"

# --- IMPORTS DEL BOT CORE ---
try:
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, SIDE_BUY, SIDE_SELL
except ImportError as e:
    # Fix para que funcione si corres desde la carpeta raíz o desde cpr_bot_v90
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from cpr_bot_v90.bot_core.risk import RiskManager
        from cpr_bot_v90.bot_core.pivots import calculate_pivots_from_data
        from cpr_bot_v90.bot_core.utils import format_price, SIDE_BUY, SIDE_SELL
    except ImportError:
        print(f"❌ Error importando bot_core: {e}")
        sys.exit(1)

# ==========================================
# 1. MOCKS Y CLASES (Igual que antes)
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
        self.cached_atr = 0
        self.cached_ema = 0
        self.cached_median_vol = 0
        self.daily_pivots = {}
        self.current_timestamp = 0
        self.current_price = 0
    def save_state(self): pass

# ==========================================
# 2. MOTOR V19 HYBRID
# ==========================================
class BacktesterV19:
    def __init__(self, symbol, start_date, custom_file=None):
        self.symbol = symbol
        self.start_date = start_date
        self.custom_file = custom_file
        
        # Configuración Dinámica según Símbolo
        is_pepe = "PEPE" in symbol
        self.tick_size = 0.0000001 if is_pepe else 0.01
        self.step_size = 1 if is_pepe else 0.001
        self.participation_rate = 0.02 if is_pepe else 0.10
        
        # Configuración Simulada (Debería coincidir con main_v90.py)
        self.config = {
            "symbol": symbol,
            "investment_pct": 0.05,
            "leverage": 15,
            "cpr_width_threshold": 0.2,
            "volume_factor": 1.5,
            "strict_volume_factor": 3.0,
            "take_profit_levels": 3,
            "breakout_atr_sl_multiplier": 1.0,
            "breakout_tp_mult": 2,
            "indicator_update_interval_minutes": 3,
            "ranging_atr_multiplier": 0.5,
            "range_tp_mult": 2.0,
            "daily_loss_limit_pct": 15.0,
            "min_volatility_atr_pct": 0.3,
            "trailing_stop_trigger_atr": 1.25,
            "trailing_stop_distance_atr": 1.0,
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

    # ... [MÉTODOS stage_order, update_sl, move_sl_to_be, execute_exit, close_position, execute_pending_order, check_exits, _process_partial_tp SON IGUALES A V18] ...
    # (Para ahorrar espacio aquí, asume que son idénticos a tu versión anterior. 
    #  Asegúrate de copiarlos o dejarlos si editas el archivo existente).
    
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
        
        if hit_sl and tps_hit: # Conflict
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
        if tp_idx == 0: self.move_sl_to_be()

    # --- SMART LOADER ---
    def load_data(self):
        # 1. Si el usuario dio un archivo específico, usarlo
        if self.custom_file:
            print(f"📂 Usando archivo personalizado: {self.custom_file}")
            possible_files = [self.custom_file]
        else:
            # 2. Búsqueda inteligente: Buscar archivo 2020-2021 o normal
            print(f"🔍 Buscando datos para {self.symbol}...")
            # Prioridad: Archivo específico de crash si la fecha coincide, sino el normal
            file_crash = f"mainnet_data_{TIMEFRAME}_{self.symbol}_2020-2021.csv"
            file_normal = f"mainnet_data_{TIMEFRAME}_{self.symbol}.csv"
            
            # Buscar en carpetas típicas
            search_paths = ["data", "cpr_bot_v90/data", "."]
            possible_files = []
            
            for folder in search_paths:
                f_crash = os.path.join(folder, file_crash)
                f_norm = os.path.join(folder, file_normal)
                
                # Si pedimos fecha vieja (2020), priorizar el archivo del crash
                if "2020" in self.start_date or "2021" in self.start_date:
                    if os.path.exists(f_crash): possible_files.append(f_crash)
                
                # Siempre añadir el normal como backup
                if os.path.exists(f_norm): possible_files.append(f_norm)

        if not possible_files:
            print(f"❌ No se encontraron datos CSV para {self.symbol}")
            return None, None

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
            
            # Verificar si tenemos datos
            if df.index[-1] < target_start:
                print(f"❌ El archivo termina en {df.index[-1]}, antes de tu fecha de inicio {target_start}")
                return None, None

            df = df[df.index >= start_buffer].copy()
            
            # Indicadores
            df['median_vol'] = df['quote_asset_volume'].rolling(60).median().shift(1)
            df['ema'] = df['close'].ewm(span=20).mean().shift(1)
            tr = pd.concat([
                df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()
            ], axis=1).max(axis=1)
            df['atr'] = tr.rolling(14).mean().shift(1)
            #ADX
            adx_period = 14
            df['up_move'] = df['high'] - df['high'].shift(1)
            df['down_move'] = df['low'].shift(1) - df['low']
            
            df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
            df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
            
            df['tr'] = df['atr'] # Ya calculado antes (aproximado)
            
            # Suavizado (EWM alpha=1/period es similar a Wilder)
            df['tr_smooth'] = df['tr'].ewm(alpha=1/adx_period, adjust=False).mean()
            df['plus_dm_smooth'] = df['plus_dm'].ewm(alpha=1/adx_period, adjust=False).mean()
            df['minus_dm_smooth'] = df['minus_dm'].ewm(alpha=1/adx_period, adjust=False).mean()
            
            df['di_plus'] = 100 * (df['plus_dm_smooth'] / df['tr_smooth'])
            df['di_minus'] = 100 * (df['minus_dm_smooth'] / df['tr_smooth'])
            
            df['dx'] = 100 * abs(df['di_plus'] - df['di_minus']) / (df['di_plus'] + df['di_minus'])
            df['adx'] = df['dx'].ewm(alpha=1/adx_period, adjust=False).mean()
            
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
        
        print(f"\n🛡️ INICIANDO BACKTEST HÍBRIDO")
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
                ts_check_price = row.high if self.state.current_position_info['side'] == SIDE_BUY else row.low
                await self.risk_manager._check_trailing_stop(ts_check_price, self.state.current_position_info['quantity'])
                self.check_exits(row)

            self.state.cached_atr = row.atr
            self.state.cached_ema = row.ema
            self.state.cached_median_vol = row.median_vol
            self.state.cached_adx = row.adx

            if not self.state.is_in_position and not self.state.pending_order:
                kline = {'o': row.open, 'c': row.close, 'h': row.high, 'l': row.low, 'v': row.volume, 'q': row.quote_asset_volume, 'x': True}
                await self.risk_manager.seek_new_trade(kline)

        self.generate_report()

    def generate_report(self):
        trades = self.state.trades_history
        if not trades:
            print("⚠️ Sin operaciones en el periodo seleccionado.")
            return

        df_t = pd.DataFrame(trades)
        
        # --- CÁLCULOS PROFESIONALES (Restaurados) ---
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
        
        # Slippage Promedio
        avg_slippage = df_t['slippage_pct'].mean()
        
        # --- EXPORTACIÓN ---
        csv_filename = f"trades_{self.symbol}_{self.start_date}.csv"
        df_t.to_csv(csv_filename, index=False)
        
        # --- IMPRESIÓN DEL REPORTE DETALLADO ---
        print("\n" + "="*60)
        print(f"📊 REPORTE PROFESIONAL (V19) - {self.symbol}")
        print("="*60)
        # Aquí recuperamos la visualización de la configuración usada
        print(f"⚙️  CONFIGURACIÓN:")
        print(f"   • Leverage:        x{self.config['leverage']}")
        print(f"   • Vol Factor:      {self.config['volume_factor']} (Rango)")
        print(f"   • Strict Factor:   {self.config['strict_volume_factor']} (Breakout/Tendencia)")
        print(f"   • TP Multiplier:   {self.config['breakout_tp_mult']}x ATR")
        print(f"   • Trailing Stop:   Trigger {self.config['trailing_stop_trigger_atr']} / Dist {self.config['trailing_stop_distance_atr']}")
        print(f"   • Liquidez (Sim):  {self.participation_rate*100}% del Vol. Vela")
        print("-" * 60)
        print(f"💰 Balance Inicial:   ${CAPITAL_INICIAL:,.2f}")
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
        print(f"💧 Slippage Prom:     {avg_slippage:.4f}%")
        print(f"💾 CSV Detallado:     {csv_filename}")
        print("=" * 60)
        
        # --- GRÁFICOS (Restaurados) ---
        try:
            output_folder = "backtest_results"
            if not os.path.exists(output_folder):
                os.makedirs(output_folder)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{output_folder}/bt_{self.symbol}_{self.start_date}_{timestamp}.png"

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [3, 1]})
            
            # Curva de Equity
            ax1.plot(pd.to_datetime(df_t['date']), df_t['balance'], label='Equity', color='#00ff00', linewidth=1)
            ax1.set_title(f"{self.symbol} | PF: {profit_factor:.2f} | DD: {max_dd:.2f}% | Net: ${net_pnl:,.0f}")
            ax1.set_ylabel('Capital USDT (Log)')
            ax1.set_yscale('log')
            ax1.grid(True, alpha=0.2)
            ax1.legend()
            
            # Drawdown
            ax2.plot(drawdown.values, color='#ff0000', linewidth=1)
            ax2.set_title("Drawdown %")
            ax2.fill_between(range(len(drawdown)), drawdown, 0, color='#ff0000', alpha=0.3)
            ax2.grid(True, alpha=0.2)
            
            plt.tight_layout()
            plt.savefig(filename)
            print(f"📈 Gráfico guardado: {filename}")
            plt.close(fig)
        except Exception as e:
            print(f"⚠️ No se pudo generar el gráfico: {e}")

# ==========================================
# 3. ENTRY POINT CLI
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CPR Bot Backtester Híbrido")
    parser.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL, help="Par a operar (ej: BTCUSDT)")
    parser.add_argument("--start", type=str, default=DEFAULT_START_DATE, help="Fecha inicio YYYY-MM-DD")
    parser.add_argument("--file", type=str, default=None, help="Archivo CSV específico (opcional)")
    
    args = parser.parse_args()
    
    try:
        bt = BacktesterV19(symbol=args.symbol, start_date=args.start, custom_file=args.file)
        asyncio.run(bt.run())
    except KeyboardInterrupt:
        print("\n🛑 Interrumpido.")
    except Exception as e:
        print(f"\n❌ Error: {e}")