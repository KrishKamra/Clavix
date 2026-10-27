"""Aggregate benchmark results into tables for the paper.

Input is the ``results_<run>.csv`` / ``.json`` written by the harness (or any
list of :class:`BenchmarkRecord`). Outputs are pandas DataFrames:

* :func:`success_rate_by_tier` — Task Success Rate per tier.
* :func:`latency_summary` — mean / median / p95 per latency phase and tier.
* :func:`latency_waterfall` — mean ASR -> LLM -> OS execution breakdown per tier.

Usage::

    uv run python -m clavix.evaluation.metrics benchmarks/results_<run>.csv
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd

from clavix.schemas.telemetry import BenchmarkRecord

LATENCY_PHASES = ("t_asr_ms", "t_llm_planning_ms", "t_total_execution_ms")


def records_to_frame(records: Iterable[BenchmarkRecord]) -> pd.DataFrame:
    """Convert records to a DataFrame with an added ``t_end_to_end_ms`` column."""
    df = pd.DataFrame([r.model_dump() for r in records])
    return _with_end_to_end(df)


def load_results(path: str | Path) -> pd.DataFrame:
    """Load a harness results file (``.csv`` or ``.json``)."""
    p = Path(path)
    if p.suffix == ".json":
        df = pd.DataFrame(json.loads(p.read_text(encoding="utf-8"))["records"])
    else:
        df = pd.read_csv(p)
    return _with_end_to_end(df)


def _with_end_to_end(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["t_end_to_end_ms"] = df[list(LATENCY_PHASES)].sum(axis=1)
    return df


def success_rate_by_tier(df: pd.DataFrame) -> pd.DataFrame:
    """Task Success Rate per tier plus an ``all`` row.

    Columns: ``tier, n_tasks, n_success, task_success_rate, mean_recovery_attempts``.
    """

    def _agg(group: pd.DataFrame, tier: object) -> dict[str, object]:
        n = len(group)
        n_ok = int(group["task_success"].astype(bool).sum())
        return {
            "tier": tier,
            "n_tasks": n,
            "n_success": n_ok,
            "task_success_rate": n_ok / n if n else float("nan"),
            "mean_recovery_attempts": float(group["recovery_attempts"].mean()) if n else float("nan"),
        }

    rows = [_agg(g, int(str(tier))) for tier, g in df.groupby("tier", sort=True)]
    rows.append(_agg(df, "all"))
    return pd.DataFrame(rows)


def latency_summary(df: pd.DataFrame, phases: Sequence[str] | None = None) -> pd.DataFrame:
    """Mean, median and p95 latency (ms) per phase and tier, in long format."""
    phases = list(phases or (*LATENCY_PHASES, "t_end_to_end_ms"))
    long = df.melt(id_vars=["tier"], value_vars=phases, var_name="phase", value_name="ms")
    grouped = long.groupby(["tier", "phase"], sort=True)["ms"]
    out = grouped.agg(mean="mean", median="median", p95=lambda s: s.quantile(0.95)).reset_index()
    out["phase"] = pd.Categorical(out["phase"], categories=phases, ordered=True)
    return out.sort_values(["tier", "phase"]).reset_index(drop=True)


def latency_waterfall(df: pd.DataFrame) -> pd.DataFrame:
    """Mean per-phase latency per tier (one row per tier), for stacked bar charts."""
    return df.groupby("tier", sort=True)[list(LATENCY_PHASES)].mean().reset_index()


def write_summary(df: pd.DataFrame, out_dir: str | Path, stem: str = "summary") -> list[Path]:
    """Write the three summary tables as CSVs into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "success_by_tier": success_rate_by_tier(df),
        "latency_summary": latency_summary(df),
        "latency_waterfall": latency_waterfall(df),
    }
    paths = []
    for name, table in tables.items():
        path = out / f"{stem}_{name}.csv"
        table.to_csv(path, index=False)
        paths.append(path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    """Print summary tables for a results file; optionally write them as CSV."""
    parser = argparse.ArgumentParser(description="Summarise Clavix benchmark results.")
    parser.add_argument("results", help="results_<run>.csv or .json")
    parser.add_argument("--out", help="directory to write summary CSVs into")
    args = parser.parse_args(argv)

    df = load_results(args.results)
    if df.empty:
        print("no records")
        return 1
    print("Task success rate\n" + success_rate_by_tier(df).to_string(index=False))
    print("\nLatency waterfall (mean ms)\n" + latency_waterfall(df).to_string(index=False))
    print("\nLatency summary (ms)\n" + latency_summary(df).to_string(index=False))
    if args.out:
        for path in write_summary(df, args.out, Path(args.results).stem):
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
