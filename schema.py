from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["Bug", "Feature Request", "How-To", "Performance", "Billing", "Integration", "Onboarding", "Data Loss"]
Team = Literal[
    "Tier 1 Support", "Product Engineering", "Integrations", "Performance & Reliability",
    "Identity & Security", "Billing", "Onboarding", "Product Management",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KBArticle(Model):
    doc_id: str = Field(description="Section ID from the KB tools, e.g. 'billing/billing-and-plans#how-seat-billing-works'")
    why: str


class DiscardedArticle(Model):
    doc_id: str
    reason: str
    replaced_by: str | None


class Triage(Model):
    issue_type: Category
    priority: Literal["P1", "P2", "P3", "P4"]
    team: Team
    rationale: str
    escalate: bool
    escalation_reason: str | None
    notify_account_manager: bool
    account_manager_reason: str | None
    is_duplicate: bool
    duplicate_of: str | None = Field(description="Earlier unresolved ticket ID; null unless is_duplicate")
    needs_more_info: bool
    missing_info: list[str] = Field(description="What to ask the customer for; empty unless needs_more_info")


class Decision(Model):
    account_id: str | None = Field(description="Account ID returned by find_account; null if not found")
    matched_by: Literal["account_id", "email_domain", "company_name", "not_found"]
    related_ticket_ids: list[str]
    kb_used: list[KBArticle]
    kb_discarded: list[DiscardedArticle]
    triage: Triage
    customer_reply: str
    internal_note: str


class Customer(Model):
    account_id: str | None
    company: str | None
    matched_by: str
    plan_tier: str | None
    health_status: str | None
    usage_trend: str | None
    renewal_date: str | None
    tam: str | None
    open_ticket_ids: list[str]
    escalation_notes: list[str]


class Step(Model):
    step: int
    tool: str
    input: dict[str, Any]
    reason: str | None
    result: str


class TriageOutput(Model):
    ticket_id: str
    customer: Customer
    triage: Triage
    related_ticket_ids: list[str]
    kb_used: list[KBArticle]
    kb_discarded: list[DiscardedArticle]
    customer_reply: str
    internal_note: str
    steps: list[Step]
    warnings: list[str]
