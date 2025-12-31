# config.py

# --- CREDENCIALES BINANCE (FUTURES) ---
API_KEY = "TZnUuGMU7MfQ90jSBeMDM0n4PquHsCuCZYIHEq5wyFarux13Y0XcQrxMZzfo0kGq"
API_SECRET = "bG5d8YfeMZRGXEA9R0YjmUk5i4TDgR2lYwNy0uMXv04iNtntyUO6sNuH7QajP8xG"
# --- CREDENCIALES TELEGRAM ---
TELEGRAM_TOKEN = "7428291479:AAF9SMtvzkaS7m2K7FvSHwfBUC97wGj29D8"
TELEGRAM_CHAT_ID = "843556559"

# --- CONFIGURACIÓN DE TRADING ---
PAIRS = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT', 'AVAX/USDT',  'LTC/USDT']
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

# --- SISTEMA ---
DRY_RUN = False          # True = Dinero ficticio (Logs), False = Dinero real
LOG_FILE = "trading_log.txt"