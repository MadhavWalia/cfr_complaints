"""LUMI baseline captured at admission (ADR-02, LDR-0014).

C01 builds both from one row of the baseline query (``intake/sql/lumi_baseline.sql``).
Neither carries a timestamp: the observation time travels on
``IngestionRequest.baseline_observed_at``, so an unchanged profile always has the
same content-addressed ref.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from ._base import Contract
from .understanding import TemporalQuality

ACCOUNT_REF_PATTERN = r"^A[1-9][0-9]*$"


class BaselineAccount(Contract):
    """One account as the model may see it: an opaque ``A<n>`` ref, never an identifier."""

    account_ref: str = Field(pattern=ACCOUNT_REF_PATTERN)
    is_case_account: bool
    product: str | None = None
    co_brand_partner: str | None = None
    business_unit: str | None = None
    account_type: str | None = None
    status_at_case: str | None = None
    opened_month: str | None = None  # YYYY-MM
    card_replaced_month: str | None = None  # YYYY-MM, only if replaced before the case
    holder: str | None = None  # "Customer" or "Other cardholder"
    card_role: str | None = None  # e.g. "Basic", "Supplementary on A1"
    role_detail: str | None = None
    market: str | None = None


def _unique_refs(refs: list[str]) -> None:
    if len(refs) != len(set(refs)):
        raise ValueError("account refs must be unique")


class BaselineProfile(Contract):
    """The approved minimal baseline for one case (OQ-06). INTERNAL; C02 Pass 2 input, never Pass 1."""

    query_version: str = Field(min_length=1)
    purpose: str = "c02.grounding"
    # Current-state attributes; status_at_case and card_replaced_month are derived as of the
    # complaint received date, but nothing here is HISTORICAL_VERIFIED (ADR-15).
    temporal_quality: TemporalQuality = TemporalQuality.CURRENT_ONLY
    case_received_date: date | None = None
    accounts: tuple[BaselineAccount, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_case_account(self) -> BaselineProfile:
        _unique_refs([a.account_ref for a in self.accounts])
        if sum(a.is_case_account for a in self.accounts) > 1:
            raise ValueError("at most one account can be the case account")
        return self


class BaselineRefEntry(Contract):
    account_ref: str = Field(pattern=ACCOUNT_REF_PATTERN)
    rel_id: str | None = None
    cm15: str | None = None  # decrypted; null for accounts without one


class BaselineRefMap(Contract):
    """``account_ref`` -> source identifiers (OQ-11). RESTRICTED; never model-visible.

    Holds other cardholders' identifiers too (linked accounts); purpose and retention: OQ-27.
    """

    query_version: str = Field(min_length=1)
    entries: tuple[BaselineRefEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> BaselineRefMap:
        _unique_refs([e.account_ref for e in self.entries])
        return self
