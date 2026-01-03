import time
import sys
import os
from datetime import datetime
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.align import Align
from rich.text import Text

# Importar módulos propios
import config
from shared.ccxt_handler import BinanceHandler
# (Opcional) Importar lógica para leer estado de BTC si quieres verlo aquí
# Para simplificar, instanciamos el handler directo

console = Console()
exchange = BinanceHandler()

def generate_header(balance):
    """Genera el encabezado con el balance total"""
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right", ratio=1)
    
    title = Text("🐉 HYDRA BOT DASHBOARD", style="bold magenta", justify="left")
    bal_text = Text(f"💰 Balance: ${balance:.2f} USDT", style="bold green", justify="right")
    
    grid.add_row(title, bal_text)
    return Panel(grid, style="white on blue")

def generate_positions_table(positions):
    """Genera la tabla de posiciones abiertas"""
    table = Table(title="🔓 POSICIONES ABIERTAS", expand=True, border_style="cyan")
    table.add_column("Symbol", style="bold yellow")
    table.add_column("Side", justify="center")
    table.add_column("Entry", justify="right")
    table.add_column("Current", justify="right") # Esto requeriría precio actual, omitimos para rapidez o hacemos fetch ticker
    table.add_column("Size (Coins)", justify="right")
    table.add_column("PnL (U$)", justify="right", style="bold")

    if not positions:
        return Panel(Align.center("[yellow]No hay posiciones activas (Bot esperando)[/yellow]"), title="🔓 POSICIONES", border_style="cyan")

    for pos in positions:
        # Calcular color del PnL
        pnl = float(pos['pnl'])
        color = "green" if pnl >= 0 else "red"
        
        table.add_row(
            pos['symbol'],
            pos['side'].upper(),
            f"{float(pos['entry_price']):.5f}",
            "---", # Para mostrar precio actual real necesitaríamos llamar a la API x cada moneda, puede ser lento
            f"{float(pos['amount']):.0f}",
            f"[{color}]${pnl:.2f}[/{color}]"
        )
    return table

def generate_market_status():
    """Muestra el estado de BTC y alertas (Simulado leyendo config/estado)"""
    # Aquí podrías leer un archivo de estado compartido o checkear BTC
    # Por ahora mostramos la configuración activa
    
    text = Text()
    text.append("📊 CONFIGURACIÓN ACTIVA:\n", style="bold underline")
    text.append(f"• Timeframe: {config.TIMEFRAME}\n")
    text.append(f"• Risk Tier S: {config.RISK_CONFIG['TIER_S']*100}%\n")
    text.append(f"• Risk Tier A: {config.RISK_CONFIG['TIER_A']*100}%\n")
    text.append(f"• Max Slots: {config.RISK_CONFIG['MAX_OPEN_POSITIONS']}\n")
    text.append("\n✅ Sistema Operativo. Presiona Ctrl+C para salir.")
    
    return Panel(text, title="⚙️ ESTADO", border_style="white")

def update_layout(layout):
    """Actualiza los datos de la pantalla"""
    try:
        balance = exchange.get_balance()
        positions = exchange.get_open_positions()
    except Exception as e:
        balance = 0
        positions = []
    
    layout["header"].update(generate_header(balance))
    layout["body"].update(generate_positions_table(positions))
    layout["footer"].update(generate_market_status())

def run_dashboard():
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=8)
    )

    with Live(layout, refresh_per_second=0.5, screen=True):
        while True:
            update_layout(layout)
            time.sleep(10) # Actualizar cada 10s para no saturar API

if __name__ == "__main__":
    try:
        run_dashboard()
    except KeyboardInterrupt:
        print("\n👋 Dashboard cerrado.")