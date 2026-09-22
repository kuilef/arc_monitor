"""Validated event contracts. Amounts are human-unit Decimal, timestamps are aware UTC."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

CHAIN_ID = 5042
USDC = '0x3600000000000000000000000000000000000000'
ZERO = '0x' + '0' * 40
BURN = '0x' + '0' * 36 + 'dead'


def address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise ValueError('expected a 20-byte 0x contract/wallet address')
    return value.lower()


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timestamp must include a timezone, preferably Z')
    return value.astimezone(timezone.utc)


def parse_time(value: str) -> datetime:
    return utc(datetime.fromisoformat(value.replace('Z', '+00:00')))


def iso(value: datetime) -> str:
    return utc(value).isoformat().replace('+00:00', 'Z')


def units(raw: int | str, decimals: int) -> Decimal:
    """Convert integer base units without rounding even above Decimal's default precision."""
    if isinstance(raw, bool) or not re.fullmatch(r'[0-9]+', str(raw)):
        raise ValueError('raw amount must be a nonnegative integer')
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 36:
        raise ValueError('decimals must be an integer in [0,36]')
    digits = str(raw).lstrip('0') or '0'
    return Decimal((0, tuple(int(c) for c in digits), -decimals))


Address = Annotated[str, AfterValidator(address)]
Time = Annotated[datetime, AfterValidator(utc)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal('1e78'), max_digits=114, decimal_places=36, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, le=Decimal('1e78'), max_digits=114, decimal_places=36, allow_inf_nan=False)]


class Event(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str = Field(min_length=1, max_length=256)
    token: Address
    ts: Time
    source: str = Field(min_length=1, max_length=80)


class Launch(Event):
    kind: Literal['launch'] = 'launch'
    tx: str
    block: int | None = Field(default=None, ge=0)
    creator: Address | None = None
    launchpad: str = 'unknown'
    symbol: str = ''
    name: str = ''
    pool: str | None = None
    origin: Literal['launch_event', 'first_trade'] = 'launch_event'


class Trade(Event):
    kind: Literal['trade'] = 'trade'
    tx: str
    wallet: Address
    side: Literal['buy', 'sell']
    quantity: Positive
    quote_amount: Positive
    pool: str = Field(min_length=1)
    symbol: str = ''
    identity_quality: Literal['log_index', 'row_fingerprint'] = 'log_index'

    @property
    def price(self) -> Decimal:
        return self.quote_amount / self.quantity


class Liquidity(Event):
    kind: Literal['liquidity'] = 'liquidity'
    pool: str = Field(min_length=1)
    amount_usdc: Money
    valuation: Literal['usdc', 'usd_assuming_usdc_peg'] = 'usdc'


class Holder(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    wallet: Address
    balance: Money
    role: Literal['wallet', 'pool', 'locker', 'protocol', 'burn'] = 'wallet'


class Holders(Event):
    kind: Literal['holders'] = 'holders'
    accounts: list[Holder]
    complete: bool = False
    total_supply: Positive | None = None
    circulating_supply: Positive | None = None

    @field_validator('accounts')
    @classmethod
    def unique_accounts(cls, value: list[Holder]) -> list[Holder]:
        if len({a.wallet for a in value}) != len(value):
            raise ValueError('duplicate holder addresses')
        return value


class Funding(Event):
    kind: Literal['funding'] = 'funding'
    token: Address = USDC
    wallet: Address
    funder: Address
    amount_usdc: Positive
    hub: bool = False
    # This is an observed transfer, not necessarily the wallet's first-ever funding.


class Flow(Event):
    kind: Literal['flow'] = 'flow'
    token: Address = USDC
    direction: Literal['in', 'out']
    amount_usdc: Positive
    message_key: str = Field(min_length=1)
    # Only reconciled CCTP messages should be imported; never infer the amount from MessageReceived alone.


class Risk(Event):
    kind: Literal['risk'] = 'risk'
    buy_tax_bps: int | None = Field(default=None, ge=0, le=10000)
    sell_tax_bps: int | None = Field(default=None, ge=0, le=10000)
    dangerous_permissions: bool | None = None
    lp_locked: bool | None = None
    evidence: str = ''


NormalizedEvent = Annotated[Launch | Trade | Liquidity | Holders | Funding | Flow | Risk,
                            Field(discriminator='kind')]
EVENT = TypeAdapter(NormalizedEvent)
