#!/usr/bin/env python3
"""
v9_optimizer.py
Bayesian optimizer (TPE) for Backtester V8/V7 family.

Usage:
    python v9_optimizer.py --backtester-module backtester_v8 --max-evals 200 --parallel 4

Notes:
- The optimizer will import the backtester module you indicate (default: backtester_v8),
  overwrite top-level globals (COMMISSION_PCT, BASE_SLIPPAGE, etc.) with candidate params,
  instantiate BacktesterV8 (or Backtester class inside the module) and run it.
- The backtester module must expose a class named `BacktesterV8` with a `.run()` coroutine or `.run()` method.
- If hyperopt is not installed, the script falls back to random search.
"""
import os
import sys
import json
import time
import argparse
import importlib
import traceback
from functools import partial
from datetime import datetime
from multiprocessing import Pool, cpu_count

import numpy as np
import pandas as pd
from tqdm import tqdm

# Try hyperopt (TPE). If missing, fallback to random search.
try:
    from hyperopt import fmin, tpe, hp, Trials, STATUS_OK
    HYPEROPT_AVAILABLE = True
except Exception:
    HYPEROPT_AVAILABLE = False

# Optional plotting
try:
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except Exception:
    MATPLOTLIB_AVAILABLE = False

# ---------------------------
# Default search space (you can extend)
# ---------------------------
def get_search_space():
    return {
        # Breakout params
        'atr_sl':           hp.choice('atr_sl', [0.6, 0.8, 1.0, 1.2]),
        'atr_tp':           hp.choice('atr_tp', [1.0, 1.25, 1.5, 1.8]),
        'strict_vol':       hp.loguniform('strict_vol', np.log(1.0), np.log(3.0)),
        'volume_factor':    hp.uniform('volume_factor', 1.0, 1.5),
        # Ranging params
        'rng_sl':           hp.choice('rng_sl', [0.3, 0.5, 0.7]),
        'rng_tp':           hp.choice('rng_tp', [1.5, 2.0, 2.5]),
        'time_stop_h':      hp.choice('time_stop_h', [8, 12, 24]),
        # Filters
        'ema_period':       hp.choice('ema_period', [20, 30, 50]),
        'min_atr_pct':      hp.uniform('min_atr_pct', 0.2, 1.0),
        'cpr_width':        hp.uniform('cpr_width', 0.05, 0.4),
        # Microstructure
        'impact_coef':      hp.loguniform('impact_coef', np.log(1e-6), np.log(1e-2)),
        'base_slippage':    hp.uniform('base_slippage', 0.00005, 0.0005),
    }

# ---------------------------
# Evaluation wrapper
# ---------------------------
def evaluate_candidate(candidate, backtester_module_name, run_id=None, run_timeout=None, verbose=False):
    """
    Candidate is a dict of parameters from the search space.
    The function imports the backtester module, overrides module-level globals,
    instantiates the backtester and runs it. Returns a dict with objective metrics.
    """
    start_ts = time.time()
    result = {
        'status': 'fail',
        'exception': None,
        'time': None,
        'metrics': {},
        'params': candidate,
        'run_id': run_id,
    }

    try:
        # Import module fresh (force reload to reset globals between runs)
        if backtester_module_name in sys.modules:
            del sys.modules[backtester_module_name]
        mod = importlib.import_module(backtester_module_name)
        importlib.reload(mod)

        # Helper to set attribute if exists
        def safe_set(name, value):
            if hasattr(mod, name):
                setattr(mod, name, value)
            else:
                # create it anyway so backtester can read it
                setattr(mod, name, value)

        # Map candidate into backtester globals (names used in your backtester)
        safe_set('ATR_SL_MULT', candidate.get('atr_sl', 1.0))            # if used
        safe_set('ATR_TP_MULT', candidate.get('atr_tp', 1.25))
        safe_set('STRICT_VOLUME_FACTOR', float(candidate.get('strict_vol', 1.5)))
        safe_set('VOLUME_FACTOR', float(candidate.get('volume_factor', 1.1)))
        safe_set('RANGING_SL_MULT', candidate.get('rng_sl', 0.5))
        safe_set('RANGING_TP_MULT', candidate.get('rng_tp', 1.5))
        safe_set('TIME_STOP_HOURS', int(candidate.get('time_stop_h', 12)))
        safe_set('EMA_PERIOD', int(candidate.get('ema_period', 20)))
        safe_set('MIN_VOLATILITY_ATR_PCT', float(candidate.get('min_atr_pct', 0.5)))
        safe_set('CPR_WIDTH_THRESHOLD', float(candidate.get('cpr_width', 0.2)))
        safe_set('IMPACT_COEF', float(candidate.get('impact_coef', 0.0005)))
        safe_set('BASE_SLIPPAGE', float(candidate.get('base_slippage', 0.0002)))

        # Optionally tune other constants
        safe_set('TEST_START_DATE', getattr(mod, 'TEST_START_DATE', None))
        safe_set('TEST_END_DATE', getattr(mod, 'TEST_END_DATE', None))

        # Instantiate backtester class. Expecting BacktesterV8 or BacktesterV7 name.
        # Try a few names
        bt_class = None
        for name in ('BacktesterV8', 'BacktesterV7', 'BacktesterV6', 'BacktesterV9', 'Backtester'):
            if hasattr(mod, name):
                bt_class = getattr(mod, name)
                break
        if bt_class is None:
            raise RuntimeError(f"No Backtester class found in module {backtester_module_name}. Expected BacktesterV8 or similar.")

        # Instantiate
        bt = bt_class()

        # Run - support coroutine or regular function
        if hasattr(bt, 'run') and callable(bt.run):
            # If run is coroutine, run via asyncio
            import inspect, asyncio
            if inspect.iscoroutinefunction(bt.run):
                # run in event loop
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(bt.run())
                loop.close()
            else:
                bt.run()
        else:
            raise RuntimeError("Backtester has no runnable 'run' method.")

        # After running, collect metrics from bt.state.trades_history and bt.state.balance (convention from V8)
        trades = getattr(bt.state, 'trades_history', [])
        balance = getattr(bt.state, 'balance', None)
        start_balance = getattr(mod, 'START_BALANCE', None) or getattr(bt.state, 'daily_start_balance', None) or 1000

        if not trades or len(trades) == 0:
            total_pnl = 0.0
            pf = 0.0
            win_rate = 0.0
            mdd = 0.0
            sharpe = -10.0
        else:
            df = pd.DataFrame(trades)
            total_pnl = float(df['pnl'].sum())
            wins = len(df[df['pnl'] > 0])
            losses = len(df[df['pnl'] <= 0])
            win_rate = wins / len(df) if len(df) else 0.0
            gross_profit = df[df['pnl'] > 0]['pnl'].sum()
            gross_loss = abs(df[df['pnl'] < 0]['pnl'].sum())
            pf = float(gross_profit / gross_loss) if gross_loss != 0 else float('inf')

            # Sharpe (simple): mean(daily returns)/std(daily returns) annualized
            # We reconstruct equity series trade-by-trade and compute returns
            equity = [start_balance]
            curr = start_balance
            for pnl in df['pnl']:
                curr += pnl
                equity.append(curr)
            returns = np.diff(equity) / equity[:-1]
            if len(returns) > 1:
                sharpe = (np.mean(returns) / (np.std(returns) + 1e-12)) * np.sqrt(252)  # rough
            else:
                sharpe = -5.0

            # Max drawdown
            peak = equity[0]
            max_dd = 0.0
            for v in equity:
                if v > peak:
                    peak = v
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
            mdd = max_dd * 100.0

        # Compose score (customizable). Higher is better.
        # We maximize PF and Sharpe, penalize MDD and low trade count.
        score = (pf * 2.0) + (sharpe * 1.5) - (mdd * 0.1) + (len(trades) * 0.01)

        result['status'] = 'ok'
        result['time'] = time.time() - start_ts
        result['metrics'] = {
            'total_pnl': total_pnl,
            'pf': pf,
            'win_rate': win_rate,
            'mdd': mdd,
            'sharpe': sharpe,
            'n_trades': len(trades),
            'balance': balance,
            'score': score,
        }
        return result

    except Exception as e:
        result['status'] = 'fail'
        result['exception'] = traceback.format_exc()
        result['time'] = time.time() - start_ts
        if verbose:
            print("Exception in evaluate_candidate:", result['exception'])
        return result

# ---------------------------
# Hyperopt wrapper objective
# ---------------------------
def make_objective(backtester_module_name, run_id_prefix):
    def objective(params):
        # hyperopt supplies nested structures — simplify
        flat = {}
        # convert choices/hp to simple floats/ints appropriately
        flat['atr_sl'] = float(params.get('atr_sl', 1.0))
        flat['atr_tp'] = float(params.get('atr_tp', 1.25))
        flat['strict_vol'] = float(params.get('strict_vol', params.get('strict_vol', 1.5)))
        flat['volume_factor'] = float(params.get('volume_factor', 1.1))
        flat['rng_sl'] = float(params.get('rng_sl', 0.5))
        flat['rng_tp'] = float(params.get('rng_tp', 1.5))
        flat['time_stop_h'] = int(params.get('time_stop_h', 12))
        flat['ema_period'] = int(params.get('ema_period', 20))
        flat['min_atr_pct'] = float(params.get('min_atr_pct', 0.5))
        flat['cpr_width'] = float(params.get('cpr_width', 0.2))
        flat['impact_coef'] = float(params.get('impact_coef', IMPACT_COEF))
        flat['base_slippage'] = float(params.get('base_slippage', BASE_SLIPPAGE))

        run_id = f"{run_id_prefix}_{int(time.time()*1000)}"
        res = evaluate_candidate(flat, backtester_module_name, run_id=run_id)
        # hyperopt minimizes the objective, so return negative score
        metrics = res.get('metrics', {})
        score = metrics.get('score', -9999.0)
        loss = -score
        out = {
            'loss': loss,
            'status': STATUS_OK if res['status'] == 'ok' else 'fail',
            'eval_time': res.get('time', 0),
            'other_metrics': metrics,
            'params': flat,
        }
        # save intermediate
        return out
    return objective

# ---------------------------
# Runner
# ---------------------------
def run_optimizer(backtester_module_name='backtester_v8', max_evals=200, parallel=1, trials_file='v9_trials.json'):
    print(f"Starting V9 optimizer on module {backtester_module_name} (max_evals={max_evals}, parallel={parallel})")
    space = get_search_space()

    if HYPEROPT_AVAILABLE:
        print("Using hyperopt TPE (Bayesian).")
        trials = Trials()
        best = fmin(
            fn=make_objective(backtester_module_name, run_id_prefix='v9'),
            space=space,
            algo=tpe.suggest,
            max_evals=max_evals,
            trials=trials,
            rstate=np.random.RandomState(42)
        )
        # convert trials to list of dicts
        trials_list = []
        for t in trials.trials:
            trials_list.append({
                'tid': t['tid'],
                'result': t['result'],
                'misc': t['misc'],
                'state': t['state'],
            })
        with open(trials_file, 'w') as f:
            json.dump({'best': best, 'trials': trials_list}, f, default=str, indent=2)
        print("Best (hyperopt raw):", best)
        # Extract top results
        records = []
        for t in trials.trials:
            r = t['result']
            if 'other_metrics' in r:
                rec = r['other_metrics'].copy()
                rec.update({'params': r.get('params', {})})
                records.append(rec)
        df = pd.DataFrame(records).sort_values('score', ascending=False)
    else:
        print("hyperopt not available — falling back to random search (bayes-like).")
        records = []
        for i in tqdm(range(max_evals)):
            # sample from same distributions
            sampled = {
                'atr_sl': np.random.choice([0.6,0.8,1.0,1.2]),
                'atr_tp': np.random.choice([1.0,1.25,1.5,1.8]),
                'strict_vol': float(np.exp(np.random.uniform(np.log(1.0), np.log(3.0)))),
                'volume_factor': float(np.random.uniform(1.0,1.5)),
                'rng_sl': float(np.random.choice([0.3,0.5,0.7])),
                'rng_tp': float(np.random.choice([1.5,2.0,2.5])),
                'time_stop_h': int(np.random.choice([8,12,24])),
                'ema_period': int(np.random.choice([20,30,50])),
                'min_atr_pct': float(np.random.uniform(0.2,1.0)),
                'cpr_width': float(np.random.uniform(0.05,0.4)),
                'impact_coef': float(np.exp(np.random.uniform(np.log(1e-6), np.log(1e-2)))),
                'base_slippage': float(np.random.uniform(0.00005,0.0005)),
            }
            res = evaluate_candidate(sampled, backtester_module_name, run_id=f"rand_{i}")
            m = res.get('metrics', {})
            m['params'] = sampled
            records.append(m)
        df = pd.DataFrame(records).sort_values('score', ascending=False)
        with open(trials_file, 'w') as f:
            json.dump({'trials': records}, f, default=str, indent=2)

    # Save top 20
    top20 = df.head(20)
    top20.to_csv('v9_top20.csv', index=False)
    print("Top 20 saved to v9_top20.csv")

    # Save best as JSON
    if not df.empty:
        best_row = df.iloc[0].to_dict()
        with open('v9_best_params.json', 'w') as f:
            json.dump(best_row, f, default=str, indent=2)
        print("Best params saved to v9_best_params.json")

    # Optional heatmap (if columns exist)
    if MATPLOTLIB_AVAILABLE and 'atr_sl' in df.columns and 'atr_tp' in df.columns:
        try:
            pivot = df.pivot_table(index='atr_sl', columns='atr_tp', values='score', aggfunc='mean')
            plt.figure(figsize=(8,6))
            plt.title('ATR_SL vs ATR_TP score heatmap')
            sns_exists = False
            try:
                import seaborn as sns
                sns_exists = True
                sns.heatmap(pivot, annot=True, fmt=".2f")
            except Exception:
                plt.imshow(pivot.values, aspect='auto', origin='lower')
                plt.colorbar()
            plt.savefig('v9_heatmap_atr_sl_tp.png', dpi=150)
            print("Heatmap saved to v9_heatmap_atr_sl_tp.png")
        except Exception as e:
            print("Could not build heatmap:", e)

    print("Optimizer finished.")

# ---------------------------
# CLI
# ---------------------------
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument('--backtester-module', type=str, default='backtester_v8', help='Python module name of backtester (without .py)')
    p.add_argument('--max-evals', type=int, default=200)
    p.add_argument('--parallel', type=int, default=1)
    p.add_argument('--trials-file', type=str, default='v9_trials.json')
    args = p.parse_args()

    run_optimizer(backtester_module_name=args.backtester_module, max_evals=args.max_evals, parallel=args.parallel, trials_file=args.trials_file)
