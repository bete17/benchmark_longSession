"""Generate a scripted long session for the multi-client account manager benchmark.

Deterministic for a given seed. Writes, into --out:
  session.jsonl   one user message per line: {"turn", "visit", "client", "visit_kind", "text"}
  checks.jsonl    one check per line (see grade/grade.py for how each is scored)
  facts.json      ground truth: every client's facts with their full value history
  meta.json       sizes, counts per tag, generator settings
  readable.md     the session as a human-readable script, with planted facts highlighted

The agent under test only ever sees the "text" of session.jsonl, in order, in one session.

Design notes (see ../DESIGN.md):
  * The session is built from VISITS: 2 to 4 consecutive user messages about one client with one
    objective. AG's boundary check only fires after 2 messages into a task, so a visit is never 1.
  * Facts are stated in conversation only: in pasted call notes ("home") or mentioned in passing
    during another client's visit ("embedded"). Some are later superseded.
  * Deliverables are written by the agent to deliverables/<id>.md, starting with a ```json header
    whose keys the request lists every time (format is boilerplate, not what is tested). Values are
    never given in the request, except for CTRL checks where the request restates one.
"""

import argparse
import json
import os
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------------------------
# Vocabulary

CLIENTS = ["Acme Robotics", "Beta Health", "Cobalt Freight", "Delta Learning",
           "Ember Foods", "Fjord Energy"]
FIRST = ["Dana", "Marcus", "Priya", "Elena", "Tomas", "Grace", "Hiro", "Lena", "Omar", "Sofia",
         "Ravi", "Nadia", "Felix", "Ines", "Jonah", "Mei", "Aaron", "Chloe", "Diego", "Hana"]
LAST = ["Okafor", "Lindqvist", "Raman", "Moreau", "Kowalski", "Chen", "Tanaka", "Haddad",
        "Brennan", "Alvarez", "Novak", "Sato", "Fischer", "Osei", "Varga", "Ito"]
CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD"]
TERMS = [12, 24, 36]
# (word the request will naturally use, word the client insists on instead)
STYLE_RULES = [("price increase", "plan adjustment"),
               ("discount", "loyalty credit"),
               ("outage", "service interruption"),
               ("add-on", "module"),
               ("upgrade", "tier change"),
               ("deadline", "target date")]
# What each style rule's trigger sentence asks the agent to talk about.
STYLE_TRIGGERS = {
    "price increase": "mention the 4% price increase on list rates next year",
    "discount": "make sure they understand the discount is what keeps the total flat",
    "outage": "acknowledge last month's outage and the credits we issued for it",
    "add-on": "pitch the analytics add-on briefly at the end",
    "upgrade": "suggest they consider an upgrade to the Enterprise plan",
    "deadline": "remind them about the deadline for signing",
}

FIELDS = ["contact_name", "cc_name", "currency", "seats", "term_months", "discount_pct",
          "renewal_deadline", "concession_ref", "style_rule"]
SUPERSEDABLE = ["contact_name", "cc_name", "seats", "discount_pct", "renewal_deadline"]

DELIVERABLE_TYPES = {
    # type: (header fields beyond id/client/type, client facing?)
    "renewal_quote": (["contact_name", "cc_name", "currency", "seats", "term_months",
                       "discount_pct", "concession_ref"], True),
    "followup_email": (["contact_name", "cc_name", "renewal_deadline"], True),
    "meeting_brief": (["contact_name", "seats", "term_months", "renewal_deadline",
                       "discount_pct"], False),
    "discount_approval": (["discount_pct", "seats", "currency", "term_months"], False),
}
REQUEST_OPENERS = {
    "renewal_quote": ["Draft the renewal quote for {c}.", "Can you put together {cp} renewal quote?",
                      "Time to send {c} their renewal pricing. Draft the quote."],
    "followup_email": ["Write a follow-up email to {c} after today's check-in.",
                       "Draft a short follow-up note to {c}.",
                       "I need an email to {c} recapping where we are on the renewal."],
    "meeting_brief": ["Prep me a one-page brief for my meeting with {c} tomorrow.",
                      "Put together an internal brief on {c} before my call.",
                      "Give me a prep doc for the {c} QBR."],
    "discount_approval": ["Fill out the internal discount approval request for {c}.",
                          "I need the discount approval form for {c} for finance.",
                          "Draft the deal-desk approval request for {c}."],
}
REFINEMENTS = {
    True: ["Make the tone a bit warmer, they've been a good partner.",
           "Tighten it up, keep the body under 180 words.",
           "Open with a thank-you for the last quarter.",
           "Add a line offering a call next week.",
           "Less salesy please, more matter-of-fact."],
    False: ["Add a short risks section.",
            "Bullet points instead of paragraphs.",
            "Put the key numbers at the top.",
            "Add one line on what we want out of this.",
            "Keep it to half a page."],
}

FILLER = [
    "We spent the first few minutes on their reorg, which is mostly settled now.",
    "They asked about the roadmap for reporting, I said I'd follow up with product.",
    "Usage is up a bit quarter over quarter, mostly from the ops team.",
    "There was some back and forth about SSO setup, IT is handling it on their side.",
    "They mentioned they're hiring in the Lisbon office this spring.",
    "Small complaint about dashboard load times, nothing urgent.",
    "Their admin wants a refresher training session sometime next month.",
    "We talked through the API rate limits, they're well under them.",
    "Someone from finance joined late and mostly listened.",
    "They're happy with support response times overall.",
    "Brief tangent about their annual offsite, not relevant for us.",
    "They want to see a case study from a similar company in their space.",
    "Data retention came up, I pointed them to the trust page.",
    "They asked whether the mobile app supports offline mode, I said not yet.",
    "General vibe was positive, no sign they're shopping around seriously.",
    "They'd like invoices sent to a shared AP inbox instead of a person.",
    "We confirmed the next QBR will be virtual.",
    "Their champion is busy with a product launch, so replies may be slow.",
    "They brought up the integration with their ticketing tool, which works fine.",
    "I walked them through the new permissions model, they liked it.",
    "There was a question about sandbox environments for testing.",
    "They'd like more notice before UI changes ship.",
    "Their legal team is slow, plan for extra time on any paperwork.",
    "We agreed to share usage reports monthly instead of quarterly.",
    "Some confusion about which team owns the account internally on their side.",
    "They're consolidating vendors this year but we are not on the chopping block.",
    "Their security questionnaire is due back to them by end of month.",
    "They floated a possible pilot in a second business unit later in the year.",
]

TEAMS = ["Ops", "Finance", "Sales", "Support", "Marketing", "Engineering", "Legal", "HR",
         "Procurement", "Data", "Product", "Customer Success", "Logistics", "Facilities", "Security",
         "Analytics", "Partnerships", "Field Ops", "Billing", "Research"]
REGIONS = ["NA-East", "NA-West", "EMEA", "APAC", "LATAM"]
THREAD_LINES = [
    "Thanks for the quick turnaround on this.", "Looping in a colleague who has more context.",
    "Can we push our sync to Thursday?", "Attaching the doc we discussed.",
    "Following up on the item from last week.", "Let me check internally and get back to you.",
    "Sounds good, we'll review on our side.", "Apologies for the delay, it's been a busy week.",
    "Is there a recording of the training session?", "We had a couple of questions about SSO groups.",
    "Our admin noticed the export took longer than usual.", "Can you resend the invoice to the AP inbox?",
    "We're still evaluating internally, no decision yet.", "Happy to jump on a call if easier.",
    "The team liked the new dashboard layout.", "Who should we contact about the API keys?",
    "We'd like to add two more admins.", "Please hold off on any changes until after our freeze.",
    "Confirming receipt, thanks.", "Our security team will send the questionnaire separately.",
]


def usage_report(rng, client, rows):
    """A pasted CSV usage export: realistic bulk with numbers that are NOT the facts."""
    head = f"Pasting {poss(client)} usage export from the admin console, in case it's useful:\n\n"
    lines = ["team,region,active_users,logins_30d,reports_created,last_active"]
    for _ in range(rows):
        lines.append(f"{rng.choice(TEAMS)},{rng.choice(REGIONS)},{rng.randint(1, 60)},"
                     f"{rng.randint(0, 900)},{rng.randint(0, 120)},2026-{rng.randint(9, 11):02d}-{rng.randint(1, 28):02d}")
    return head + "```\n" + "\n".join(lines) + "\n```"


def email_thread(rng, client, n):
    """A pasted email thread with generic content (no facts)."""
    people = [f"{rng.choice(FIRST)} ({client})", "me"]
    out = [f"For context, here's the recent thread with {client}:"]
    for i in range(n):
        who = people[i % 2]
        body = " ".join(rng.sample(THREAD_LINES, rng.randint(2, 4)))
        out.append(f"\n> From: {who}\n> {body}")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------

@dataclass
class FactValue:
    value: object
    turn: int            # user turn where it was stated
    visit: int
    placement: str       # "home" | "embedded" | "request" (restated in a request, CTRL)


@dataclass
class Client:
    name: str
    truth: Dict[str, object]              # final intended values, initial ones
    history: Dict[str, List[FactValue]] = field(default_factory=dict)

    def current(self, f: str) -> Optional[FactValue]:
        h = self.history.get(f)
        return h[-1] if h else None


def poss(c):
    return c + ("'" if c.endswith("s") else "'s")


def name(rng, used):
    while True:
        n = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if n not in used:
            used.add(n)
            return n


def fmt_value(f, v):
    if f == "discount_pct":
        return f"{v}%"
    if f == "style_rule":
        return v[1]
    return str(v)


def fact_sentence(rng, client, f, v, old=None):
    """A natural sentence stating fact f = v about client (old: superseded value, if any)."""
    c = client
    if f == "contact_name":
        if old:
            return rng.choice([f"{old} has left {c}; {v} is our main contact there from now on.",
                               f"Heads up, {v} replaced {old} as our point of contact at {c}."])
        return rng.choice([f"{v} is our main contact at {c} and owns the renewal on their side.",
                           f"Our point of contact at {c} is {v}."])
    if f == "cc_name":
        if old:
            return f"For {c}, stop copying {old} on pricing; copy {v} instead, they took over procurement."
        return rng.choice([f"Always cc {v} from {c} procurement on anything involving pricing.",
                           f"{v} runs procurement at {c}, so they need to be cc'd on pricing emails."])
    if f == "currency":
        return rng.choice([f"{c} is billed in {v}, so everything we send them should be in {v}.",
                           f"Reminder that {poss(c)} contract is denominated in {v}."])
    if f == "seats":
        if old:
            return f"{c} is changing their seat count from {old} to {v} for the renewal."
        return rng.choice([f"{c} wants to renew at {v} seats.", f"{c} plans to renew {v} seats."])
    if f == "term_months":
        return rng.choice([f"{c} prefers a {v}-month term.", f"{c} agreed in principle to a {v}-month term."])
    if f == "discount_pct":
        if old:
            return f"Deal desk revised {poss(c)} approved discount from {old}% to {v}%."
        return rng.choice([f"We got a {v}% discount approved for {poss(c)} renewal.",
                           f"The discount approved for {c} is {v}%."])
    if f == "renewal_deadline":
        if old:
            return f"{c} moved their signing date from {old} to {v}."
        return rng.choice([f"{c} needs the renewal signed by {v}.", f"{poss(c)} renewal has to close by {v}."])
    if f == "concession_ref":
        return (f"On the last call we promised {c} free onboarding for their new admins; "
                f"the concession is logged as {v} and has to be referenced on their quote.")
    if f == "style_rule":
        bad, good = v
        return (f"{poss(c)} comms team is particular: never say \"{bad}\" to them, always say "
                f"\"{good}\" instead.")
    raise ValueError(f)


def call_notes(rng, client, fact_lines, scale):
    n = int(rng.randint(10, 18) * scale)
    lines = rng.sample(FILLER, min(n, len(FILLER)))
    while len(lines) < n:
        lines.append(rng.choice(FILLER))
    for fl in fact_lines:
        lines.insert(rng.randint(0, len(lines)), fl)
    who = rng.choice(["my call", "this morning's call", "the check-in", "the QBR"])
    intro = rng.choice([f"Here are my raw notes from {who} with {client}:",
                        f"Pasting my notes from {who} with {client}, they're messy:"])
    return intro + "\n\n" + "\n".join(f"- {l}" for l in lines)


# ---------------------------------------------------------------------------------------------

def generate(seed=0, n_clients=4, n_visits=48, notes_scale=1.0, embed_prob=0.3,
             supersede_per_client=2, ctrl_prob=0.25, report_rows=300, thread_prob=0.5,
             same_client_prob=0.2):
    rng = random.Random(seed)
    used = set()
    names = CLIENTS[:n_clients]
    style_pool = rng.sample(STYLE_RULES, n_clients)
    clients = {}
    for i, c in enumerate(names):
        clients[c] = Client(c, {
            "contact_name": name(rng, used), "cc_name": name(rng, used),
            "currency": rng.choice(CURRENCIES), "seats": rng.randrange(40, 400, 5),
            "term_months": rng.choice(TERMS), "discount_pct": rng.randrange(5, 26),
            "renewal_deadline": f"2027-0{rng.randint(1, 6)}-{rng.randint(10, 28)}",
            "concession_ref": f"CX-{rng.randint(1000, 9999)}", "style_rule": style_pool[i],
        })

    # Visit sequence: first 2 visits per client are debriefs (shuffled); then a mix.
    seq = []
    opening = [(c, "debrief") for c in names for _ in range(2)]
    rng.shuffle(opening)
    seq += opening
    rest = n_visits - len(seq)
    late_debriefs = 2 * n_clients  # two more per client, where held-back facts and supersessions land
    kinds = ["debrief"] * late_debriefs + ["deliverable"] * (rest - late_debriefs)
    rng.shuffle(kinds)
    prev = None
    for k in kinds:
        # Usually switch client; sometimes stay (a deliverable right after that client's debrief
        # gives near-term checks to contrast with old ones).
        if prev is not None and rng.random() < same_client_prob:
            c = prev
        else:
            c = rng.choice([c for c in names if c != prev])
        seq.append((c, k))
        prev = c

    # Which facts each client introduces where. The first two debriefs state most facts; a few
    # facts are held back and introduced embedded in another client's visit or in a late debrief.
    pending = {}   # client -> list of (field, kind) still to introduce: kind "initial"|"supersede"
    for c in names:
        fs = FIELDS[:]
        rng.shuffle(fs)
        pending[c] = {"initial": fs, "supersede": rng.sample(SUPERSEDABLE, supersede_per_client)}

    messages: List[dict] = []
    checks: List[dict] = []
    visits_meta = []
    turn = 0
    deliverable_n = 0
    last_visit_of = {}

    def new_value(c, f):
        cl = clients[c]
        cur = cl.current(f).value
        if f in ("contact_name", "cc_name"):
            return name(rng, used)
        if f == "seats":
            return max(20, cur + rng.choice([-40, -25, 30, 50, 80]))
        if f == "discount_pct":
            return max(3, min(30, cur + rng.choice([-5, -3, 3, 4, 6])))
        if f == "renewal_deadline":
            y, m, d = cur.split("-")
            return f"{y}-0{min(9, int(m) + rng.randint(1, 2))}-{d}"
        raise ValueError(f)

    def state(c, f, v, placement, vidx, old=None):
        clients[c].history.setdefault(f, []).append(FactValue(v, turn, vidx, placement))
        return fact_sentence(rng, c, f, v, old)

    def pop_initial(c, k):
        out = []
        while pending[c]["initial"] and len(out) < k:
            out.append(pending[c]["initial"].pop())
        return out

    def ready_supersede(c):
        # only supersede facts already stated
        for f in list(pending[c]["supersede"]):
            if clients[c].current(f) is not None:
                pending[c]["supersede"].remove(f)
                return f
        return None

    n_debriefs_done = {c: 0 for c in names}
    for vidx, (c, kind) in enumerate(seq):
        texts = []
        planted = []   # (client, field, placement) for readable view
        n_msgs = rng.randint(2, 4)
        if kind == "debrief":
            n_debriefs_done[c] += 1
            fact_lines = []
            if n_debriefs_done[c] <= 2:
                k = 3 if n_debriefs_done[c] == 1 else 2
                fs = pop_initial(c, k)
            else:
                fs = pop_initial(c, 1)
            # late debriefs carry a supersession
            sup = ready_supersede(c) if n_debriefs_done[c] >= 3 or (vidx > len(names) * 2 and rng.random() < 0.5) else None
            turn_of_msg = turn + 1
            for f in fs:
                turn = turn_of_msg
                fact_lines.append(state(c, f, clients[c].truth[f], "home", vidx))
                planted.append((c, f, "home"))
            if sup:
                old = clients[c].current(sup).value
                nv = new_value(c, sup)
                turn = turn_of_msg
                fact_lines.append(state(c, sup, nv, "home", vidx, old=old))
                planted.append((c, sup, "home-supersede"))
            turn = turn_of_msg - 1
            texts.append(call_notes(rng, c, fact_lines, notes_scale) +
                         "\n\nCan you pull out the action items with an owner for each?")
            if report_rows:
                texts[0] += "\n\n" + usage_report(rng, c, int(report_rows * notes_scale))
            follow = ["Group them by who owns them.", "Which of these are blocking the renewal?",
                      "Turn that into a short checklist I can paste into our CRM.",
                      "Anything in there I should flag to my manager?"]
            for m in rng.sample(follow, n_msgs - 1):
                texts.append(m)
            deliverable = None
        else:
            dtype = rng.choice(list(DELIVERABLE_TYPES))
            hdr_fields, facing = DELIVERABLE_TYPES[dtype]
            deliverable_n += 1
            did = f"D{deliverable_n:02d}"
            opener = rng.choice(REQUEST_OPENERS[dtype]).format(c=c, cp=poss(c))
            ctrl_field = None
            if rng.random() < ctrl_prob:
                cands = [f for f in hdr_fields if clients[c].current(f) is not None and f != "concession_ref"]
                if cands:
                    ctrl_field = rng.choice(cands)
            if ctrl_field:
                v = clients[c].current(ctrl_field).value
                opener += f" (Reminder: {ctrl_field.replace('_', ' ')} is {fmt_value(ctrl_field, v)}.)"
            if facing:
                bad = clients[c].truth["style_rule"][0]
                opener += f" Please {STYLE_TRIGGERS[bad]}."
            if rng.random() < thread_prob:
                opener += "\n\n" + email_thread(rng, c, int(rng.randint(6, 14) * notes_scale))
            texts.append(opener)
            refs = rng.sample(REFINEMENTS[facing], n_msgs - 2)
            texts += refs
            keys = ["deliverable_id", "client", "type"] + hdr_fields
            texts.append(
                f"Good. Write the final version to deliverables/{did}.md. The file must start with a "
                f"```json block containing exactly these keys: {', '.join(keys)} "
                f"(deliverable_id is \"{did}\", type is \"{dtype}\", discount_pct and seats and "
                f"term_months as numbers, dates as YYYY-MM-DD), then the body below it.")
            deliverable = (did, dtype, hdr_fields, facing, ctrl_field)

        # Embedded facts: in some visits, mention a fact about another client in passing.
        if rng.random() < embed_prob:
            others = [o for o in names if o != c and (pending[o]["initial"] or ready_supersede_peek(clients, pending, o))]
            if others:
                o = rng.choice(others)
                sup = None
                if pending[o]["initial"]:
                    f = pending[o]["initial"].pop()
                    old = None
                    v = clients[o].truth[f]
                else:
                    f = ready_supersede(o)
                    old = clients[o].current(f).value
                    v = new_value(o, f)
                pos = rng.randint(0, len(texts) - 2) if len(texts) > 1 else 0
                turn_embed = turn + pos + 1
                saved = turn
                turn = turn_embed
                s = state(o, f, v, "embedded", vidx, old=old)
                turn = saved
                lead = rng.choice(["Unrelated, but before I forget: ", "Side note: ",
                                   "Oh, and separately, ", "Quick aside, not about this: "])
                texts[pos] = texts[pos] + "\n\n" + lead + s
                planted.append((o, f, "embedded" + ("-supersede" if old is not None else "")))

        # Emit messages; checks are scored at the final message's turn.
        first_turn = turn + 1
        for t in texts:
            turn += 1
            messages.append({"turn": turn, "visit": vidx, "client": c, "visit_kind": kind, "text": t})
        if deliverable:
            did, dtype, hdr_fields, facing, ctrl_field = deliverable
            for f in hdr_fields:
                cur = clients[c].current(f)
                if cur is None:
                    continue   # not yet introduced: header value is unknowable, not graded
                hist = clients[c].history[f]
                prev_vis = last_visit_of.get(c)
                stated_visit = cur.visit
                other_visits = sum(1 for (cc, _) in seq[stated_visit + 1:vidx] if cc != c)
                checks.append({
                    "check_id": f"{did}.{f}", "deliverable_id": did, "client": c, "type": dtype,
                    "kind": "field", "field": f, "expected": cur.value,
                    "superseded_values": [h.value for h in hist[:-1] if h.value != cur.value],
                    "turn": turn, "stated_turn": cur.turn, "age_turns": turn - cur.turn,
                    "placement": cur.placement, "other_visits_since_stated": other_visits,
                    "visits_since_client_last_seen": (vidx - prev_vis - 1) if prev_vis is not None else None,
                    "tags": tags_for(f == ctrl_field, len(hist) > 1, turn - cur.turn,
                                     (vidx - prev_vis - 1) if prev_vis is not None else None),
                })
            if facing and clients[c].current("style_rule") is not None:
                cur = clients[c].current("style_rule")
                bad, good = cur.value
                other_visits = sum(1 for (cc, _) in seq[cur.visit + 1:vidx] if cc != c)
                checks.append({
                    "check_id": f"{did}.style", "deliverable_id": did, "client": c, "type": dtype,
                    "kind": "style", "field": "style_rule", "expected": {"include": good, "exclude": bad},
                    "superseded_values": [], "turn": turn, "stated_turn": cur.turn,
                    "age_turns": turn - cur.turn, "placement": cur.placement,
                    "other_visits_since_stated": other_visits,
                    "visits_since_client_last_seen": (vidx - last_visit_of[c] - 1) if c in last_visit_of else None,
                    "tags": tags_for(False, False, turn - cur.turn,
                                     (vidx - last_visit_of[c] - 1) if c in last_visit_of else None),
                })
        visits_meta.append({"visit": vidx, "client": c, "kind": kind, "first_turn": first_turn,
                            "last_turn": turn, "deliverable": deliverable[0] if deliverable else None,
                            "planted": planted})
        last_visit_of[c] = vidx

    return clients, messages, checks, visits_meta


def ready_supersede_peek(clients, pending, o):
    return any(clients[o].current(f) is not None for f in pending[o]["supersede"])


A_AGE = 40      # turns: a fact this old counts as "early fact, needed late"
C_VISITS = 2    # visits to other clients since this client was last seen: "returning after switching away"


def tags_for(is_ctrl, superseded, age, away_visits):
    if is_ctrl:
        return ["CTRL"]
    t = []
    if age >= A_AGE:
        t.append("A")
    if away_visits is not None and away_visits >= C_VISITS:
        t.append("C")
    if superseded:
        t.append("D")
    return t or ["NEAR"]


# ---------------------------------------------------------------------------------------------

def readable(messages, visits_meta, clients):
    out = ["# Scripted session (readable view)\n",
           "Planted facts are listed under each visit. The agent sees only the message text.\n"]
    by_visit = {}
    for m in messages:
        by_visit.setdefault(m["visit"], []).append(m)
    for v in visits_meta:
        out.append(f"\n## Visit {v['visit']}: {v['client']} ({v['kind']}"
                   + (f", writes {v['deliverable']}" if v['deliverable'] else "") + ")\n")
        if v["planted"]:
            out.append("Planted: " + "; ".join(f"{c} {f} [{p}]" for c, f, p in v["planted"]) + "\n")
        for m in by_visit[v["visit"]]:
            out.append(f"\n**Turn {m['turn']}**\n\n{m['text']}\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--visits", type=int, default=48)
    ap.add_argument("--notes-scale", type=float, default=1.0,
                    help="multiplies filler lines in call notes, to tune session size")
    ap.add_argument("--report-rows", type=int, default=300,
                    help="rows in each pasted usage export (main lever for session size)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    clients, messages, checks, visits = generate(a.seed, a.clients, a.visits, a.notes_scale,
                                                report_rows=a.report_rows)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "session.jsonl"), "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")
    with open(os.path.join(a.out, "checks.jsonl"), "w") as f:
        for c in checks:
            f.write(json.dumps(c) + "\n")
    with open(os.path.join(a.out, "facts.json"), "w") as f:
        json.dump({c: {k: [vars(h) for h in v] for k, v in cl.history.items()}
                   for c, cl in clients.items()}, f, indent=1)
    with open(os.path.join(a.out, "visits.json"), "w") as f:
        json.dump(visits, f, indent=1)
    with open(os.path.join(a.out, "readable.md"), "w") as f:
        f.write(readable(messages, visits, clients))
    chars = sum(len(m["text"]) for m in messages)
    tagc = {}
    for c in checks:
        for t in c["tags"]:
            tagc[t] = tagc.get(t, 0) + 1
    plc = {}
    for c in checks:
        plc[c["placement"]] = plc.get(c["placement"], 0) + 1
    meta = {"seed": a.seed, "clients": a.clients, "visits": a.visits, "notes_scale": a.notes_scale,
            "user_messages": len(messages), "deliverables": len({c["deliverable_id"] for c in checks}),
            "checks": len(checks), "checks_by_tag": tagc, "checks_by_placement": plc,
            "user_text_chars": chars, "user_text_tokens_est": chars // 4}
    with open(os.path.join(a.out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
