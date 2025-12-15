import ccxt
import os
from dotenv import load_dotenv

# Configuración
SYMBOLS_TO_CHECK = ['BTC/USDT', 'ETH/USDT']
MY_CONFIGURED_SIZES = {
    'BTC/USDT': 0.002,  # Pon aquí lo que pusiste en carry_bot.py
    'ETH/USDT': 0.02
}

def check_everything():
    load_dotenv()
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_SECRET_KEY'),
        'options': {'defaultType': 'future'}
    })
    
    print("\n✈️  INICIANDO PRE-FLIGHT CHECK...\n")

    # --- CHECK 1: POSITION MODE ---
    # En Binance Futures API: 'dualSidePosition': True significa Hedge Mode
    try:
        # A veces ccxt lo guarda en exchange.properties, pero mejor consultar directo
        response = exchange.fapiPrivate_get_positionsidedual() 
        is_hedge_mode = response['dualSidePosition'] 
        
        print(f"1️⃣  MODO DE POSICIÓN:")
        if is_hedge_mode:
            print("   ❌ PELIGRO: Estás en HEDGE MODE.")
            print("   👉 Debes cambiarlo a One-Way Mode en la App o UI de Binance.")
        else:
            print("   ✅ CORRECTO: Estás en ONE-WAY Mode.")
    except Exception as e:
        print(f"   ⚠️ Error chequeando modo: {e}")

    print("-" * 40)

    # --- CHECK 2: MARKET LIMITS ---
    print(f"2️⃣  LÍMITES DE MERCADO:")
    exchange.load_markets()
    
    all_good = True
    
    for symbol in SYMBOLS_TO_CHECK:
        market = exchange.market(symbol)
        limits = market['limits']
        min_amount = limits['amount']['min']
        min_cost = limits['cost']['min'] # Valor nocional mínimo (ej. 5 USDT)
        
        my_size = MY_CONFIGURED_SIZES[symbol]
        price = exchange.fetch_ticker(symbol)['last']
        my_notional = my_size * price
        
        print(f"\n   🔍 Analizando {symbol}:")
        print(f"      Tu orden: {my_size} (Valor aprox: ${my_notional:.2f})")
        print(f"      Min Amount Exchange: {min_amount}")
        print(f"      Min Cost Exchange:   ${min_cost}")
        
        if my_size < min_amount:
            print("      ❌ ERROR: Tu tamaño es menor al mínimo de cantidad.")
            all_good = False
        elif my_notional < min_cost:
            print("      ❌ ERROR: Tu valor nocional es menor al mínimo (generalmente $5 USD).")
            all_good = False
        else:
            print("      ✅ TAMAÑO VÁLIDO.")

    print("\n" + "="*40)
    if all_good and not is_hedge_mode:
        print("🚀  STATUS: GO FOR LAUNCH")
    else:
        print("🛑  STATUS: NO GO (Corrige los errores)")

if __name__ == "__main__":
    check_everything()