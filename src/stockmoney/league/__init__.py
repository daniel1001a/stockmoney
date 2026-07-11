"""Trader League Arena (~/.claude/plans/trader-council-rearchitecture.md).

A runtime where a roster of simulated traders -- each with a distinct core
philosophy and a prediction engine -- each make one call per watchlist symbol
per day, get graded on one identical market ruler at close, get reviewed
(per-trader + cross-trader divergence), and evolve their method forward-only
under human review. The quant model becomes one trader (Chartist); the
news-cascade reasoner another (Analyst); more can be added trivially.

This whole package is a discretion/meta layer (CLAUDE.md section 7): it is
never read by any training path.
"""
