# config.py

# ==============================================================================
# ⚠️ IMPORTANTE: Las API KEYS (BINANCE_API_KEY, etc.) están en el archivo .env
# No escribir credenciales reales aquí.
# ==============================================================================

# --- 1. ARQUITECTURA DE PARES (Doble Velocidad) ---

# A) DIVISIÓN RÁPIDA "FAST" (Timeframe 1H)
# Monedas de alta volatilidad (Memes, IA) que requieren reacción rápida.
PAIRS_FAST = [
    'DOGE/USDT',
    '1000PEPE/USDT',
    'WIF/USDT',
    'FET/USDT',
    'AVAX/USDT'  # Configurado agresivo, va a 1H
]

# B) DIVISIÓN LENTA "SLOW" (Timeframe 4H)
# Monedas estructurales / L1s sólidas. Buscamos tendencias de días/semanas.
PAIRS_SLOW = [
    'SOL/USDT',
    'BTC/USDT',
    'ETH/USDT'
]

# (Lista Maestra para compatibilidad con Dashboard)
PAIRS = PAIRS_FAST + PAIRS_SLOW

# --- 2. CONFIGURACIÓN GLOBAL DE TRADING ---
# Estos valores aplican si no se sobrescriben en el perfil específico
TIMEFRAME_BREAKOUT = '4h'  # Fallback por defecto
LEVERAGE = 10              # Apalancamiento (Cross o Isolated según tu cuenta)
RISK_PER_TRADE = 0.03      # 3% de riesgo por operación
MAX_DRAWDOWN_SESSION = 0.10 # Stop de emergencia del bot

# --- 3. PERFILES DE RIESGO POR ACTIVO ---
# Definimos el "carácter" de cada moneda.

RISK_PROFILES_BREAKOUT = {
    # --- DIVISIÓN RÁPIDA (1H) ---
    'DOGE/USDT': {
        'sl_atr': 2.0, 
        'tp_partial_atr': 4.0, 
        'trailing_dist_atr': 2.5, 
        'vol_multiplier': 1.8
    },
    'FET/USDT': {
        'sl_atr': 2.0, 
        'tp_partial_atr': 6.0, 
        'trailing_dist_atr': 3.0, 
        'vol_multiplier': 1.7
    },
    'WIF/USDT': {
        'sl_atr': 2.5, 
        'tp_partial_atr': 4.0, 
        'trailing_dist_atr': 3.5, 
        'vol_multiplier': 1.8 
    },
    '1000PEPE/USDT': {
        'sl_atr': 2.5, 
        'tp_partial_atr': 6.0, 
        'trailing_dist_atr': 3.5, 
        'vol_multiplier': 1.8
    },

    # --- DIVISIÓN LENTA (4H) ---
    'SOL/USDT': {
        'sl_atr': 1.5, 
        'tp_partial_atr': 4.0, 
        'trailing_dist_atr': 2.5, 
        'vol_multiplier': 1.5
    },
    # BTC (4H): Es más pesado. Stop más corto, Vol más bajo para poder entrar.
    'BTC/USDT': {
        'sl_atr': 1.0,           # BTC respeta mejor los niveles, no necesita tanto aire
        'tp_partial_atr': 2.5,   # TP más conservador
        'trailing_dist_atr': 1.5, 
        'vol_multiplier': 1.3    # Exigimos solo un 30% extra de volumen (es difícil mover BTC x2)
    },
    # ETH (4H): Híbrido entre BTC y SOL.
    'ETH/USDT': {
        'sl_atr': 1.2, 
        'tp_partial_atr': 3.0, 
        'trailing_dist_atr': 2.0, 
        'vol_multiplier': 1.4
    },

    # --- DEFAULT (Seguridad) ---
    'DEFAULT': {
        'sl_atr': 1.5, 
        'tp_partial_atr': 3.0, 
        'trailing_dist_atr': 2.0, 
        'vol_multiplier': 1.5
    }
}

# --- 4. SISTEMA ---
DRY_RUN = False          # False = Dinero Real
LOG_FILE = "trading_log.txt"

# (Legacy Scalper - Mantenemos esto vacío para no romper imports viejos si quedan)
PAIRS_SCALPER = []
PROFILES = {}