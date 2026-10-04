"""Ledger Live and Ledger Wallet operations CSV.

Both apps export the same operations file. Headers are matched by name, so an
older file without Status and a current file with Status both parse.

    Operation Date, Status, Currency Ticker, Currency Name, Account Name,
    Operation Type, Operation Amount, Operation Fees, Countervalue Ticker,
    Countervalue at Operation Date, Countervalue at CSV Export

IN and OUT are transfers: coins arriving take a cost from the euro
countervalue, and coins leaving release that cost. Staking rewards arrive at
zero cost. Fees stay on the asset. Failed and pending rows are skipped.
Every coin is ``security_type`` Crypto and ``country`` Global once it becomes
a holding.
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

SOURCE = "ledger"
SECURITY_TYPE = "Crypto"
COUNTRY = "Global"

_IN = {"IN", "RECEIVE", "RECEIVED"}
_OUT = {"OUT", "SEND", "SENT"}
_REWARD = {"REWARD", "REWARD_PAYOUT", "STAKING", "INTEREST", "CLAIM"}
_FEE = {"FEES", "FEE"}
_HOLD = {
    "DELEGATE",
    "REDELEGATE",
    "UNDELEGATE",
    "BOND",
    "UNBOND",
    "LOCK",
    "UNLOCK",
    "FREEZE",
    "UNFREEZE",
    "STAKE",
    "UNSTAKE",
    "VOTE",
    "NOMINATE",
    "CHILL",
    "APPROVE",
}
_SKIPPED = {"failed", "cancelled", "canceled", "rejected", "pending"}
_CONFIRMED = {"", "confirmed", "success", "successful", "completed"}


def matches(filename: str, columns: list[str]) -> bool:
    del filename
    headers = {norm_header(column) for column in columns}
    if "quote currency" in headers:
        return False
    distinctive = headers & {
        "operation date",
        "operation amount",
        "operation type",
        "currency ticker",
        "countervalue ticker",
        "operation fees",
    }
    has_asset = bool(headers & {"currency ticker", "currency", "asset"})
    return bool(distinctive) and has_asset


def security_type_for(symbol: str) -> str:
    """Ledger coins are crypto. Fiat countervalues are not a holding."""
    if not symbol or symbol in FIAT:
        return ""
    return SECURITY_TYPE


def country_for(symbol: str) -> str:
    if not security_type_for(symbol):
        return ""
    return COUNTRY


def parse(df: pd.DataFrame) -> pd.DataFrame:
    dates = find_column(df, "operation date", "date")
    status = find_column(df, "status")
    ticker = find_column(df, "currency ticker", "currency", "asset")
    currency_name = find_column(df, "currency name")
    operation = find_column(df, "operation type", "type")
    amount = find_column(df, "operation amount", "amount")
    fees = find_column(df, "operation fees", "fees", "fee")
    counter_ticker = find_column(df, "countervalue ticker")
    counter_at_date = find_column(df, "countervalue at operation date")
    counter_at_export = find_column(df, "countervalue at csv export")
    records = []
    skipped = 0
    for index in range(len(df)):
        state = cell_at(status, index).lower()
        if state in _SKIPPED or (state and state not in _CONFIRMED):
            skipped += 1
            continue
        when = parse_datetime(cell_at(dates, index))
        asset = cell_at(ticker, index).upper()
        kind = cell_at(operation, index).upper().replace(" ", "_")
        quantity = number_at(amount, index)
        if pd.isna(when) or not asset or asset in FIAT:
            skipped += 1
            continue
        if kind in _HOLD:
            continue
        fee = number_at(fees, index)
        if math.isnan(fee):
            fee = 0.0
        else:
            fee = abs(fee)
        if math.isnan(quantity):
            quantity = 0.0
        else:
            quantity = abs(quantity)
        counter = _countervalue(counter_at_date, counter_at_export, index)
        counter_code = cell_at(counter_ticker, index).upper() or "EUR"
        price = math.nan
        if not math.isnan(counter) and quantity:
            price = abs(counter) / quantity
        if kind in _IN or kind.endswith("_IN"):
            signed_quantity = quantity
        elif kind in _OUT or kind.endswith("_OUT"):
            signed_quantity = -(quantity + fee)
        elif kind in _REWARD or "REWARD" in kind or "STAKING" in kind:
            signed_quantity = quantity
            price = math.nan
        elif kind in _FEE:
            signed_quantity = -(quantity or fee)
            fee = quantity or fee
            price = math.nan
        else:
            skipped += 1
            continue
        if signed_quantity == 0:
            skipped += 1
            continue
        label = cell_at(currency_name, index) or asset
        records.append(
            make_row(
                date=when,
                broker=SOURCE,
                type="transfer",
                symbol=asset,
                name=label,
                quantity=signed_quantity,
                price=price,
                amount=0.0,
                fees=fee,
                currency=counter_code,
                original_currency=counter_code if not math.isnan(price) else asset,
            )
        )
    frame = records_to_frame(records)
    if skipped:
        frame.attrs["warning"] = f"Skipped {skipped} rows that were incomplete or not a known operation."
    return frame


def _countervalue(at_date: pd.Series | None, at_export: pd.Series | None, index: int) -> float:
    historical = number_at(at_date, index)
    if not math.isnan(historical):
        return historical
    return number_at(at_export, index)
