"""Display currency. Prices are always stored in USD (Scryfall's market prices); ZAR is
only a display conversion with a rate you set yourself (settings.json: usd_zar)."""

SYMBOLS = {"USD": "$", "ZAR": "R"}


def display_currency(settings):
    cur = (settings or {}).get("currency", "USD")
    rate = (settings or {}).get("usd_zar")
    if cur == "ZAR" and not rate:
        return "USD", 1.0  # no rate set yet: keep showing dollars
    return (cur, float(rate)) if cur == "ZAR" else ("USD", 1.0)


def money(usd, settings, decimals=2):
    """Format a USD amount in the chosen display currency: $72.20 or R1,245.50."""
    if usd is None:
        return "-"
    cur, rate = display_currency(settings)
    return f"{SYMBOLS[cur]}{usd * rate:,.{decimals}f}"


def parse_rate(text):
    """'17.25', 'R17,25', '17.25 ' -> 17.25; raises ValueError for anything unusable."""
    t = (text or "").strip().upper().lstrip("R").strip().replace(",", ".")
    rate = float(t)
    if not 1 <= rate <= 1000:
        raise ValueError("that doesn't look like a USD to ZAR rate")
    return rate
