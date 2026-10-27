# Support Ticket Triage Agent

An AI agent that triages one support ticket at a time. The model (`openai/gpt-oss-120b` on Groq) calls
tools in a loop to find the customer, check their account and ticket history, and search the knowledge base.
Then it decides the issue type, priority, team and flags, drafts a customer reply and an internal
note, and writes a JSON record with a log of every step.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env              # add GROQ_API_KEY
# copy the starter repo's data/ and knowledge-base/ folders into this folder
```

## Run

```bash
python agent.py TKT-10042 TKT-10050    # specific tickets -> outputs/<ticket_id>.json
python agent.py --sample 10            # 10 random tickets
python check_output.py outputs/        # validate the output files
```

Groq's free tier allows 8,000 tokens per minute, counting each request's input and maximum output.
So expect 1–2 minutes per ticket. On a paid plan, set `GROQ_TOKEN_LIMIT` higher. Set `GROQ_MODEL`
to use a different model.

## Files

| File | What it does |
|---|---|
| `agent.py` | Agent loop, system prompt (triage policy), validation, CLI |
| `llm.py` | Groq chat session: tool calls, tool results, final JSON answer |
| `tools.py` | Data and knowledge-base loading, and the 6 tools the model can call |
| `schema.py` | Output schema (Pydantic) |
| `check_output.py` | Check script for the output JSON |

## How it works

1. **The agent loop.** The model gets the ticket and six tools: `find_account`, `get_account`,
   `get_ticket_history`, `get_ticket`, `search_kb` and `read_kb`.
   - Each round, the model either calls tools or stops.
   - The code runs whatever the model asks for and sends the results back.
   - No order of lookups is hard-coded. The model decides what to look up next and when it has enough.
   - Rounds are capped at 12 as a safety limit.
2. **The final answer.** When the model stops, it writes the triage record as JSON, constrained to
   the `Decision` schema with Groq's strict `json_schema` mode. If the JSON still doesn't parse, that
   counts as a problem for the repair round below.
3. **Validation and repair.** The answer is checked against the data:
   - every account and ticket ID it cites exists;
   - a duplicate is an earlier ticket, not the current one or a later one;
   - no cited KB section is deprecated;
   - the flags are consistent, and follow the data-based parts of the policy (feature requests are
     P4; At Risk/Churning, Declining/Inactive or renewal within 90 days means notify the account manager);
   - the customer reply contains no internal account data.

   If anything fails, the model gets one repair round. Anything still failing is listed in `warnings`.
4. **The output file.** The step log and the customer facts (tier, health, renewal, TAM, open tickets)
   are filled in by code from the data, not written by the model.

| Task step | Where it is handled |
|---|---|
| 1. Identify the customer | `find_account` takes one key per call: account_id, email domain or company name. The model retries with another key when one fails. |
| 2. Customer background | `get_account` (tier, health, usage trend, renewal with days to go, escalation notes), `get_ticket_history`, `get_ticket` |
| 3. Knowledge base | `search_kb` and `read_kb`. Every section carries a `status` and a `replaced_by`. |
| 4. Triage decisions | The model decides, following the policy in the system prompt |
| 5. Customer reply and internal note | `customer_reply`, `internal_note` |
| 6. Step log | `steps`: tool, input, the model's stated reason, and the result, recorded in order by the loop |

## Design notes

- **Outdated KB articles.** A section is marked deprecated in four cases:
  - the article's front matter says `status: deprecated`;
  - its heading says so;
  - a banner at the top of the section says so (`> **Deprecated:** use [X](...)`);
  - the same heading exists with a newer `(vN)`.

  The replacement is resolved from the link or path, so the model can read it next. An article that only
  mentions "deprecated" in its body text stays current. (The provided KB has no deprecated articles; its
  only "Deprecated" lines are product versions past end-of-life, which the model reads as content.)
- **Dates.** The data is a fixed snapshot, so the ticket's `created_at` counts as "today". Renewal windows
  and ticket history are computed from it, and tickets created after the current one are never shown.
- **No hindsight.** Fields that describe what happened after intake (`status`, `assigned_agent`,
  `satisfaction_score`, `updated_at`) are hidden from the model.
- **Wrong account IDs.** In the provided data, 496 of 500 ticket `account_id`s don't exist. The other 4
  exist but belong to a different company. So:
  - the model finds the account by company name, or by an email domain if the ticket text has one;
  - `get_account` and `find_account` add a `warning` when the account's company differs from the ticket's;
  - ticket history is matched by company.

  If nothing matches, the output says `not_found` and uses the ticket's own `plan_tier`.
- **Plan tiers.** The ticket's `plan_tier` disagrees with its account's in 371 of 500 tickets, so the
  account record wins. The tiers (Starter/Professional/Business/Enterprise) are used as-is.
- **Teams.** The data defines no teams, so eight are defined in `schema.py`.
- **Model.** `openai/gpt-oss-120b` on Groq.
  - Requests that hit the per-minute token limit wait and retry.
  - Before each call, the longest tool results are shortened until the request fits the limit.
  - The final JSON call is sent without tools, because Groq doesn't support tools together with
    structured outputs.
