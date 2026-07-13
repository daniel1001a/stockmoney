-- Wave D (IMPROVEMENT_PLAN.md §S3): give each trader's directional call a
-- concrete option instrument and grade it on REAL option P&L, not just
-- whether the underlying direction was right. This is the same "a signal can
-- be right on direction and still lose to theta/IV-crush" gap
-- backtest_options_pnl.py already closes for the module-A backtest -- this
-- migration brings that same mechanism into the live per-prediction league
-- ledger.
--
-- option_structure: JSON snapshot of the OptionEntry chosen AT PREDICTION
-- TIME (spot/strike/is_call/iv/dte_days/side) via option_selection.select_option
-- -- NULL for 'range' calls (option_selection's no-trade convention) or when
-- no usable entry IV existed that day. Stored so grading later reprices the
-- EXACT SAME contract chosen at entry, never a contract picked with
-- hindsight.
-- option_pnl: net option return on premium (options_pnl.option_return's
-- convention, same shape as daily_predictions/ev_gate's pnl_gross), filled by
-- league/grading_options.py alongside the existing directional grading. NULL
-- until graded, and stays NULL forever for rows with no option_structure.
ALTER TABLE trader_predictions ADD COLUMN option_structure VARCHAR;
ALTER TABLE trader_predictions ADD COLUMN option_pnl DOUBLE;
