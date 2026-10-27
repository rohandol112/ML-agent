import json
import posixpath
import re
from datetime import datetime, timedelta
from pathlib import Path

OPEN = {"Open", "In Progress", "Pending Customer"}
DEPRECATED = re.compile(
    r"\b(deprecated|superseded|outdated|obsolete|retired|replaced by|no longer (?:valid|supported|applies))\b", re.I
)
LINK = re.compile(r"\]\(([^)\s]+)\)")
VERSION = re.compile(r"\(v(\d+)\)", re.I)


def parse_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) if value else None


def company_key(name):
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return "".join(w for w in words if w not in {"inc", "corp", "corporation", "co", "llc", "ltd", "group", "the"})


def slug(heading):
    return re.sub(r"[^\w\s-]", "", heading.lower()).strip().replace(" ", "-")


def load_kb(kb_dir):
    sections = {}
    for path in sorted(Path(kb_dir).rglob("*.md")):
        doc_id = path.relative_to(kb_dir).with_suffix("").as_posix()
        text = path.read_text(encoding="utf-8")
        doc_notice = None
        front = re.match(r"---\n(.*?)\n---\n", text, re.S)
        if front:
            text = text[front.end():]
            if re.search(r"^status:\s*(deprecated|superseded|archived)", front.group(1), re.I | re.M):
                doc_notice = front.group(1).replace("\n", "; ")
        title = (re.search(r"^# (.+)", text, re.M) or [None, doc_id])[1]

        for index, chunk in enumerate(re.split(r"\n(?=#{1,3} )", text.strip())):
            lines = chunk.strip().splitlines()
            heading = lines[0].lstrip("#").strip() if lines[0].startswith("#") else title
            body = [line.strip() for line in lines[1:] if line.strip()]
            if not body:
                continue
            banner = next((l for l in body[:2] if l.startswith((">", "**", "Note", "Warning", "Status"))
                           and DEPRECATED.search(l)), None)
            notice = doc_notice or (heading if DEPRECATED.search(heading) else None) or banner
            if index == 0 and banner:
                doc_notice = banner
            sections[f"{doc_id}#{slug(heading)}"] = {
                "id": f"{doc_id}#{slug(heading)}", "article": title, "heading": heading, "content": chunk.strip(),
                "status": "deprecated" if notice else "current", "evidence": notice, "replaced_by": notice, "links": [],
            }

    for section in sections.values():
        doc_id = section["id"].split("#")[0]
        section["links"] = [r for r in (_resolve(doc_id, t, sections) for t in LINK.findall(section["content"])) if r]
        notice = section["replaced_by"] or ""
        targets = LINK.findall(notice) + re.findall(r"[\w/.-]+\.md(?:#[\w-]+)?", notice)
        section["replaced_by"] = next((r for r in (_resolve(doc_id, t, sections) for t in targets) if r), None)

    versions = {}
    for section in sections.values():
        match = VERSION.search(section["heading"])
        if match:
            key = (section["id"].split("#")[0], VERSION.sub("", section["heading"]).strip())
            versions.setdefault(key, []).append((int(match.group(1)), section))
    for group in versions.values():
        newest = max(group, key=lambda item: item[0])[1]
        for _, section in group:
            if section is not newest:
                section["status"] = "deprecated"
                section["evidence"] = section["evidence"] or f"Newer version exists: {newest['heading']}"
                section["replaced_by"] = section["replaced_by"] or newest["id"]
    return sections


def _resolve(doc_id, target, sections):
    if target.startswith(("http", "mailto")):
        return None
    path, _, anchor = target.partition("#")
    candidates = [posixpath.normpath(posixpath.join(posixpath.dirname(doc_id), path)), path] if path else [doc_id]
    for doc in (c.removesuffix(".md") for c in candidates):
        if f"{doc}#{anchor}" in sections:
            return f"{doc}#{anchor}"
        if any(key.startswith(doc + "#") for key in sections):
            return doc
    return None


class Store:
    def __init__(self, data_dir="data", kb_dir="knowledge-base"):
        self.tickets = json.loads(Path(data_dir, "tickets.json").read_text(encoding="utf-8"))
        accounts = json.loads(Path(data_dir, "accounts.json").read_text(encoding="utf-8"))
        self.accounts = {a["account_id"]: a for a in accounts}
        self.kb = load_kb(kb_dir)

    def ticket(self, ticket_id):
        return next((t for t in self.tickets if t["ticket_id"] == ticket_id), None)


class Tools:
    def __init__(self, store, ticket):
        self.store = store
        self.ticket = ticket
        self.now = parse_date(ticket.get("created_at")) or datetime.now()

    def run(self, name, args):
        if name not in TOOL_NAMES:
            return {"error": f"Unknown tool {name}"}
        try:
            return getattr(self, name)(**args)
        except (TypeError, ValueError) as exc:
            return {"error": f"Bad arguments: {exc}"}

    def find_account(self, by, value):
        accounts = self.store.accounts.values()
        if by == "account_id":
            matches = [a for a in accounts if a["account_id"].lower() == value.strip().lower()]
        else:
            if by == "email_domain":
                parts = value.split("@")[-1].lower().split(".")
                value = parts[-3] if len(parts) > 2 and parts[-2] in {"co", "com", "org"} else parts[-2 if len(parts) > 1 else 0]
            key = company_key(value)
            matches = ([a for a in accounts if company_key(a["company"]) == key]
                       or [a for a in accounts if key and company_key(a["company"]).startswith(key)])
        if len(matches) == 1:
            a = matches[0]
            return {"found": True, "account_id": a["account_id"], "company": a["company"], "plan_tier": a["plan_tier"],
                    **self._company_warning(a)}
        if matches:
            return {"found": False, "ambiguous": [{"account_id": a["account_id"], "company": a["company"]} for a in matches]}
        return {"found": False, "note": f"No account matches {by}={value!r}. Try another key."}

    def get_account(self, account_id):
        account = self.store.accounts.get(account_id)
        if not account:
            return {"found": False}
        renewal = parse_date(account.get("renewal_date"))
        return {**account, "days_to_renewal": (renewal.date() - self.now.date()).days if renewal else None,
                **self._company_warning(account)}

    def _company_warning(self, account):
        company = self.ticket.get("company")
        if company and company_key(company) != company_key(account["company"]):
            return {"warning": f"This account is {account['company']}, but the ticket is from {company}. "
                               "Probably the wrong account; look it up by company name."}
        return {}

    def get_ticket_history(self, account_id=None, company=None, days=90):
        account = self.store.accounts.get(account_id) or {}
        key = company_key(account.get("company") or company)
        if not key:
            return {"error": f"Unknown account_id {account_id!r}; pass the company name instead."}
        start = self.now - timedelta(days=days)
        rows = []
        for t in self.store.tickets:
            same_customer = company_key(t.get("company")) == key
            if same_customer and t["ticket_id"] != self.ticket.get("ticket_id") and start <= parse_date(t["created_at"]) < self.now:
                rows.append({k: t.get(k) for k in ("ticket_id", "created_at", "subject", "product", "product_area",
                                                   "category", "urgency", "status")})
        return {"window_days": days, "tickets": sorted(rows, key=lambda r: r["created_at"], reverse=True)}

    def get_ticket(self, ticket_id):
        ticket = self.store.ticket(ticket_id)
        if not ticket or parse_date(ticket["created_at"]) >= self.now:
            return {"found": False}
        return ticket

    def search_kb(self, query):
        terms = set(re.findall(r"[a-z0-9_]{3,}", query.lower()))

        def score(section):
            head = f"{section['article']} {section['heading']}".lower()
            return sum(3 * (term in head) + section["content"].lower().count(term) for term in terms)

        hits = [s for s in sorted(self.store.kb.values(), key=score, reverse=True)[:5] if score(s) > 0]
        return {"results": [{"id": s["id"], "heading": s["heading"], "status": s["status"], "evidence": s["evidence"],
                             "replaced_by": s["replaced_by"], "snippet": s["content"][:300]} for s in hits]}

    def read_kb(self, doc_id):
        if doc_id in self.store.kb:
            return self.store.kb[doc_id]
        sections = [s for key, s in self.store.kb.items() if key.split("#")[0] == doc_id.removesuffix(".md")]
        if sections:
            return {"sections": sections}
        return {"found": False, "articles": sorted({key.split("#")[0] for key in self.store.kb})}


def _tool(name, description, properties, required):
    properties = {**properties, "reason": {"type": "string", "description": "Why you are making this call."}}
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": [*required, "reason"]}}}


STRING = {"type": "string"}
TOOLS = [
    _tool("find_account", "Find the customer's account by one key: account_id, email_domain (an address or domain "
          "from the ticket) or company_name. Ticket account_ids are sometimes missing or wrong; if a lookup fails, "
          "try another key.",
          {"by": {"type": "string", "enum": ["account_id", "email_domain", "company_name"]}, "value": STRING},
          ["by", "value"]),
    _tool("get_account", "Full account profile: plan tier, ARR, seats, health, usage trend, renewal date (with "
          "days_to_renewal counted from the ticket date), TAM and escalation notes.", {"account_id": STRING}, ["account_id"]),
    _tool("get_ticket_history", "Earlier tickets from the same company, created before this ticket, newest first. "
          "Use it to spot duplicates and recurring problems. Pass the account_id you found or the company name.",
          {"account_id": STRING, "company": STRING, "days": {"type": "integer", "description": "Default 90."}}, []),
    _tool("get_ticket", "One earlier ticket in full, to check whether it really is the same problem.",
          {"ticket_id": STRING}, ["ticket_id"]),
    _tool("search_kb", "Search knowledge-base sections. Every hit has a status; deprecated hits say what replaced "
          "them. Don't rely on deprecated content.", {"query": STRING}, ["query"]),
    _tool("read_kb", "Read a knowledge-base section ('billing/billing-and-plans#how-seat-billing-works') or a whole "
          "article ('billing/billing-and-plans').", {"doc_id": STRING}, ["doc_id"]),
]
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}
