import pandas as pd
import os

DIR = "data" # O donde tengas los csv
SYM = "ETHUSDT"

print(f"Revisando datos para {SYM}...")
try:
    df_1d = pd.read_csv(f"{DIR}/mainnet_data_1d_{SYM}.csv", index_col="Open_Time", parse_dates=True)
    df_1m = pd.read_csv(f"{DIR}/mainnet_data_1m_{SYM}.csv", index_col="Open_Time", parse_dates=True)
    
    print(f"1D Rango: {df_1d.index[0]} a {df_1d.index[-1]}")
    print(f"1M Rango: {df_1m.index[0]} a {df_1m.index[-1]}")
    
    # Chequeo de Pivotes
    sample_date = df_1m.index[1000].date()
    prev_date = sample_date - pd.Timedelta(days=1)
    ts_lookup = pd.Timestamp(prev_date)
    
    print(f"Probando lookup de pivote para {sample_date} (busca {ts_lookup})...")
    if ts_lookup in df_1d.index:
        print("✅ DATOS ALINEADOS: Pivote encontrado.")
    else:
        print("❌ ERROR CRÍTICO: No tengo datos del día anterior para calcular pivotes.")
        print("Esto causa que el bot no opere nunca.")
except Exception as e:
    print(f"Error leyendo archivos: {e}")