# config.py

# Configuración General
TIMEFRAME_BREAKOUT = '4h'
TIMEFRAME_SCALPER = '5m'

# PERFILES DE RIESGO (Resultado del Backtest)
# Ajustaremos estos valores tras correr el script de abajo
RISK_PROFILES = {
    'BTC/USDT': {
        'sl_atr': 1.0,
        'tp_partial_atr': 2.5,
        'trailing_dist_atr': 1.5,
        'vol_multiplier': 1.5
    },
    'ETH/USDT': {
        'sl_atr': 1.2,          # ETH necesita más aire
        'tp_partial_atr': 3.0,  # ETH corre más fuerte
        'trailing_dist_atr': 2.0,
        'vol_multiplier': 1.6
    },
    'SOL/USDT': {
        'sl_atr': 1.5,          # Mucha volatilidad
        'tp_partial_atr': 4.0,  # Explosiones masivas
        'trailing_dist_atr': 2.5,
        'vol_multiplier': 1.8   # Filtrar mucho ruido
    }
}