# config.py

# ==========================================
# ⚠️ NOTA DE SEGURIDAD:
# Las API KEYS y TOKENS se han movido al archivo .env
# No escribir credenciales reales aquí.
# ==========================================

# --- CONFIGURACIÓN DE TRADING ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAME = '5m'
LEVERAGE = 10           # Apalancamiento máximo permitido
RISK_PER_TRADE = 0.03   # 3% de riesgo por operación (Aggressive)
MAX_DRAWDOWN_SESSION = 0.10 # Si perdemos 10% en el día, el bot se apaga

# --- FILTROS DE ESTRATEGIA (V6.4) ---
RSI_LONG_THRESHOLD = 45
RSI_SHORT_THRESHOLD = 55
ATR_PERCENTILE = 0.25
VOLUME_MA_PERIOD = 20
TP1_RATIO = 1.0
TP2_RATIO = 3.0

# --- PERFILES DE ACTIVOS (Para Hydra V6.5) ---
# Aquí definimos si un par se opera con perfil Sniper o Flow
ASSET_MAP = {
    'BTC/USDT': 'SNIPER',
    'ETH/USDT': 'SNIPER',
    'SOL/USDT': 'FLOW'    # Ejemplo: SOL es más volátil
}

PROFILES = {
    'SNIPER': {
        'risk_type': 'standard',
        'tp_multiplier': 1.0
    },
    'FLOW': {
        'risk_type': 'aggressive',
        'tp_multiplier': 1.5
    }
}

# --- SISTEMA ---
DRY_RUN = False          # True = Dinero ficticio (Logs), False = Dinero real
LOG_FILE = "trading_log.txt"