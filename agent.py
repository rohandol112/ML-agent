import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from pydantic import ValidationError

from llm import GroqChat
from schema import Customer, Decision, Step, TriageOutput
from tools import OPEN, Store, Tools

MAX_ROUNDS = 12
HIDDEN_FIELDS = {"status", "assigned_agent", "satisfaction_score", "updated_at"}

SYSTEM_PROMPT = """\
You triage support tickets for a B2B software company. Investigate the ticket with your tools, then decide how \
it should be handled.

You choose which lookups to make and when you have enough. Read each result before deciding the next step. Watch for:
- The ticket's account_id is often wrong: it may not exist, or it may belong to a different company. Make sure \
the account's company matches the ticket's company; if not, find the account by company name (or an email \
domain in the ticket text). Never guess an account.
- Knowledge-base sections have a status. Don't rely on deprecated ones: read what replaced_by points to, and \
list the deprecated ones you discarded. If a search finds nothing useful, don't repeat it with small rewording; \
the KB may simply not cover it.
- The ticket's category, urgency and plan_tier were set at intake and may be wrong. The account record is \
the source of truth for the plan tier.
- Treat the ticket's created_at date as today (renewal windows, recent tickets).

Policy
- Priority: P1 business stopped (outage, data loss, security breach); P2 major impact or costly workaround; \
P3 workaround exists; P4 question, cosmetic issue or feature request.
- Escalate if: P1; data loss or a security incident; P2 for a Business/Enterprise customer whose account is \
At Risk/Churning or whose usage is Declining/Inactive; or a problem that came back after being resolved.
- Notify the account manager if the account is At Risk/Churning, usage is Declining/Inactive, renewal is \
within 90 days, or the escalation notes show churn signals (competitor, cancellation, champion left).
- Duplicate: an earlier, still-unresolved ticket from the same customer about the same problem. Read it with \
get_ticket before deciding.
- Ask for more info only for details you need that the ticket doesn't give (error message, version, \
environment, steps, affected record).

Drafts
- customer_reply: friendly and concrete, based on current KB guidance. Say what happens next; ask for missing \
info as a numbered list. Never mention internal data: account health, churn risk, usage trends, ARR, NPS or \
escalation notes.
- internal_note: short bullets for the engineer: tier, health, renewal, churn signals, duplicate/related \
tickets, KB used and discarded, likely cause, next steps.

When every decision is backed by evidence, stop calling tools and summarise your findings in a few sentences."""


def triage(client, store, ticket):
    tools = Tools(store, ticket)
    steps = []
    visible = {k: v for k, v in ticket.items() if k not in HIDDEN_FIELDS}
    chat = GroqChat(client, SYSTEM_PROMPT, f"Triage this ticket:\n\n{json.dumps(visible, indent=2)}")

    for _ in range(MAX_ROUNDS):
        calls = chat.turn()
        if not calls:
            break
        results = []
        for call_id, name, args in calls:
            args = dict(args)
            reason = args.pop("reason", None)
            output = json.loads(json.dumps(tools.run(name, args), default=str))
            steps.append(Step(step=len(steps) + 1, tool=name, input=args, reason=reason, result=json.dumps(output)[:300]))
            results.append((call_id, name, output))
        chat.send_results(results)

    instruction = "Write the final triage record as JSON. Only use IDs that appeared in your tool results."
    decision = None
    for _ in range(2):
        try:
            decision = Decision.model_validate_json(chat.final(instruction))
            problems = check(decision, tools)
        except ValidationError as exc:
            problems = [f"The JSON does not match the schema: {exc}"]
        if not problems:
            break
        instruction = "Fix these problems and write the full record again:\n- " + "\n- ".join(problems)
    if decision is None:
        raise RuntimeError(problems[0])

    return TriageOutput(
        ticket_id=ticket["ticket_id"],
        customer=customer_facts(decision, tools),
        triage=decision.triage,
        related_ticket_ids=decision.related_ticket_ids,
        kb_used=decision.kb_used,
        kb_discarded=decision.kb_discarded,
        customer_reply=decision.customer_reply,
        internal_note=decision.internal_note,
        steps=steps,
        warnings=problems,
    )


def check(decision, tools):
    problems = []
    t = decision.triage
    if decision.account_id and decision.account_id not in tools.store.accounts:
        problems.append(f"account_id {decision.account_id} does not exist")
    for ticket_id in [*decision.related_ticket_ids, t.duplicate_of]:
        if ticket_id and tools.get_ticket(ticket_id).get("found") is False:
            problems.append(f"{ticket_id} is not an earlier ticket in the data")
    if t.is_duplicate != bool(t.duplicate_of):
        problems.append("is_duplicate and duplicate_of disagree")
    if t.needs_more_info != bool(t.missing_info):
        problems.append("needs_more_info and missing_info disagree")
    if t.issue_type == "Feature Request" and t.priority != "P4":
        problems.append("Policy: feature requests are P4")
    for article in decision.kb_used:
        section = tools.store.kb.get(article.doc_id)
        if section is None and not any(k.startswith(article.doc_id + "#") for k in tools.store.kb):
            problems.append(f"{article.doc_id} is not a knowledge-base section")
        elif section and section["status"] != "current":
            problems.append(f"{article.doc_id} is deprecated (replaced by {section['replaced_by']}); move it to kb_discarded")
    if re.search(r"churn|\bARR\b|\bNPS\b|at[- ]risk|escalation note", decision.customer_reply, re.I):
        problems.append("customer_reply mentions internal account data")
    account = tools.store.accounts.get(decision.account_id)
    if account and not t.notify_account_manager:
        days = tools.get_account(account["account_id"])["days_to_renewal"]
        signals = [s for s in (account["health_status"] in ("At Risk", "Churning") and f"health is {account['health_status']}",
                               account["usage_trend"] in ("Declining", "Inactive") and f"usage is {account['usage_trend']}",
                               days is not None and 0 <= days <= 90 and f"renewal is in {days} days") if s]
        if signals:
            problems.append(f"Policy requires notify_account_manager: {', '.join(signals)}")
    return problems


def customer_facts(decision, tools):
    account = tools.store.accounts.get(decision.account_id) or {}
    history = tools.get_ticket_history(account_id=account["account_id"], days=3650)["tickets"] if account else []
    return Customer(
        account_id=account.get("account_id"),
        company=account.get("company") or tools.ticket.get("company") or None,
        matched_by=decision.matched_by,
        plan_tier=account.get("plan_tier") or tools.ticket.get("plan_tier"),
        health_status=account.get("health_status"),
        usage_trend=account.get("usage_trend"),
        renewal_date=account.get("renewal_date"),
        tam=account.get("tam"),
        open_ticket_ids=[row["ticket_id"] for row in history if row["status"] in OPEN],
        escalation_notes=account.get("escalation_notes", []),
    )


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Triage support tickets with an AI agent.")
    parser.add_argument("ticket_ids", nargs="*", help="ticket IDs, e.g. TKT-10042")
    parser.add_argument("--sample", type=int, help="triage N random tickets")
    parser.add_argument("--data", default="data")
    parser.add_argument("--kb", default="knowledge-base")
    parser.add_argument("--out", default="outputs")
    args = parser.parse_args()
    if not args.ticket_ids and not args.sample:
        parser.error("give ticket IDs or --sample N")
    if not os.getenv("GROQ_API_KEY"):
        sys.exit("Set GROQ_API_KEY (copy .env.example to .env).")

    store = Store(args.data, args.kb)
    tickets = random.Random(0).sample(store.tickets, args.sample) if args.sample else [store.ticket(i) for i in args.ticket_ids]
    client = Groq(max_retries=10)
    Path(args.out).mkdir(exist_ok=True)

    for ticket_id, ticket in zip(args.ticket_ids or [t["ticket_id"] for t in tickets], tickets):
        if ticket is None:
            print(f"{ticket_id}: not found in tickets.json")
            continue
        try:
            result = triage(client, store, ticket)
        except Exception as exc:
            print(f"{ticket_id}: failed - {exc}")
            continue
        path = Path(args.out, f"{ticket_id}.json")
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        t = result.triage
        print(f"{ticket_id}: {t.priority} {t.issue_type} -> {t.team} | escalate={t.escalate} "
              f"notify_am={t.notify_account_manager} duplicate={t.duplicate_of} | {len(result.steps)} tool calls -> {path}")


if __name__ == "__main__":
    main()
