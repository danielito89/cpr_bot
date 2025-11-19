#!/usr/bin/env python3
# backtester_v3.py
# Versión: v3.4 Final (Contabilidad corregida + Estrategia Ganadora)

import os
import sys
import pandas as pd
import numpy as np
import asyncio
import logging
import time
import gc
from datetime import datetime, timedelta

# Configurar logger simple para la consola
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger()

# --- 1. CONFIGURACIÓN DEL BACKTESTER ---
SYMBOL_TO_TEST = "ETHUSDT"  # Cambia a BTCUSDT para probar el otro
START_BALANCE = 10000

# Parámetros de Riesgo (Tu perfil agresivo)
LEVERAGE = 30
INVESTMENT_PCT = 0.05       # 5% del capital por trade
COMMISSION_PCT = 0.0004     # 0.04% (Taker)
DAILY_LOSS_LIMIT_PCT = 15.0 # 15% Límite diario

# Parámetros de Estrategia (Los Ganadores)
EMA_PERIOD = 20
ATR_PERIOD = 14
VOLUME_FACTOR = 1.3
CPR_WIDTH_THRESHOLD = 0.2
TIME_STOP_HOURS = 12        # El factor clave
BREAKOUT_ATR_SL_MULT = 1.0
BREAKOUT_TP_MULT = 1.25
RANGING_ATR_MULTIPLIER = 0.5
RANGE_TP_MULT = 2.0

# Directorio de datos (Sube un nivel desde cpr_bot_v90 a cpr_bot/data)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# --- IMPORTAR LÓGICA REAL ---
try:
    # Añadir el directorio actual al path para poder importar bot_core
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from bot_core.risk import RiskManager
    from bot_core.pivots import calculate_pivots_from_data
    from bot_core.utils import format_price, format_qty, SIDE_BUY, SIDE_SELL
except ImportError as e:
    print(f"Error importando módulos del bot: {e}")
    print("Asegúrate de ejecutar esto desde la carpeta cpr_bot_v90/")
    sys.exit(1)

# --- MOCKS (IMITADORES) ---

class MockTelegram:
    async def _send_message(self, text):
        pass 

class MockOrdersManager:
    def __init__(self, simulator):
        self.sim = simulator

    async def place_bracket_order(self, side, qty, price, sl, tps, type):
        # Interceptamos la orden y se la pasamos al simulador
        self.sim.open_position(side, qty, price, sl, tps, type)

    async def move_sl_to_be(self, qty):
        self.sim.move_sl_to_be()

    async def close_position_manual(self, reason):
        self.sim.close_position(reason)

class MockBotController:
    """Simula ser el Orquestador Principal (BotController)."""
    def __init__(self, simulator, symbol):
        self.symbol = symbol
        self.client = None 
        self.telegram_handler = MockTelegram()
        self.orders_manager = MockOrdersManager(simulator)
        self.state = simulator.state 
        self.lock = asyncio.Lock()
        
        # Inyectar configuración como atributos del bot
        self.investment_pct = INVESTMENT_PCT
        self.leverage = LEVERAGE
        self.cpr_width_threshold = CPR_WIDTH_THRESHOLD
        self.volume_factor = VOLUME_FACTOR
        self.take_profit_levels = 3
        self.breakout_atr_sl_multiplier = BREAKOUT_ATR_SL_MULT
        self.breakout_tp_mult = BREAKOUT_TP_MULT
        self.ranging_atr_multiplier = RANGING_ATR_MULTIPLIER
        self.range_tp_mult = RANGE_TP_MULT
        self.daily_loss_limit_pct = DAILY_LOSS_LIMIT_PCT
        
        # Reglas de Exchange Simuladas
        self.tick_size = 0.01
        self.step_size = 0.001

    async def _get_account_balance(self):
        return self.state.balance

    async def _get_current_position(self):
        if not self.state.is_in_position: return None
        info = self.state.current_position_info
        amt = info['quantity'] if info['side'] == SIDE_BUY else -info['quantity']
        return {
            "positionAmt": amt,
            "entryPrice": info['entry_price'],
            "markPrice": self.state.current_price, 
            "unRealizedProfit": 0.0 
        }

# --- SIMULADOR ---

class SimulatorState:
    """Mantiene el estado del bot y del simulador."""
    def __init__(self):
        # Estado del Bot
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
        
        # Estado del Simulador
        self.balance = START_BALANCE
        self.current_price = 0.0
        self.current_time = None
        self.trades_history = []

class BacktesterV3:
    def __init__(self):
        self.state = SimulatorState()
        self.controller = MockBotController(self, SYMBOL_TO_TEST)
        self.risk_manager = RiskManager(self.controller)

    def open_position(self, side, qty, price, sl, tps, type_):
        notional = qty * price
        comm = notional * COMMISSION_PCT
        self.state.balance -= comm # Restamos comisión de entrada
        
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": qty, "entry_price": price,
            "entry_type": type_, "tps_hit_count": 0,
            "total_pnl": -comm, 
            "sl": sl, "tps": tps, "entry_time": self.state.current_time,
            "comm_entry": comm # Guardamos comisión para el reporte final
        }
        self.state.last_known_position_qty = qty
        self.state.sl_moved_to_be = False

    def move_sl_to_be(self):
        if self.state.is_in_position:
            self.state.current_position_info['sl'] = self.state.current_position_info['entry_price']
            self.state.sl_moved_to_be = True

    def close_position(self, reason):
        if not self.state.is_in_position: return
        
        info = self.state.current_position_info
        exit_price = self.state.current_price
        
        # PnL Bruto del movimiento
        pnl_gross = (exit_price - info['entry_price']) * info['quantity']
        if info['side'] == SIDE_SELL: pnl_gross = -pnl_gross
        
        # Comisión de Salida
        comm_exit = (exit_price * info['quantity']) * COMMISSION_PCT
        
        # Comisión de Entrada (Recuperada)
        comm_entry = info.get('comm_entry', 0.0)
        
        # PnL Neto Real
        net_pnl = pnl_gross - comm_exit - comm_entry
        
        # Actualizar Balance (Sumamos el resultado del trade - comisión salida)
        # (La comisión de entrada ya se restó al abrir)
        self.state.balance += (pnl_gross - comm_exit)
        
        self.state.trades_history.append({
            'entry_time': info['entry_time'],
            'exit_time': self.state.current_time,
            'side': info['side'],
            'type': info['entry_type'],
            'pnl': net_pnl, # PnL Neto reportado
            'reason': reason
        })
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.daily_trade_stats.append({'pnl': net_pnl})

    def check_exits(self, row):
        if not self.state.is_in_position: return
        
        info = self.state.current_position_info
        high, low = row.High, row.Low
        
        # 1. Stop Loss
        sl_hit = (info['side'] == SIDE_BUY and low <= info['sl']) or \
                 (info['side'] == SIDE_SELL and high >= info['sl'])
        
        if sl_hit:
            self.state.current_price = info['sl'] # Asumimos fill en SL
            self.close_position("Stop-Loss")
            return

        # 2. Take Profits (Simplificado: Toca TP2->BE, Toca TP3->Cierre)
        tps = info['tps']
        
        # Check TP2 para BE
        if len(tps) >= 2:
            tp2 = tps[1]
            hit_tp2 = (info['side'] == SIDE_BUY and high >= tp2) or \
                      (info['side'] == SIDE_SELL and low <= tp2)
            if hit_tp2 and info['tps_hit_count'] < 2:
                info['tps_hit_count'] = 2
                self.move_sl_to_be()

        # Check TP Final (Último TP)
        if tps:
            last_tp = tps[-1]
            hit_tp = (info['side'] == SIDE_BUY and high >= last_tp) or \
                     (info['side'] == SIDE_SELL and low <= last_tp)
            
            if hit_tp:
                self.state.current_price = last_tp
                self.close_position("Take-Profit Final")

    async def run(self):
        print(f"Iniciando Backtest Realista (v3.4) para {SYMBOL_TO_TEST}...")
        
        # 1. Cargar Datos
        file_1h = f"mainnet_data_1h_{SYMBOL_TO_TEST}.csv"
        file_1d = f"mainnet_data_1d_{SYMBOL_TO_TEST}.csv"
        file_1m = f"mainnet_data_1m_{SYMBOL_TO_TEST}.csv"
        
        print("Cargando datos...")
        df_1h = pd.read_csv(os.path.join(DATA_DIR, file_1h), index_col="Open_Time", parse_dates=True)
        df_1d = pd.read_csv(os.path.join(DATA_DIR, file_1d), index_col="Open_Time", parse_dates=True)
        df_1m = pd.read_csv(os.path.join(DATA_DIR, file_1m), index_col="Open_Time", parse_dates=True)

        # Indicadores 1H
        df_1h['EMA'] = df_1h['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        tr1 = df_1h['High'] - df_1h['Low']
        tr2 = abs(df_1h['High'] - df_1h['Close'].shift(1))
        tr3 = abs(df_1h['Low'] - df_1h['Close'].shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df_1h['ATR'] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
        
        # Indicadores 1M
        print("Calculando Mediana...")
        df_1m['MedianVol'] = df_1m['Quote_Asset_Volume'].rolling(window=60).median().shift(1)
        
        print("Fusionando...")
        df_merged = pd.merge_asof(df_1m, df_1h[['EMA', 'ATR']], left_index=True, right_index=True, direction='backward')
        df_merged.dropna(inplace=True)
        
        del df_1h, df_1m
        gc.collect()

        print(f"Simulando {len(df_merged)} velas...")
        
        for row in df_merged.itertuples():
            self.state.current_time = row.Index
            self.state.current_price = row.Close
            
            # 1. Actualizar Pivotes
            current_date = row.Index.date()
            if self.state.last_pivots_date != current_date:
                yesterday_ts = pd.Timestamp(current_date - timedelta(days=1))
                if yesterday_ts in df_1d.index:
                    d_row = df_1d.loc[yesterday_ts]
                    h, l, c = float(d_row['High']), float(d_row['Low']), float(d_row['Close'])
                    self.state.daily_pivots = calculate_pivots_from_data(h, l, c, 0.01, 0.2)
                    self.state.last_pivots_date = current_date
            
            self.state.cached_atr = row.ATR
            self.state.cached_ema = row.EMA
            self.state.cached_median_vol = row.MedianVol
            
            # 2. Lógica de Trading
            if self.state.is_in_position:
                # Time Stop check (usando lógica del RiskManager)
                if self.state.current_position_info['entry_type'].startswith("Ranging"):
                     hours = (row.Index - self.state.current_position_info['entry_time']).total_seconds() / 3600
                     if hours > TIME_STOP_HOURS:
                         self.close_position(f"Time-Stop ({TIME_STOP_HOURS}h)")
                
                self.check_exits(row)

            if not self.state.is_in_position and self.state.daily_pivots:
                # Crear kline dict para RiskManager
                k = {
                    'o': row.Open, 'c': row.Close, 'h': row.High, 'l': row.Low,
                    'v': row.Volume, 'q': row.Quote_Asset_Volume, 'x': True
                }
                await self.risk_manager.seek_new_trade(k)

        self.print_results()

    def print_results(self):
        trades = self.state.trades_history
        if not trades:
            print("\n--- NO SE REALIZARON TRADES ---")
            return

        df = pd.DataFrame(trades)
        total_pnl = df['pnl'].sum()
        wins = len(df[df['pnl'] > 0])
        win_rate = (wins / len(df)) * 100
        
        gross_profit = df[df['pnl'] > 0]['pnl'].sum()
        gross_loss = abs(df[df['pnl'] < 0]['pnl'].sum())
        pf = gross_profit / gross_loss if gross_loss != 0 else 0

        print("\n" + "="*40)
        print(f" RESULTADOS REALISTAS (v3.4): {SYMBOL_TO_TEST}")
        print("="*40)
        print(f" PnL Neto:      ${total_pnl:.2f}")
        print(f" Balance Final: ${self.state.balance:.2f}")
        print(f" Profit Factor: {pf:.2f}")
        print(f" Win Rate:      {win_rate:.2f}% ({wins}/{len(df)})")
        print(f" Total Trades:  {len(df)}")
        print("="*40)
        df.to_csv(os.path.join(DATA_DIR, f"backtest_v3_{SYMBOL_TO_TEST}.csv"))

if __name__ == "__main__":
    asyncio.run(BacktesterV3().run())
