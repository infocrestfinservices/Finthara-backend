"""An amount written out in words, as an invoice states its total.

INR uses the Indian system (thousand, lakh, crore) — ₹2,358.82 is "Rupees Two Thousand Three
Hundred Fifty-Eight and Eighty-Two Paise Only". Any other currency is written in the
international system with its own unit names (US Dollars / Cents).
"""
from __future__ import annotations

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

_UNITS = {"INR": ("Rupees", "Rupee", "Paise", "Paisa"),
          "USD": ("US Dollars", "US Dollar", "Cents", "Cent")}


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def _indian(n: int) -> str:
    """Whole number in words, Indian grouping: crore, lakh, thousand, hundred."""
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    if crore:
        parts.append(f"{_indian(crore)} Crore")
    if lakh:
        parts.append(f"{_below_hundred(lakh)} Lakh")
    if thousand:
        parts.append(f"{_below_hundred(thousand)} Thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def _international(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((1_000_000_000, "Billion"), (1_000_000, "Million"), (1_000, "Thousand")):
        chunk, n = divmod(n, size)
        if chunk:
            parts.append(f"{_below_thousand(chunk)} {name}")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(amount: float, currency: str = "INR") -> str:
    """e.g. amount_in_words(2358.82) -> 'Rupees Two Thousand Three Hundred Fifty-Eight and
    Eighty-Two Paise Only'."""
    currency = (currency or "INR").upper()
    cents_total = int(round(abs(float(amount or 0)) * 100))
    whole, fraction = divmod(cents_total, 100)
    many, one, many_small, one_small = _UNITS.get(currency, (currency, currency, "", ""))
    words = _indian(whole) if currency == "INR" else _international(whole)
    text = f"{one if whole == 1 else many} {words}"
    if fraction:
        small = _below_hundred(fraction)
        text += f" and {small} {one_small if fraction == 1 else many_small}".rstrip()
    return text + " Only"
