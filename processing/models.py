"""Normalized tables shared by DEGIRO, Bitvavo, and Ledger.

Every importer writes :class:`Transaction` rows. Holdings, dividends, and cash
are derived from that table; they are not read from the brokers.

Transactions
    date               When the broker booked the row.
    broker             ``degiro``, ``bitvavo``, or ``ledger``.
    type               One of :data:`TRANSACTION_TYPES`.
    symbol             Ticker. DEGIRO exports often have no ticker, so the ISIN is used.
    isin               ISO ISIN, or blank for crypto and cash.
    name               Product name from the export.
    quantity           Units of ``symbol``. Positive for buys. For ``transfer``,
                       positive means into this broker and negative means out.
                       Cash-only rows use 0.
    price              Price per unit, quoted in ``original_currency``.
    amount             Signed cash impact in ``currency``. Buys, withdrawals, and
                       fees are negative. Sells, dividends, and deposits are positive.
                       A security transfer is 0 because it does not move cash.
    fees               Absolute fee. On trades this is in ``currency`` and is already
                       included in ``amount`` when the broker total is net of fees.
                       On-chain transfer fees are denominated in the asset.
    currency           Currency of ``amount`` and of cash fees.
    original_currency  Currency ``price`` is quoted in. Same as ``currency`` when
                       the trade was not converted.
    venue              Execution venue from broker exports (for example EAM/XAMS).

How broker rows are mapped
    DEGIRO buy/sell, dividend, deposit, withdrawal, and fee keep those types.
    A DEGIRO currency conversion is a transfer in each currency.
    Bitvavo and Ledger buys and sells stay buy/sell. Euro deposits and withdrawals
    stay deposit/withdrawal. Crypto moving between Bitvavo and Ledger is a
    transfer, so the same coins are not counted twice. Staking and in-kind
    rewards are transfers in at zero cost. Cash interest is a dividend.

Holdings
    One row per instrument after transfers are netted. ``sector``, ``industry``,
    ``country``, ``exchange``, ``security_type``, and ``currency`` are filled by
    :func:`processing.enrich.enrich_holdings` from ``data/instruments.json``.
    Missing symbols are looked up in finance-database, then Yahoo Finance, then
    manual overrides. Crypto from Bitvavo and Ledger starts as ``security_type``
    ``Crypto`` and ``country`` ``Global``. ``avg_cost`` and ``current_value`` are
    in ``currency``.

Dividends
    Dividend rows copied out of transactions. ``amount`` is the cash received.

Cash
    One row per cash movement, with a running ``balance`` inside each
    broker and currency.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd

TRANSACTION_TYPES = (
    "buy",
    "sell",
    "dividend",
    "deposit",
    "withdrawal",
    "fee",
    "transfer",
)

TRANSACTION_COLUMNS = (
    "date",
    "broker",
    "type",
    "symbol",
    "isin",
    "name",
    "quantity",
    "price",
    "amount",
    "fees",
    "currency",
    "original_currency",
    "venue",
)

HOLDING_COLUMNS = (
    "symbol",
    "isin",
    "name",
    "quantity",
    "avg_cost",
    "current_value",
    "currency",
    "sector",
    "industry",
    "country",
    "exchange",
    "security_type",
)

DIVIDEND_COLUMNS = (
    "date",
    "broker",
    "symbol",
    "isin",
    "name",
    "amount",
    "currency",
)

CASH_COLUMNS = (
    "date",
    "broker",
    "currency",
    "amount",
    "balance",
)

FIAT = frozenset({"EUR", "USD", "GBP", "CHF"})


@dataclass(frozen=True)
class Transaction:
    date: datetime
    broker: str
    type: str
    symbol: str
    isin: str = ""
    name: str = ""
    quantity: float = 0.0
    price: float | None = None
    amount: float | None = None
    fees: float = 0.0
    currency: str = "EUR"
    original_currency: str = "EUR"
    venue: str = ""


@dataclass(frozen=True)
class Holding:
    symbol: str
    isin: str = ""
    name: str = ""
    quantity: float = 0.0
    avg_cost: float | None = None
    current_value: float = 0.0
    currency: str = "EUR"
    sector: str = ""
    industry: str = ""
    country: str = ""
    exchange: str = ""
    security_type: str = ""


@dataclass(frozen=True)
class Dividend:
    date: datetime
    broker: str
    symbol: str
    isin: str = ""
    name: str = ""
    amount: float = 0.0
    currency: str = "EUR"


@dataclass(frozen=True)
class CashMovement:
    date: datetime
    broker: str
    currency: str
    amount: float
    balance: float


def make_row(**kwargs: object) -> dict[str, object]:
    """Build one transaction dict. Unknown keys are rejected."""
    row: dict[str, object] = {
        "date": pd.NaT,
        "broker": "",
        "type": "",
        "symbol": "",
        "isin": "",
        "name": "",
        "quantity": 0.0,
        "price": math.nan,
        "amount": math.nan,
        "fees": 0.0,
        "currency": "EUR",
        "original_currency": "",
        "venue": "",
    }
    unknown = set(kwargs) - set(row)
    if unknown:
        raise TypeError(f"Unknown transaction fields: {', '.join(sorted(unknown))}")
    row.update(kwargs)
    return row


def empty_frame(columns: tuple[str, ...]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def records_frame(records: list[dict[str, object]] | list[Transaction], columns: tuple[str, ...]) -> pd.DataFrame:
    if not records:
        return empty_frame(columns)
    rows = [asdict(record) if isinstance(record, Transaction) else record for record in records]
    frame = pd.DataFrame.from_records(rows)
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame.loc[:, list(columns)].copy()
