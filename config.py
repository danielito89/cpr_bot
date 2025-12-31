# config.py

# --- GENERAL ---
TIMEFRAME_BREAKOUT = '4h'
TIMEFRAME_SCALPER = '5m'
USDT_PER_TRADE = 50.0  # Tamaño fijo para empezar (o lógica dinámica)

# --- PERFILES DE RIESGO (Optimizados Backtest 2022-2024) ---
RISK_PROFILES = {
    'BTC/USDT': {
        'sl_atr': 1.0,
        'tp_partial_atr': 2.5,
        'trailing_dist_atr': 1.5,
        'vol_multiplier': 1.3  # Bajamos un poco para capturar más movimientos en BTC
    },
    'ETH/USDT': {
        'sl_atr': 1.2,
        'tp_partial_atr': 3.0,
        'trailing_dist_atr': 2.0,
        'vol_multiplier': 1.4
    },
    'SOL/USDT': { # La joya de la corona
        'sl_atr': 1.5,          # Mayor espacio para respirar
        'tp_partial_atr': 4.0,  # Buscar Home Runs
        'trailing_dist_atr': 2.5,
        'vol_multiplier': 1.5
    }
}