#!/usr/bin/env python3
"""MCP server exposing your local Fold data as tools for Claude (read-only).

Run:  .venv/bin/python fold_mcp.py     (stdio)
Env:  FOLD_DB (default fold_export/fold.db), FOLD_ALIASES (default fold_aliases.json)

Register in Claude Code:
  claude mcp add fold -- /home/het/Pentest/fold.money/.venv/bin/python \\
      /home/het/Pentest/fold.money/fold_mcp.py
"""
import os
import sqlite3
from pathlib import Path

import fold_analytics as fa
from mcp.server.fastmcp import FastMCP

DB = Path(os.environ.get("FOLD_DB", str(Path(__file__).parent / "fold_export/fold.db")))
ALIASES = Path(os.environ.get("FOLD_ALIASES", str(Path(__file__).parent / "fold_aliases.json")))
mcp = FastMCP("fold")


def _need():
    if not DB.exists():
        raise RuntimeError(f"No Fold DB at {DB}. Run `./fold sync` first.")


@mcp.tool()
def summary(month: str = "") -> str:
    """Spending summary for a month (YYYY-MM; default latest): balances, in/out/net, top categories & payees, recurring."""
    _need()
    d = fa.compute(DB, month=month or None, aliases_path=ALIASES)
    lt = d["lifetime"]
    out = [f"Month {d['month']} | balance {fa.inr(d['total_balance'])} | "
           f"lifetime in {fa.inr(lt['income'])} out {fa.inr(lt['expense'])} net {fa.inr(lt['net'])}",
           "Top categories: " + ", ".join(f"{n} {fa.inr_c(a)}" for n, a, _ in d["cat_month"][:6]),
           "Top payees: " + ", ".join(f"{n} {fa.inr_c(a)}" for n, a, _ in d["merch_month"][:6]),
           "Recurring: " + ", ".join(f"{cp}({m}mo)" for cp, m, _, _ in d["recurring"][:6])]
    return "\n".join(out)


@mcp.tool()
def monthly_trend() -> str:
    """Income, expense and net for every month on record."""
    _need()
    d = fa.compute(DB, aliases_path=ALIASES)
    return "\n".join(f"{m['month']}: in {fa.inr_c(m['income'])} out {fa.inr_c(m['expense'])} net {fa.inr_c(m['net'])}"
                     for m in d["monthly"])


@mcp.tool()
def tax(fy: str = "") -> str:
    """Indian financial-year (Apr-Mar) income/expense report. fy like '2025-26' (default current)."""
    _need()
    t = fa.tax_year(DB, fy=fy or None, aliases_path=ALIASES)
    cats = ", ".join(f"{k} {fa.inr_c(v)}" for k, v in t["by_category"][:8])
    return (f"FY {t['fy']} ({t['start']}->{t['end']}): income {fa.inr(t['income'])}, "
            f"expense {fa.inr(t['expense'])}, net {fa.inr(t['net'])}, {t['n']} txns\nBy category: {cats}")


@mcp.tool()
def compare(month1: str, month2: str) -> str:
    """Compare two months (YYYY-MM) with per-category deltas."""
    _need()
    c = fa.compare_months(DB, month1, month2, aliases_path=ALIASES)
    rows = "\n".join(f"  {k}: {fa.inr_c(a)} -> {fa.inr_c(b)} ({'+' if d>=0 else ''}{fa.inr_c(d)})"
                     for k, a, b, d in c["by_category"][:12])
    return (f"{c['m1']} vs {c['m2']}\nincome {c['income']} expense {c['expense']}\n{rows}")


@mcp.tool()
def unusual() -> str:
    """Transactions that are >=2.5x a payee's average (possible anomalies)."""
    _need()
    u = fa.unusual(DB, aliases_path=ALIASES)
    return "\n".join(f"{d} {cp} {fa.inr(a)} (avg {fa.inr_c(av)})" for d, cp, a, av in u) or "None flagged."


@mcp.tool()
def search(query: str, limit: int = 25) -> str:
    """Search transactions by text in narration or merchant. Returns date, amount, type, narration."""
    _need()
    db = sqlite3.connect(str(DB))
    q = ("SELECT txn_date, type, amount, narration FROM transactions "
         "WHERE narration LIKE ? OR merchant_name LIKE ? ORDER BY txn_timestamp DESC LIMIT ?")
    like = f"%{query}%"
    rows = db.execute(q, (like, like, limit)).fetchall()
    db.close()
    if not rows:
        return f"No transactions matching '{query}'."
    return "\n".join(f"{d} {t[:3]} {fa.inr(a)}  {(n or '')[:50]}" for d, t, a, n in rows)


if __name__ == "__main__":
    mcp.run()
