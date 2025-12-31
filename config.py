# config.py

# ==========================================
# ⚠️ LAS API KEYS ESTÁN EN EL ARCHIVO .ENV
# ==========================================

# --- 1. LISTAS DE PARES (DIVIDIDAS) ---

# A) PARES PARA SCALPER (Hydra Mean Reversion)
# Monedas que se mueven en rangos o son muy pesadas
PAIRS_SCALPER = [
    'BTC/USDT', 
    'ETH/USDT', 
    'SOL/USDT'
]

# B) PARES PARA BREAKOUT (Nuevo Bot)
# Monedas explosivas
PAIRS_BREAKOUT = [
    'SOL/USDT', 
    'DOGE/USDT', 
    'FET/USDT',  # Nueva estrella IA
    'WIF/USDT'   # Meme Sniper
]

# (Mantenemos compatibilidad hacia atrás por si acaso)
PAIRS = PAIRS_SCALPER 

# --- CONFIGURACIÓN DE TRADING ---
TIMEFRAME = '5m' # Timeframe del Scalper
TIMEFRAME_BREAKOUT = '4h'
LEVERAGE = 10           
RISK_PER_TRADE = 0.03   
MAX_DRAWDOWN_SESSION = 0.10 

# --- FILTROS DE ESTRATEGIA (V6.4) ---
RSI_LONG_THRESHOLD = 45
RSI_SHORT_THRESHOLD = 55
ATR_PERCENTILE = 0.25
VOLUME_MA_PERIOD = 20
TP1_RATIO = 1.0
TP2_RATIO = 3.0

# --- PERFILES DE RIESGO (Breakout) ---
# Valores validados por tu Backtest de Alta Fidelidad
RISK_PROFILES_BREAKOUT = {
    'SOL/USDT': {
        'sl_atr': 1.5, 
        'tp_partial_atr': 4.0, 
        'trailing_dist_atr': 2.5, 
        'vol_multiplier': 1.5
    },
    'DOGE/USDT': {
        'sl_atr': 1.0, 
        'tp_partial_atr': 3.0, 
        'trailing_dist_atr': 2.0, 
        'vol_multiplier': 1.5
    },
    'FET/USDT': {  # Configuración IA
        'sl_atr': 1.5, 
        'tp_partial_atr': 5.0, # Dejar correr mucho
        'trailing_dist_atr': 3.0, 
        'vol_multiplier': 1.5
    },
    'WIF/USDT': {  # Configuración Meme Volátil
        'sl_atr': 2.5, # Mucho aire
        'tp_partial_atr': 6.0, 
        'trailing_dist_atr': 3.5, 
        'vol_multiplier': 1.8 # Solo volumen muy alto
    },
    'DEFAULT': { # Por seguridad
        'sl_atr': 1.5, 'tp_partial_atr': 3.0, 'trailing_dist_atr': 2.0, 'vol_multiplier': 1.5
    }
}

# --- PERFILES DE ACTIVOS ---
ASSET_MAP = {
    'BTC/USDT': 'SNIPER',
    'ETH/USDT': 'SNIPER',
    'SOL/USDT': 'FLOW'
}

PROFILES = {
    'SNIPER': { 'risk_type': 'standard', 'tp_multiplier': 1.0 },
    'FLOW': { 'risk_type': 'aggressive', 'tp_multiplier': 1.5 }
}

# --- SISTEMA ---
DRY_RUN = False
LOG_FILE = "trading_log.txt"