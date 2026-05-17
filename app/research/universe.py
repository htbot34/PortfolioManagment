"""Curated candidate universe organized by the investor's themes.

Used by the candidate generator to ground LLM suggestions in real tickers
we can actually fetch data for, rather than letting the model hallucinate.
"""

UNIVERSE: dict[str, list[str]] = {
    "AI infrastructure": [
        "NVDA", "AMD", "AVGO", "TSM", "MU", "MRVL", "SMCI", "ARM", "ASML", "LRCX",
    ],
    "AI applications": [
        "GOOGL", "MSFT", "CRM", "NOW", "SNOW", "DDOG", "NET", "MDB", "PLTR", "AI",
    ],
    "Small modular reactors / clean energy": [
        "SMR", "NNE", "BWXT", "GEV", "VST", "CEG", "CCJ", "URA", "UEC", "LEU",
    ],
    "Cybersecurity": [
        "PANW", "CRWD", "ZS", "S", "FTNT", "OKTA", "CYBR", "RBRK",
    ],
    "Bitcoin / digital assets infra": [
        "COIN", "MSTR", "MARA", "RIOT", "CIFR", "IREN", "HUT",
    ],
    "Secular growth": [
        "SHOP", "MELI", "SOFI", "HOOD", "ROKU", "U", "RBLX",
    ],
    "Quality compounders": [
        "META", "AMZN", "NFLX", "COST", "BKNG",
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
