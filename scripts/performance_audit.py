"""
performance_audit.py – Comprehensive statistical and strategy audit of bot trading history.
"""
import os
import sys
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import config

def run_audit():
    db_path = str(config.DB_FILE_PATH)
    if not os.path.exists(db_path):
        print(f"No DB found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("=================================================================")
    print("           SENIOR QUANT & TRADER PERFORMANCE AUDIT               ")
    print("=================================================================")

    # 1. Trade Performance Analysis
    trades_df = pd.read_sql_query("SELECT * FROM trades", conn)
    print(f"\nTotal Trades Recorded: {len(trades_df)}")

    if not trades_df.empty:
        closed_trades = trades_df[trades_df['status'] == 'CLOSED'].copy()
        open_trades = trades_df[trades_df['status'] == 'OPEN'].copy()

        print(f"Open Trades: {len(open_trades)} | Closed Trades: {len(closed_trades)}")

        if not closed_trades.empty:
            closed_trades['pnl'] = pd.to_numeric(closed_trades['pnl'], errors='coerce').fillna(0)
            closed_trades['entry_price'] = pd.to_numeric(closed_trades['entry_price'], errors='coerce')
            closed_trades['exit_price'] = pd.to_numeric(closed_trades['exit_price'], errors='coerce')
            closed_trades['qty'] = pd.to_numeric(closed_trades['qty'], errors='coerce')
            
            wins = closed_trades[closed_trades['pnl'] > 0]
            losses = closed_trades[closed_trades['pnl'] < 0]
            scratch = closed_trades[closed_trades['pnl'] == 0]

            win_count = len(wins)
            loss_count = len(losses)
            total_closed = len(closed_trades)
            win_rate = (win_count / total_closed) * 100 if total_closed > 0 else 0

            total_gain = wins['pnl'].sum()
            total_loss = abs(losses['pnl'].sum())
            net_pnl = closed_trades['pnl'].sum()
            profit_factor = (total_gain / total_loss) if total_loss > 0 else (np.inf if total_gain > 0 else 0)

            avg_win = wins['pnl'].mean() if win_count > 0 else 0
            avg_loss = abs(losses['pnl'].mean()) if loss_count > 0 else 0
            win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0
            expectancy = (win_rate/100 * avg_win) - ((1 - win_rate/100) * avg_loss)

            print("\n--- CLOSED TRADES SUMMARY ---")
            print(f"Win Rate:          {win_rate:.1f}% ({win_count} wins / {loss_count} losses / {len(scratch)} breakeven)")
            print(f"Net Realized PnL:  ${net_pnl:,.2f}")
            print(f"Total Gains:       ${total_gain:,.2f}")
            print(f"Total Losses:      ${total_loss:,.2f}")
            print(f"Profit Factor:     {profit_factor:.2f}")
            print(f"Average Win:       ${avg_win:,.2f}")
            print(f"Average Loss:      ${avg_loss:,.2f}")
            print(f"Win/Loss Payoff:   {win_loss_ratio:.2f}x")
            print(f"Expectancy/Trade:  ${expectancy:,.2f}")

            print("\n--- ALL CLOSED TRADES BREAKDOWN ---")
            for idx, r in closed_trades.iterrows():
                print(f"  [{r['id']}] {r['symbol']} ({r['side'].upper()} {r['qty']}) | Entry: ${r['entry_price']:.2f} ({r['entry_time']}) -> Exit: ${r['exit_price']:.2f} ({r['exit_time']}) | PnL: ${r['pnl']:,.2f} | Reason: {r['alpaca_exit_order_id']}")

        if not open_trades.empty:
            print("\n--- CURRENT OPEN TRADES ---")
            for idx, r in open_trades.iterrows():
                print(f"  [{r['id']}] {r['symbol']} ({r['side'].upper()} {r['qty']}) | Entry: ${r['entry_price']:.2f} | SL: ${r['stop_loss']} | TP: ${r['take_profit']} | Entry Time: {r['entry_time']}")

    # 2. Equity Curve Statistics
    equity_df = pd.read_sql_query("SELECT timestamp, equity, balance, unrealized_pnl FROM equity_history ORDER BY timestamp ASC", conn)
    if not equity_df.empty:
        equity_df['equity'] = pd.to_numeric(equity_df['equity'], errors='coerce')
        initial_equity = equity_df['equity'].iloc[0]
        current_equity = equity_df['equity'].iloc[-1]
        max_equity = equity_df['equity'].max()
        min_equity = equity_df['equity'].min()
        total_return_pct = ((current_equity - initial_equity) / initial_equity) * 100

        # Drawdown calculation
        equity_df['peak'] = equity_df['equity'].cummax()
        equity_df['dd'] = (equity_df['peak'] - equity_df['equity']) / equity_df['peak']
        max_dd_pct = equity_df['dd'].max() * 100

        print("\n--- ACCOUNT EQUITY & RISK PROFILE ---")
        print(f"History Snapshots: {len(equity_df)}")
        print(f"First Snapshot:    {equity_df['timestamp'].iloc[0]} -> Equity: ${initial_equity:,.2f}")
        print(f"Latest Snapshot:   {equity_df['timestamp'].iloc[-1]} -> Equity: ${current_equity:,.2f}")
        print(f"Peak Equity:       ${max_equity:,.2f}")
        print(f"Trough Equity:     ${min_equity:,.2f}")
        print(f"Total Return:      {total_return_pct:+.2f}%")
        print(f"Max Drawdown:      {max_dd_pct:.2f}%")

    conn.close()

    # 3. Decision Metrics Analysis
    metrics_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decision_metrics.log")
    if os.path.exists(metrics_path):
        signals = {"BUY": 0, "SELL": 0, "SELL_SHORT": 0, "COVER": 0, "HOLD": 0}
        symbol_counts = {}
        with open(metrics_path, "r", encoding="utf-8") as f:
            for line in f:
                for sig in ["SELL_SHORT", "BUY", "SELL", "COVER", "HOLD"]:
                    if f"Signal: {sig}" in line:
                        signals[sig] += 1
                        break
        print("\n--- STRATEGY SIGNAL DISTRIBUTION (decision_metrics.log) ---")
        for sig, count in signals.items():
            print(f"  {sig:12s}: {count:,}")

if __name__ == "__main__":
    run_audit()
