"""
CONFIGURACIÓN MAESTRA - ESTRATEGIA HYDRA (BREAKOUT)
Define los pares, riesgos y parámetros operativos.
"""

# --- GESTIÓN DE RIESGO GLOBAL ---
# Risk per Trade según Tier (Validado en Backtest)
RISK_CONFIG = {
    'TIER_S': 0.03,  # 3% Riesgo (Motores de ganancia)
    'TIER_A': 0.02,  # 2% Riesgo (Acompañantes)
    'MAX_OPEN_POSITIONS': 3,
    'MAX_DAILY_DRAWDOWN': 0.06 # 6% (Si perdemos esto en un día, el bot se duerme)
}

# --- PORTFOLIO "WINNER'S CIRCLE" ---
# Nota: Usamos símbolos de Futuros (1000FLOKI, etc.)
PAIRS_CONFIG = {
    # --- TIER S (Los que pagan la fiesta) ---
    '1000FLOKI/USDT': {
        'tier': 'TIER_S',
        'leverage': 5,       # Apalancamiento conservador
        'trail_atr': 4.5,    # Aire para memes
        'tp_atr': 5.5
    },
    'WIF/USDT': {
        'tier': 'TIER_S',
        'leverage': 5,
        'trail_atr': 4.5,
        'tp_atr': 5.5
    },
    'NEAR/USDT': {
        'tier': 'TIER_S',
        'leverage': 5,
        'trail_atr': 4.0,    # Estándar para L1
        'tp_atr': 5.0
    },
    'INJ/USDT': {
        'tier': 'TIER_S',
        'leverage': 5,
        'trail_atr': 4.0,
        'tp_atr': 5.0
    },

    # --- TIER A (Apoyo) ---
    '1000BONK/USDT': {
        'tier': 'TIER_A',
        'leverage': 5,
        'trail_atr': 4.5,
        'tp_atr': 5.0
    },
    'JUP/USDT': {
        'tier': 'TIER_A',
        'leverage': 5,
        'trail_atr': 4.0,
        'tp_atr': 5.0
    }
}

# --- FILTROS GLOBALES ---
TIMEFRAME = '4h'
BTC_SYMBOL = 'BTC/USDT'  # Para el filtro de régimen Macro
SCORE_THRESHOLD = 30     # Score mínimo de calidad (ADX + Expansion)
COOLDOWN_CANDLES = 12    # 48hs de espera tras salida
LEVERAGE = 5
DRY_RUN = False