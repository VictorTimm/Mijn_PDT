"""Bitvavo transaction-history CSV.

The export's Currency column is the base: Amount is denominated in it.
Quote Currency is the price currency. Received / Paid Currency is the cash
side and fills in the quote when Quote Currency is empty. A Market column
such as BTC-EUR is used when those two disagree.

Rows use the shared transaction schema. Coins are ``security_type`` Crypto
once they become holdings. Euro deposits and withdrawals stay cash.
"""

from __future__ import annotations

import math

import pandas as pd

from processing.normalize import (
    FIAT,
    cell_at,
    find_column,
    make_row,
    norm_header,
    number_at,
    parse_datetime,
    records_to_frame,
)

SOURCE = "bitvavo"
SECURITY_TYPE = "Crypto"

_DONE = {"", "completed", "voltooid", "success", "successful"}
_EXACT_TYPES = {
    "buy": "buy",
    "koop": "buy",
    "sell": "sell",
    "verkoop": "sell",
    "deposit": "deposit",
    "storting": "deposit",
    "withdrawal": "withdrawal",
    "withdraw": "withdrawal",
    "opname": "withdrawal",
    "staking": "staking",
    "interest": "interest",
    "rebate": "interest",
    "affiliate": "interest",
    "dividend": "dividend",
    "distribution": "dividend",
}


def matches(filename: str, columns: list[str]) -> bool:
    del filename
    headers = {norm_header(column) for column in columns}
    return {"quote currency", "amount", "type"}.issubset(headers)


def security_type_for(symbol: str) -> str:
    """Bitvavo coins are crypto. Fiat rows are cash and have no security type."""
    if not symbol or symbol in FIAT:
        return ""
    return SECURITY_TYPE


def parse(df: pd.DataFrame) -> pd.DataFrame:
    dates = find_column(df, "date", "datum")
    times = find_column(df, "time", "tijd")
    types = find_column(df, "type")
    currency = find_column(df, "currency")
    amount = find_column(df, "amount")
    quote_currency = find_column(df, "quote currency")
    quote_price = find_column(df, "quote price")
    paid_currency = find_column(df, "received paid currency")
    paid_amount = find_column(df, "received paid amount")
    fee_currency = find_column(df, "fee currency")
    fee_amount = find_column(df, "fee amount")
    status = find_column(df, "status")
    market = find_column(df, "market")
    records = []
    skipped = 0
    for index in range(len(df)):
        state = cell_at(status, index).lower()
        if state not in _DONE:
            skipped += 1
            continue
        kind = _map_type(cell_at(types, index))
        if kind is None:
            skipped += 1
            continue
        when = parse_datetime(cell_at(dates, index), cell_at(times, index))
        if pd.isna(when):
            skipped += 1
            continue
        quantity = number_at(amount, index)
        if math.isnan(quantity):
            skipped += 1
            continue
        base, quote = _base_and_quote(
            cell_at(currency, index),
            cell_at(quote_currency, index),
            cell_at(paid_currency, index),
            cell_at(market, index),
        )
        if not base:
            skipped += 1
            continue
        price = number_at(quote_price, index)
        paid_code = cell_at(paid_currency, index).upper() or quote
        paid = number_at(paid_amount, index)
        fee = number_at(fee_amount, index)
        fee_code = cell_at(fee_currency, index).upper()
        if math.isnan(fee):
            fee = 0.0
        row = _row(when, kind, base, quote, abs(quantity), price, paid, paid_code, fee, fee_code)
        if row is None:
            skipped += 1
            continue
        records.append(row)
    frame = records_to_frame(records)
    if skipped:
        frame.attrs["warning"] = f"Skipped {skipped} rows that were incomplete or not a known type."
    return frame


def _map_type(raw: str) -> str | None:
    text = " ".join(raw.strip().lower().replace("_", " ").split())
    if text in _EXACT_TYPES:
        return _EXACT_TYPES[text]
    if "staking" in text or "reward" in text or text == "earn":
        return "staking"
    if text.startswith("buy") or " koop" in f" {text}":
        return "buy"
    if text.startswith("sell") or "verkoop" in text:
        return "sell"
    if "deposit" in text or "stort" in text:
        return "deposit"
    if "withdraw" in text or "opname" in text:
        return "withdrawal"
    if any(word in text for word in ("interest", "rebate", "affiliate", "lending")):
        return "interest"
    if "dividend" in text or "distribution" in text:
        return "dividend"
    return None


def _base_and_quote(currency: str, quote_currency: str, paid_currency: str, market: str) -> tuple[str, str]:
    """Return ``(base, quote)`` for one Bitvavo row."""
    base = currency.strip().upper()
    quote = quote_currency.strip().upper()
    paid = paid_currency.strip().upper()
    market_base, market_quote = _split_market(market)
    if market_base and market_quote:
        if not base:
            base, quote = market_base, market_quote
        elif base == market_quote and (quote == market_base or not quote):
            base, quote = market_base, market_quote
        elif base == market_base:
            quote = quote or market_quote
    if not quote:
        quote = paid
    return base, quote


def _split_market(market: str) -> tuple[str, str]:
    text = market.strip().upper().replace("/", "-").replace("_", "-")
    if "-" not in text:
        return "", ""
    base, quote = text.split("-", 1)
    if not base or not quote:
        return "", ""
    return base, quote


def _row(
    when: pd.Timestamp,
    kind: str,
    base: str,
    quote: str,
    quantity: float,
    price: float,
    paid: float,
    paid_code: str,
    fee: float,
    fee_code: str,
) -> dict[str, object] | None:
    if kind in {"buy", "sell"}:
        if math.isnan(paid):
            return None
        settlement = paid_code or quote or "EUR"
        cashflow = _signed_quote(kind, price, quantity, paid, fee, fee_code in {"", settlement, quote})
        return make_row(
            date=when,
            broker=SOURCE,
            type=kind,
            symbol=base,
            name=base,
            quantity=quantity,
            price=price,
            amount=cashflow,
            fees=fee if fee_code in {"", settlement} else 0.0,
            currency=settlement,
            original_currency=quote or settlement,
        )
    if base in FIAT:
        inbound = kind in {"deposit", "staking", "interest", "dividend"}
        cashflow = quantity if inbound else -quantity
        mapped = "dividend" if kind in {"staking", "interest", "dividend"} else kind
        if mapped not in {"deposit", "withdrawal", "dividend", "fee"}:
            mapped = "deposit" if cashflow > 0 else "withdrawal"
        return make_row(
            date=when,
            broker=SOURCE,
            type=mapped,
            symbol=base,
            name=base,
            amount=cashflow,
            fees=fee,
            currency=base,
            original_currency=base,
        )
    # Crypto deposits, withdrawals, and staking rewards move coins, not cash.
    signed_quantity = -quantity if kind == "withdrawal" else quantity
    return make_row(
        date=when,
        broker=SOURCE,
        type="transfer",
        symbol=base,
        name=base,
        quantity=signed_quantity,
        price=price,
        amount=0.0,
        fees=fee if fee_code in {"", quote} else fee,
        currency=quote or "EUR",
        original_currency=quote or base,
    )


def _signed_quote(
    kind: str,
    price: float,
    quantity: float,
    paid: float,
    fee: float,
    fee_in_quote: bool,
) -> float:
    amount = abs(paid)
    gross = None if math.isnan(price) or price <= 0 else price * quantity
    if fee_in_quote and fee and gross is not None:
        inclusive = gross + fee if kind == "buy" else max(gross - fee, 0.0)
        if abs(amount - gross) <= abs(amount - inclusive):
            amount = amount + fee if kind == "buy" else max(amount - fee, 0.0)
    return -amount if kind == "buy" else amount
