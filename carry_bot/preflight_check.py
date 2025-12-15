import ccxt
import os
from dotenv import load_dotenv

# Configuración
SYMBOLS_TO_CHECK = ['BTC/USDT', 'ETH/USDT']
MY_CONFIGURED_SIZES = {
    'BTC/USDT': 0.002, 
    'ETH/USDT': 0.02
}

def check_everything():
    load_dotenv()
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_SECRET_KEY'),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    print("\n✈️  INICIANDO PRE-FLIGHT CHECK...\n")
    
    # --- PASO CRITICO: CARGAR MERCADOS PRIMERO ---
    print("⏳ Cargando información de mercados de Binance...")
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"❌ Error fatal conectando a Binance: {e}")
        return

    # --- CHECK 1: POSITION MODE ---
    is_hedge_mode = True 
    print(f"\n1️⃣  MODO DE POSICIÓN:")
    
    try:
        # Ahora sí, con los mercados cargados, preguntamos
        response = exchange.fetch_position_mode(symbol='BTC/USDT')
        is_hedge_mode = response['hedged']

        if is_hedge_mode:
            print("   ❌ PELIGRO: Estás en HEDGE MODE.")
            print("   👉 Debes cambiarlo a One-Way Mode en la App de Binance.")
        else:
            print("   ✅ CORRECTO: Estás en ONE-WAY Mode.")
            
    except Exception as e:
        print(f"   ⚠️ Error chequeando modo: {e}")
        is_hedge_mode = True 

    print("-" * 40)

    # --- CHECK 2: MARKET LIMITS ---
    print(f"2️⃣  LÍMITES DE MERCADO:")
    
    all_good = True
    
    for symbol in SYMBOLS_TO_CHECK:
        try:
            market = exchange.market(symbol)
            limits = market['limits']
            min_amount = limits['amount']['min']
            min_cost = limits['cost']['min'] if limits['cost']['min'] else 5.0 
            
            my_size = MY_CONFIGURED_SIZES[symbol]
            ticker = exchange.fetch_ticker(symbol)
            price = ticker['last']
            my_notional = my_size * price
            
            print(f"\n   🔍 Analizando {symbol}:")
            print(f"      Tu orden: {my_size} (Valor aprox: ${my_notional:.2f})")
            print(f"      Min Amount Exchange: {min_amount}")
            print(f"      Min Cost Exchange:   ${min_cost}")
            
            if my_size < min_amount:
                print("      ❌ ERROR: Tu tamaño es menor al mínimo de cantidad.")
                all_good = False
            elif my_notional < min_cost:
                print("      ❌ ERROR: Tu valor nocional es menor al mínimo (~$5 USD).")
                all_good = False
            else:
                print("      ✅ TAMAÑO VÁLIDO.")
        except Exception as e:
            print(f"      ⚠️ Error analizando {symbol}: {e}")
            all_good = False

    print("\n" + "="*40)
    
    if all_good and not is_hedge_mode:
        print("🚀  STATUS: GO FOR LAUNCH (Sistemas listos)")
    else:
        print("🛑  STATUS: NO GO (Revisa los errores arriba)")

if __name__ == "__main__":
    check_everything()