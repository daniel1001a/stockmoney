"""Learning layer: post-mortems, shadow books, regime allocator, event chains.

Isolation rule: nothing in this package imports a broker or the risk engine, and
nothing here can create, change or cancel an order. It READS the ledger tables
(fills, planned_orders, submitted, bridge_messages) and the price cache, and
WRITES only its own tables (see tables.py). It runs as its own scheduled job
(``python -m stockmoney.desk.learn.job``), outside the order-path job lock, so
a failure here can never affect trading.
"""
