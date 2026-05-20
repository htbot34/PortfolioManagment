"""Breadth basket - a fixed list of ~50 large-caps used to measure market
breadth (the % trading above their own 50-day SMA).

This is roughly the S&P 500's top 50 by index weight. It is a static list on
purpose: breadth only needs a representative large-cap sample, not an exact
index replica. Refresh it manually every few quarters if the megacap mix
drifts materially.
"""

BREADTH_BASKET: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO", "TSLA",
    "BRK-B", "JPM", "LLY", "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "JNJ",
    "NFLX", "BAC", "ABBV", "CRM", "ORCL", "CVX", "WMT", "KO", "AMD", "PEP",
    "TMO", "LIN", "ACN", "MRK", "ADBE", "MCD", "CSCO", "ABT", "PM", "IBM",
    "GE", "TXN", "ISRG", "QCOM", "INTU", "CAT", "VZ", "BKNG", "DIS", "NOW",
]
