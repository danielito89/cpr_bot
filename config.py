# config.py

# ==============================================================================
# CONFIGURACIÓN MAESTRA HYDRA HYBRID (VERSION GOLD 443% ROI)
# ==============================================================================

# --- 1. ARQUITECTURA DE PARES ---

# A) DIVISIÓN RÁPIDA "FAST" (1H) - Volatilidad Pura
PAIRS_FAST = [
    '1000PEPE/USDT', # El Alpha (ROI 72%)
    'FET/USDT',      # El Francotirador (ROI 34%)
    'WIF/USDT',      # El Volátil (ROI 27%)
    'DOGE/USDT'      # El Veterano (ROI 16% - Opcional)
]

# B) DIVISIÓN LENTA "SLOW" (4H) - Estructura & Tendencia
PAIRS_SLOW = [
    'SOL/USDT',      # El Rey (ROI 193%)
    'BTC/USDT'       # El Tanque (ROI 98%)
]

# Lista Maestra
PAIRS = PAIRS_FAST + PAIRS_SLOW

# --- 2. GESTIÓN DE CAPITAL ---
TIMEFRAME_BREAKOUT = '4h'  # Default fallback
LEVERAGE = 10              
RISK_PER_TRADE = 0.03      # 3% de la cuenta por operación
MAX_DRAWDOWN_SESSION = 0.10 

# --- 3. PERFILES DE RIESGO OPTIMIZADOS (GOLD SETTINGS) ---

RISK_PROFILES_BREAKOUT = {
    # --- DIVISIÓN FAST (1H) ---
    '1000PEPE/USDT': {
        'sl_atr': 2.5, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.9
    },
    'FET/USDT': {
        'sl_atr': 2.0, 'tp_partial_atr': 6.0, 'trailing_dist_atr': 3.0, 'vol_multiplier': 2.0
    },
    'WIF/USDT': {
        'sl_atr': 2.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 3.5, 'vol_multiplier': 1.6
    },
    'DOGE/USDT': {
        'sl_atr': 2.0, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.9
    },

    # --- DIVISIÓN SLOW (4H) ---
    'SOL/USDT': {
        'sl_atr': 1.5, 'tp_partial_atr': 4.0, 'trailing_dist_atr': 2.5, 'vol_multiplier': 1.5
    },
    'BTC/USDT': {
        'sl_atr': 1.5,          # Stop amplio
        'tp_partial_atr': 2.0,  # TP corto asegurado
        'trailing_dist_atr': 1.5, 
        'vol_multiplier': 1.1   # Casi sin filtro (Unlocked)
    },

    # --- DEFAULT (Seguridad) ---
    'DEFAULT': {
        'sl_atr': 1.5, 'tp_partial_atr': 3.0, 'trailing_dist_atr': 2.0, 'vol_multiplier': 1.5
    }
}

# --- 4. SISTEMA ---
DRY_RUN = False          # False = Dinero Real
LOG_FILE = "trading_log.txt"

# Legacy (Dejar vacío)
PAIRS_SCALPER = []
PROFILES = {}
MAX_OPEN_POSITIONS = 4