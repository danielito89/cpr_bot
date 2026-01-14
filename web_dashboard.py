import streamlit as st
import pandas as pd
import sqlite3
import json
import os
import time

# Configuración de la página
st.set_page_config(
    page_title="Hydra Bot Dashboard",
    page_icon="🐲",
    layout="wide"
)

# Título y Auto-refresh
st.title("🐲 Hydra Bot Dashboard (Alemania)")
if st.button('🔄 Actualizar Datos'):
    st.rerun()

# 1. ESTADO DEL BOT (JSON)
st.header("🤖 Estado del Bot")
try:
    if os.path.exists('bot_state.json'):
        with open('bot_state.json', 'r') as f:
            state = json.load(f)
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Balance Inicial", f"${state.get('initial_balance', 0)}")
        col1.metric("Balance Actual", f"${state.get('current_balance', 0)}")
        
        # Intentar mostrar posiciones
        positions = state.get('open_positions', [])
        col2.metric("Posiciones Abiertas", len(positions))
        
        if positions:
            st.subheader("Posiciones Activas")
            st.json(positions)
        else:
            st.info("Sin posiciones activas por el momento.")
    else:
        st.warning("⚠️ No se encontró el archivo bot_state.json todavía.")
except Exception as e:
    st.error(f"Error leyendo estado: {e}")

st.markdown("---")

# 2. HISTORIAL DE TRADES (SQLite)
st.header("📚 Historial de Operaciones")

try:
    if os.path.exists('trades_db.sqlite'):
        # Conectar a la base de datos
        conn = sqlite3.connect('trades_db.sqlite')
        
        # Leer trades cerrados
        query = "SELECT * FROM trades ORDER BY close_time DESC LIMIT 50"
        df = pd.read_sql_query(query, conn)
        conn.close()

        if not df.empty:
            # Mostrar métricas rápidas
            total_profit = df['pnl'].sum() if 'pnl' in df.columns else 0
            wins = len(df[df['pnl'] > 0]) if 'pnl' in df.columns else 0
            total_trades = len(df)
            
            m1, m2, m3 = st.columns(3)
            m1.metric("PnL Realizado Total", f"${total_profit:.2f}")
            m2.metric("Trades Totales", total_trades)
            m3.metric("Wins", wins)

            # Mostrar tabla
            st.dataframe(df)
        else:
            st.info("La base de datos existe pero no hay trades cerrados aún.")
    else:
        st.warning("⚠️ No se encontró la base de datos trades_db.sqlite.")

except Exception as e:
    st.error(f"Error leyendo base de datos: {e}")