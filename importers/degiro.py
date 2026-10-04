"""DEGIRO Account.csv and Transactions.csv, in Dutch or English.

Numbers may use a comma as the decimal separator (``1.234,56``) or a dot.
The reader accepts comma, semicolon, and tab separators.

Transactions.csv columns (Dutch / English), with a currency cell after each
amount when DEGIRO leaves the header blank:

    Datum/Date, Tijd/Time, Product, ISIN, Aantal/Quantity, Koers/Price,
    Lokale waarde/Local value, Waarde/Value, Wisselkoers/FX rate,
    Transactiekosten/Transaction costs, Totaal/Total

Account.csv columns:

    Datum/Date, Tijd/Time, Valutadatum/Value date, Product, ISIN,
    Omschrijving/Description, FX, Mutatie/Change, Saldo/Balance

Buys and sells keep those types. ``amount`` is the signed cash total in the
settlement currency. ``original_currency`` is the currency of the price, so a
USD listing bought from a euro account stays distinguishable. Dividends,
dividend tax, deposits, withdrawals, and broker fees map to dividend, fee,
deposit, withdrawal, and fee. A currency conversion (FX Debit/Credit, Valuta
Debitering/Creditering) is a transfer in each currency, not a new deposit.
"""

from __future__ import annotations

import math
import re

import pandas as pd

from processing.normalize import (
    _looks_like_currency,
    cell_at,
    find_column,
    make_row,
    norm_header,
    number_at,
    parse_datetime,
    parse_number,
    records_to_frame,
)

SOURCE = "degiro"
_TRADE = re.compile(
    r"(?P<side>Koop|Verkoop|Buy|Sell)\s+(?P<qty>[\d.,]+)\s*@\s*(?P<price>[\d.,]+)\s*(?P<ccy>[A-Za-z]{3})?",
    re.IGNORECASE,
)
_CCY = re.compile(r"\b([A-Za-z]{3})\b")
_FX_MARKERS = (
    "fx debit",
    "fx credit",
    "valuta debitering",
    "valuta creditering",
    "currency conversion",
    "valutaconversie",
    "foreign exchange",
)


def matches(filename: str, columns: list[str]) -> bool:
    del filename
    headers = {norm_header(column) for column in columns}
    has_product = bool(headers & {"isin", "product"})
    has_activity = bool(
        headers
        & {
            "aantal",
            "quantity",
            "omschrijving",
            "description",
            "mutatie",
            "change",
        }
    )
    return has_product and has_activity


def parse(df: pd.DataFrame) -> pd.DataFrame:
    headers = {norm_header(column) for column in df.columns}
    if headers & {"aantal", "quantity"}:
        return _parse_transactions(df)
    if headers & {"omschrijving", "description", "mutatie", "change"}:
        return _parse_account(df)
    return _parse_transactions(df)


def _parse_transactions(df: pd.DataFrame) -> pd.DataFrame:
    dates = find_column(df, "datum", "date")
    times = find_column(df, "tijd", "time")
    product = find_column(df, "product")
    isin = find_column(df, "isin")
    description = find_column(df, "omschrijving", "description")
    quantity, _quantity_currency = _maybe_pair(find_column(df, "aantal", "quantity", with_currency=True))
    price, price_currency = _maybe_pair(find_column(df, "koers", "price", with_currency=True))
    venue = find_column(df, "beurs", "exchange", "execution venue", "uitvoeringsplaats")
    total, total_currency = _maybe_pair(
        find_column(df, "totaal", "totaal eur", "total", "total eur", with_currency=True)
    )
    fee, _fee_currency = _maybe_pair(
        find_column(
            df,
            "transactiekosten en of",
            "transaction costs",
            "transaction and or third party fees",
            "fee",
            with_currency=True,
        )
    )
    records = []
    for index in range(len(df)):
        when = parse_datetime(cell_at(dates, index), cell_at(times, index))
        if pd.isna(when):
            continue
        signed_quantity = number_at(quantity, index)
        signed_total = number_at(total, index)
        product_name = cell_at(product, index)
        product_isin = cell_at(isin, index).upper()
        text = cell_at(description, index)
        settlement = _currency(total_currency, index)
        if math.isnan(signed_quantity) or signed_quantity == 0:
            if math.isnan(signed_total) or signed_total == 0:
                continue
            kind = _cash_kind(text, product_name, signed_total)
            records.append(_cash_row(when, kind, product_name, product_isin, text, signed_total, settlement))
            continue
        kind = "buy" if signed_quantity > 0 else "sell"
        cashflow = signed_total
        if not math.isnan(cashflow):
            cashflow = -abs(cashflow) if kind == "buy" else abs(cashflow)
        fee_amount = number_at(fee, index)
        records.append(
            make_row(
                date=when,
                broker=SOURCE,
                type=kind,
                symbol=product_isin or product_name,
                name=product_name or product_isin,
                isin=product_isin,
                quantity=abs(signed_quantity),
                price=number_at(price, index),
                amount=cashflow,
                fees=0.0 if math.isnan(fee_amount) else abs(fee_amount),
                currency=settlement,
                original_currency=_currency(price_currency, index, settlement),
                venue=cell_at(venue, index).upper(),
            )
        )
    return records_to_frame(records)


def _parse_account(df: pd.DataFrame) -> pd.DataFrame:
    dates = find_column(df, "datum", "date")
    times = find_column(df, "tijd", "time")
    product = find_column(df, "product")
    isin = find_column(df, "isin")
    description = find_column(df, "omschrijving", "description")
    change, change_currency = _movement_column(df)
    balance, balance_currency = _amount_and_currency(df, "saldo", "balance")
    records = []
    seen_dates: list[pd.Timestamp] = []
    balance_rows: list[tuple[pd.Timestamp, int, str, float]] = []
    for index in range(len(df)):
        when = parse_datetime(cell_at(dates, index), cell_at(times, index))
        if pd.isna(when):
            continue
        seen_dates.append(pd.Timestamp(when))
        text = cell_at(description, index)
        product_name = cell_at(product, index)
        product_isin = cell_at(isin, index).upper()
        cashflow = number_at(change, index)
        currency = _currency(change_currency, index)
        balance_amount = number_at(balance, index)
        if not math.isnan(balance_amount):
            balance_rows.append((pd.Timestamp(when), index, _currency(balance_currency, index, currency), balance_amount))
        trade = _TRADE.search(text)
        if trade:
            kind = "buy" if trade.group("side").lower() in {"koop", "buy"} else "sell"
            price_currency = (trade.group("ccy") or currency).upper()
            signed = cashflow
            if not math.isnan(signed):
                signed = -abs(signed) if kind == "buy" else abs(signed)
            shares = parse_number(trade.group("qty"))
            records.append(
                make_row(
                    date=when,
                    broker=SOURCE,
                    type=kind,
                    symbol=product_isin or product_name,
                    name=product_name or product_isin,
                    isin=product_isin,
                    quantity=0.0 if math.isnan(shares) else abs(shares),
                    price=parse_number(trade.group("price")),
                    amount=signed,
                    currency=currency,
                    original_currency=price_currency,
                )
            )
            continue
        if math.isnan(cashflow) or cashflow == 0:
            continue
        kind = _cash_kind(text, product_name, cashflow)
        records.append(_cash_row(when, kind, product_name, product_isin, text, cashflow, currency))
    newest_first = len(seen_dates) >= 2 and seen_dates[0] > seen_dates[-1]
    _append_opening_balances(records, _latest_balances(balance_rows, newest_first))
    return records_to_frame(records)


def _append_opening_balances(records: list[dict[str, object]], last_balance: dict[str, float]) -> None:
    if not records or not last_balance:
        return
    earliest = min(record["date"] for record in records)
    for currency, saldo in last_balance.items():
        flowed = 0.0
        seen = False
        for record in records:
            if str(record.get("currency") or "EUR") != currency:
                continue
            amount = record.get("amount")
            if amount is None or (isinstance(amount, float) and math.isnan(amount)):
                continue
            flowed += float(amount)
            seen = True
        opening = saldo - flowed
        if not seen or abs(opening) < 0.01:
            continue
        records.append(
            make_row(
                date=pd.Timestamp(earliest) - pd.Timedelta(seconds=1),
                broker=SOURCE,
                type="deposit" if opening > 0 else "withdrawal",
                symbol=currency,
                name="Opening balance",
                quantity=0.0,
                amount=opening,
                currency=currency,
                original_currency=currency,
            )
        )


def _movement_column(df: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    """The euro movement column. DEGIRO sometimes names it ``Mutatie EUR``."""
    found, currency = _amount_and_currency(df, "mutatie", "change", "bedrag", "amount", "wijziging")
    if found is not None:
        return found, currency
    normalized = [norm_header(column) for column in df.columns]
    for position, header in enumerate(normalized):
        if not any(header == alias or header.startswith(f"{alias} ") for alias in ("mutatie", "change", "bedrag", "amount", "wijziging")):
            continue
        currency = df.iloc[:, position + 1] if position + 1 < len(df.columns) else None
        return df.iloc[:, position], currency
    for position, header in enumerate(normalized):
        if header != "fx" or position + 1 >= len(df.columns):
            continue
        return df.iloc[:, position + 1], None
    return None, None


def _cash_kind(description: str, product: str, cashflow: float) -> str:
    text = description.lower()
    if _is_fx(text):
        return "transfer"
    if "cash sweep transfer" in text:
        return "transfer"
    if _is_tax(text):
        return "fee"
    if "dividend" in text:
        return "dividend"
    if any(word in text for word in ("storting", "deposit", "ideal", "sofort", "flatex", "overboeking", "sepa")):
        return "deposit"
    if any(word in text for word in ("opname", "withdrawal", "terugstorting")):
        return "withdrawal"
    if any(word in text for word in ("belasting", "kosten", "fee", "cost", "tax", "duty")):
        return "fee"
    if product and cashflow > 0:
        return "dividend"
    if cashflow > 0:
        return "deposit"
    return "fee"


def _is_fx(text: str) -> bool:
    if "autofx" in text and any(word in text for word in ("kosten", "cost", "fee")):
        return False
    if any(marker in text for marker in _FX_MARKERS):
        return True
    return text.startswith("fx ") or text.startswith("valuta ")


def _is_tax(text: str) -> bool:
    return any(word in text for word in ("belasting", "withholding", "dividend tax", "bronbelasting"))


def _cash_row(
    when: pd.Timestamp,
    kind: str,
    product_name: str,
    product_isin: str,
    description: str,
    cashflow: float,
    currency: str,
) -> dict[str, object]:
    if kind == "deposit":
        cashflow = abs(cashflow)
    elif kind == "withdrawal":
        cashflow = -abs(cashflow)
    counter = _counter_currency(description, currency)
    if kind == "transfer":
        return make_row(
            date=when,
            broker=SOURCE,
            type="transfer",
            symbol=currency,
            name=description or "Currency conversion",
            amount=cashflow,
            currency=currency,
            original_currency=counter,
        )
    if kind in {"deposit", "withdrawal", "fee"}:
        symbol = currency
        name = product_name or description or symbol
        isin = ""
    else:
        symbol = product_isin or product_name or currency
        name = product_name or symbol
        isin = product_isin
    return make_row(
        date=when,
        broker=SOURCE,
        type=kind,
        symbol=symbol,
        name=name,
        isin=isin,
        amount=cashflow,
        currency=currency,
        original_currency=counter if kind == "dividend" else currency,
    )


def _counter_currency(description: str, currency: str) -> str:
    for code in _CCY.findall(description.upper()):
        if code != currency and code not in {"FX"}:
            return code
    return currency


def _maybe_pair(found):
    if isinstance(found, tuple):
        return found
    return found, None


def _amount_and_currency(df: pd.DataFrame, *aliases: str) -> tuple[pd.Series | None, pd.Series | None]:
    """Return amount and currency, including exports where these two are swapped."""
    values, currency = _maybe_pair(find_column(df, *aliases, with_currency=True))
    if values is None:
        return None, None
    if not _looks_like_currency(values):
        return values, currency
    position = next((idx for idx, column in enumerate(df.columns) if column == values.name), None)
    if position is None or position + 1 >= df.shape[1]:
        return values, currency
    candidate = df.iloc[:, position + 1]
    if _looks_like_currency(candidate):
        return values, currency
    return candidate, values


def _latest_balances(
    rows: list[tuple[pd.Timestamp, int, str, float]],
    newest_first: bool,
) -> dict[str, float]:
    latest: dict[str, tuple[pd.Timestamp, int, float]] = {}
    for when, index, currency, amount in rows:
        current = latest.get(currency)
        if current is None:
            latest[currency] = (when, index, amount)
            continue
        is_later = when > current[0]
        same_time = when == current[0]
        better_index = index < current[1] if newest_first else index > current[1]
        if is_later or (same_time and better_index):
            latest[currency] = (when, index, amount)
    return {currency: amount for currency, (_, _, amount) in latest.items()}


def _currency(series: pd.Series | None, index: int, default: str = "EUR") -> str:
    value = cell_at(series, index).upper()
    if re.fullmatch(r"[A-Z]{3}", value):
        return value
    return default
