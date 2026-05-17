"""Expanded universe of trade-able names organized by theme.

This is the scanning surface for daily opportunity detection. Bias is toward
aggressive growth / momentum names appropriate for a young, aggressive
risk profile.
"""

UNIVERSE: dict[str, list[str]] = {
    "Mega cap tech": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX", "AVGO", "ORCL",
    ],
    "Semiconductors": [
        "AMD", "TSM", "MU", "MRVL", "ARM", "LRCX", "ASML", "AMAT", "KLAC", "QCOM",
        "INTC", "SMCI", "ALAB", "ONTO",
    ],
    "AI infra / data": [
        "PLTR", "SNOW", "DDOG", "MDB", "NET", "ESTC", "PATH", "AI", "BBAI", "SOUN",
    ],
    "Cloud / SaaS": [
        "CRM", "NOW", "WDAY", "ADBE", "INTU", "VEEV", "ZS", "PANW", "CRWD", "S",
        "OKTA", "TEAM", "HUBS",
    ],
    "Cybersecurity": [
        "FTNT", "CYBR", "RBRK", "QLYS", "TENB", "VRNS",
    ],
    "SMR / nuclear / clean energy": [
        "SMR", "NNE", "BWXT", "CCJ", "URA", "UEC", "LEU", "GEV", "VST", "CEG",
        "NEE", "ENPH", "FSLR", "RUN",
    ],
    "Bitcoin / digital assets infra": [
        "COIN", "MSTR", "MARA", "RIOT", "CIFR", "IREN", "HUT", "BTBT", "CLSK", "WULF",
    ],
    "Fintech / payments": [
        "V", "MA", "PYPL", "SQ", "SOFI", "HOOD", "AFRM", "NU", "TOST",
    ],
    "Consumer growth": [
        "SHOP", "MELI", "SE", "ABNB", "UBER", "LYFT", "DASH", "CMG", "DKNG", "ROKU",
    ],
    "Healthcare / biotech": [
        "LLY", "NVO", "VRTX", "REGN", "ISRG", "DXCM", "MRNA", "RXRX", "CRSP", "BEAM",
    ],
    "Industrials growth": [
        "GE", "ETN", "PWR", "ROK", "TT", "URI", "CAT",
    ],
    "Defense / aero": [
        "RTX", "LMT", "NOC", "GD", "LDOS", "KTOS", "ASTS", "RKLB", "ACHR",
    ],
    "Speculative / high-beta": [
        "NBIS", "OKLO", "FBL", "RDDT", "TEM", "QBTS", "RGTI", "IONQ", "LUNR", "JOBY",
    ],
    "Quality compounders": [
        "BRK-B", "COST", "BKNG", "MA", "SPGI",
    ],
    "Sector / index ETFs": [
        "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLI",
        "XLC", "XBI", "SMH", "ARKK", "BITQ", "URA",
    ],
}


def all_tickers(exclude: set[str] | None = None) -> list[str]:
    exclude = {t.upper() for t in (exclude or set())}
    out: list[str] = []
    seen: set[str] = set()
    for tickers in UNIVERSE.values():
        for t in tickers:
            tu = t.upper()
            if tu in seen or tu in exclude:
                continue
            seen.add(tu)
            out.append(tu)
    return out


def theme_of(ticker: str) -> str | None:
    t = ticker.upper()
    for theme, names in UNIVERSE.items():
        if t in (n.upper() for n in names):
            return theme
    return None
