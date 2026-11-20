"""Generate a scripted long session for the multi-client account manager benchmark.

Deterministic for a given seed. Writes, into --out:
  session.jsonl   one user message per line: {"turn", "visit", "client", "visit_kind", "text"}
  checks.jsonl    one check per line (see grade/grade.py for how each is scored)
  facts.json      ground truth: every client's facts with their full value history
  meta.json       sizes, counts per tag, generator settings
  readable.md     the session as a human-readable script, with planted facts highlighted

The agent under test only ever sees the "text" of session.jsonl, in order, in one session.

Two schedules (--schedule, default v2):
  v1  the original pilot design: facts front-loaded, checks at whatever age the visit order gives.
  v2  fixed-age check schedule: facts planted throughout the session, some buried in pasted material,
      and every deliverable carries at least one fresh (2 to 10 turns old), one medium (15 to 45) and
      one old (60+) fact check, so fact age and session position can be separated. See DESIGN.md.

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
import math
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
    # Neutral wording: the request never uses the banned word itself, but describes a topic where
    # the agent would naturally reach for it. So the check tests whether the agent REMEMBERS the
    # client's rule, not whether it resolves a conflict between an old rule and a new instruction.
    "price increase": "mention that list rates go up 4% next year",
    "discount": "make sure they understand the reduced rate we approved is what keeps their total flat",
    "outage": "acknowledge last month's service problems and the credits we issued for them",
    "add-on": "briefly pitch the optional analytics package they can bolt on",
    "upgrade": "suggest they consider moving up to the Enterprise plan",
    "deadline": "remind them of the date they need to sign by",
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
    sentence: str = ""   # the exact sentence that stated it (for checking if it was in context)


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
    counts = {c: 2 for c in names}
    for k in kinds:
        # Usually switch client; sometimes stay (a deliverable right after that client's debrief
        # gives near-term checks to contrast with old ones).
        if prev is not None and rng.random() < same_client_prob:
            c = prev
        else:
            # balanced: among clients other than prev, pick one of the least visited so far
            cands = [c for c in names if c != prev]
            low = min(counts[c] for c in cands)
            c = rng.choice([c for c in cands if counts[c] == low])
        counts[c] += 1
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
        sent = fact_sentence(rng, c, f, v, old)
        clients[c].history.setdefault(f, []).append(FactValue(v, turn, vidx, placement, sent))
        return sent

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
                f"term_months as numbers, dates as YYYY-MM-DD, and null for any value I haven't told you), "
                f"then the body below it.")
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
                    # Not stated yet: the right answer is null. Anything else is made up.
                    checks.append({
                        "check_id": f"{did}.{f}", "deliverable_id": did, "client": c, "type": dtype,
                        "kind": "unknown", "field": f, "expected": None, "superseded_values": [],
                        "turn": turn, "stated_turn": None, "age_turns": None, "placement": "none",
                        "other_visits_since_stated": None,
                        "visits_since_client_last_seen": (vidx - last_visit_of[c] - 1) if c in last_visit_of else None,
                        "tags": ["UNK"],
                    })
                    continue
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


# =============================================================================================
# Schedule v2: fixed-age check schedule
#
# The plan is built in two layers. First the SKELETON: visits (client, kind, length) and which
# visits end in a deliverable, so every checkpoint turn is known up front. Then the STATEMENT PLAN:
# for every (client, field) the turns at which a value is stated. Values and sentences are filled in
# afterwards, in time order, so inserting a statement into the plan never invalidates another one.
# A deliverable is "satisfied" when its header holds at least one fresh, one medium and one old fact
# (the medium and old bins only once the session is long enough). A local search adds statements
# until every deliverable is satisfied, then adds extra statements to reach the target totals.
# =============================================================================================

from bisect import bisect_left
from collections import defaultdict

V2_TYPES = {
    # Wider headers than v1 so that every type holds fields of all three ages.
    "renewal_quote": (["contact_name", "cc_name", "currency", "seats", "term_months",
                       "discount_pct", "concession_ref"], True),
    "followup_email": (["contact_name", "cc_name", "renewal_deadline", "seats", "currency",
                        "concession_ref"], True),
    "meeting_brief": (["contact_name", "seats", "term_months", "renewal_deadline",
                       "discount_pct", "currency"], False),
    "discount_approval": (["discount_pct", "seats", "currency", "term_months", "renewal_deadline",
                           "cc_name"], False),
}
CHURN = ["contact_name", "cc_name", "seats", "discount_pct", "renewal_deadline"]
STABLE = ["currency", "term_months", "concession_ref", "style_rule"]   # stated once, never changed

FRESH = (2, 10)
MEDIUM = (15, 45)
OLD_MIN = 60
MEDIUM_REQUIRED_FROM = 20   # deliverables at turn >= this must carry a medium check
OLD_REQUIRED_FROM = 66      # deliverables at turn >= this must carry an old check
MIN_GAP = 6                 # turns between two statements of the same field
CTRL_SHARE = 0.25
BURIED_SHARE = 0.285


def age_bin_of(age):
    if age is None:
        return None
    if FRESH[0] <= age <= FRESH[1]:
        return "fresh"
    if MEDIUM[0] <= age <= MEDIUM[1]:
        return "medium"
    if age >= OLD_MIN:
        return "old"
    return "other"


def session_bin_of(turn):
    return (turn - 1) // 30


# Balance criteria (schedule v2, Nov 19): inside each age bin (fresh, medium, old; CTRL and UNK left out) the
# mix of field group, placement and form must not depend on the age bin.
BAL_BINS = ("fresh", "medium", "old")
MAX_GROUP_SPREAD = 0.15     # share of checks on SUPERSEDABLE fields, max minus min over the three bins
MAX_PLACE_SPREAD = 0.10     # share of checks whose fact was said in another client's visit
MAX_FORM_SPREAD = 0.10      # share of checks whose fact was buried in pasted material
GEN_MARGIN = 0.85           # the generator aims inside this fraction of each limit
MIN_SUPERSESSIONS = 30


def balance_stats(checks):
    """Per age bin: n, share supersedable, share embedded, share buried; and the field x age_bin counts.
    checks: check dicts (kind field or style, CTRL left out)."""
    main = [c for c in checks if c["kind"] in ("field", "style") and "CTRL" not in c["tags"]
            and c.get("scope", "client") == "client"]     # shared (G) checks are reported separately
    out = {"bins": {}, "fields": {f: {b: 0 for b in BAL_BINS} for f in FIELDS}}
    for b in BAL_BINS:
        x = [c for c in main if c["age_bin"] == b]
        n = len(x)
        out["bins"][b] = {
            "n": n,
            "sup": sum(c["field"] in SUPERSEDABLE for c in x) / n if n else 0.0,
            "emb": sum(c["placement"] == "embedded" for c in x) / n if n else 0.0,
            "bur": sum(c["form"] == "buried" for c in x) / n if n else 0.0}
        for c in x:
            out["fields"][c["field"]][b] += 1
    return out


def balance_problems(checks, scale=1.0):
    """Violations of the three balance criteria (scale < 1 tightens the limits)."""
    st = balance_stats(checks)
    probs = []
    for key, lim, label in (("sup", MAX_GROUP_SPREAD, "supersedable share"),
                            ("emb", MAX_PLACE_SPREAD, "embedded share"),
                            ("bur", MAX_FORM_SPREAD, "buried share")):
        vals = [st["bins"][b][key] for b in BAL_BINS]
        if max(vals) - min(vals) > lim * scale:
            probs.append(f"{label} spread {max(vals) - min(vals):.1%} over bins (limit {lim * scale:.1%}): "
                         + ", ".join(f"{b} {v:.1%}" for b, v in zip(BAL_BINS, vals)))
    for f, row in st["fields"].items():
        if sum(1 for b in BAL_BINS if row[b] > 0) < 2:
            probs.append(f"field {f} appears in fewer than two age bins: {row}")
    return probs


@dataclass
class FactValueV2(FactValue):
    form: str = "sentence"   # "sentence" (stated outright) | "buried" (inside pasted notes or a thread)


EMAIL_LEADS = ["Please note: ", "One more thing: ", "FYI, ", "Quick update from our side: "]


def email_sentence(rng, client, f, v, old=None):
    """The same fact as it would read inside an email from the client's side (buried form)."""
    c = client
    if f == "contact_name":
        if old:
            return rng.choice([f"{old} has moved to another team, so {v} will be your contact at {c} from now on.",
                               f"Please address the renewal to {v} instead of {old}."])
        return rng.choice([f"Please address everything on the renewal to {v}, who owns it for us.",
                           f"{v} is your contact for the renewal on our side."])
    if f == "cc_name":
        if old:
            return f"{old} no longer handles procurement here, please cc {v} on anything about pricing."
        return rng.choice([f"Please keep {v} from procurement in copy on anything about pricing.",
                           f"Our procurement lead is {v}, copy them on pricing emails."])
    if f == "currency":
        return rng.choice([f"Just a reminder that we pay in {v}, so please quote everything in {v}.",
                           f"Our finance team can only process invoices in {v}."])
    if f == "seats":
        if old:
            return f"We have revised our seat count for the renewal from {old} to {v}."
        return rng.choice([f"We are planning to renew with {v} seats.", f"Our headcount plan comes to {v} seats."])
    if f == "term_months":
        return rng.choice([f"We would like a {v}-month term this time.", f"Legal prefers a {v}-month term."])
    if f == "discount_pct":
        if old:
            return f"Our procurement team has updated its working assumption on the discount from {old}% to {v}%."
        return rng.choice([f"Our procurement team is working on the basis of a {v}% discount.",
                           f"Finance has penciled in a {v}% discount for the renewal."])
    if f == "renewal_deadline":
        if old:
            return f"Our signing date has moved from {old} to {v}."
        return rng.choice([f"We need the paperwork signed by {v}.", f"Our approval window closes on {v}."])
    if f == "concession_ref":
        return (f"For reference, the onboarding concession promised on the last call is logged on our side "
                f"as {v}; please reference it on the quote.")
    if f == "style_rule":
        bad, good = v
        return f"A note from our comms team: please never use the word \"{bad}\" with us, say \"{good}\" instead."
    raise ValueError(f)


def new_value_v2(rng, f, prev, used):
    """A fresh value for field f, different from every earlier value (prev: list of them)."""
    for _ in range(200):
        if f in ("contact_name", "cc_name"):
            v = name(rng, used)
        elif f == "seats":
            v = max(20, prev[-1] + rng.choice([-40, -25, -15, 20, 30, 50, 60, 80]))
        elif f == "discount_pct":
            v = max(3, min(30, prev[-1] + rng.choice([-5, -3, -2, 2, 3, 4, 6])))
        elif f == "renewal_deadline":
            v = f"2027-0{rng.randint(1, 9)}-{rng.randint(10, 28)}"
        elif f == "term_months":
            v = rng.choice(TERMS)
        else:
            raise ValueError(f)
        if v not in prev:
            return v
    raise RuntimeError("could not draw a new value for " + f)


@dataclass
class Visit:
    idx: int
    client: str
    kind: str            # "debrief" | "deliverable"
    n: int               # messages
    first: int = 0
    last: int = 0
    dtype: Optional[str] = None
    did: Optional[str] = None
    hdr: Optional[list] = None
    facing: bool = False
    ctrl_field: Optional[str] = None
    shared_hdr: list = field(default_factory=list)   # shared header fields this deliverable carries
    shared_std: list = field(default_factory=list)   # shared standards checked in its body


def build_skeleton(rng, names, n_deliv, n_debriefs, msg_range=(146, 150)):
    n_open = len(names)
    extra = n_debriefs - n_open
    for _ in range(5000):
        ins = sorted({int((j + 1) * n_deliv / (extra + 1) + rng.uniform(-1.2, 1.2)) for j in range(extra)})
        if len(ins) != extra or ins[0] < 1 or ins[-1] >= n_deliv:
            continue
        kinds = ["debrief"] * n_open
        for i in range(n_deliv):
            kinds.append("deliverable")
            if (i + 1) in ins:
                kinds.append("debrief")
        sizes = []
        for i, k in enumerate(kinds):
            if k == "debrief":
                sizes.append(2 if i < n_open else rng.choice([2, 3, 3, 4]))
            else:
                sizes.append(rng.choice([2, 3, 3, 3, 4, 4]))
        if not msg_range[0] <= sum(sizes) <= msg_range[1]:
            continue
        # clients
        order = names[:]
        rng.shuffle(order)
        clients = order[:]
        dcount = {c: 0 for c in names}
        bcount = {c: 0 for c in names}
        for i in range(n_open, len(kinds)):
            prev, pk = clients[-1], kinds[i - 1]
            k = kinds[i]
            cnt = dcount if k == "deliverable" else bcount
            stay = 0.35 if (pk == "debrief" and k == "deliverable") else 0.08
            if rng.random() < stay:
                c = prev
            else:
                cands = [c for c in names if c != prev]
                low = min(cnt[c] for c in cands)
                c = rng.choice([c for c in cands if cnt[c] == low])
            cnt[c] += 1
            clients.append(c)
        types = list(V2_TYPES)
        tlist = []
        while len(tlist) < n_deliv:
            blk = types[:]
            rng.shuffle(blk)
            tlist += blk
        visits, turn, dn = [], 0, 0
        for i, (k, n, c) in enumerate(zip(kinds, sizes, clients)):
            v = Visit(i, c, k, n, turn + 1, turn + n)
            turn += n
            if k == "deliverable":
                v.dtype = tlist[dn]
                dn += 1
                v.did = f"D{dn:02d}"
                v.hdr, v.facing = V2_TYPES[v.dtype]
            visits.append(v)
        first_d = next(v for v in visits if v.kind == "deliverable")
        if first_d.last > 11:
            continue
        return visits
    raise RuntimeError("no skeleton")


class Plan:
    """Statement plan: stm[(client, field)] = sorted turns at which a value of that field is stated."""

    def __init__(self, rng, names, visits):
        self.rng, self.names, self.visits = rng, names, visits
        self.n_turns = visits[-1].last
        self.vis = {}
        for v in visits:
            for t in range(v.first, v.last + 1):
                self.vis[t] = v
        self.delivs = [v for v in visits if v.kind == "deliverable"]
        self.by_client = defaultdict(list)
        for d in self.delivs:
            self.by_client[d.client].append(d)
        self.stm = defaultdict(list)
        self.cap = defaultdict(lambda: [0, 0])      # turn -> [home, embedded] statements

    # ---- slots
    def pos(self, t):
        return t - self.vis[t].first + 1

    def is_home(self, t, c):
        return self.vis[t].client == c

    def buried_capable(self, t, c):
        v, p = self.vis[t], self.pos(t)
        return p == 1 and (v.kind == "debrief" or v.client != c)

    def slot_ok(self, t, c, f):
        v, p = self.vis[t], self.pos(t)
        if v.kind == "deliverable":
            if p == v.n:
                return False        # the final "write the file" message states nothing
            if p == 1 and c == v.client:
                return False        # a client's own fact never rides in its deliverable's request
        home = v.client == c
        h, e = self.cap[t]
        cap_h, cap_e = (5, 2) if (v.kind == "debrief" and p == 1) else (1, 2)
        if (home and h >= cap_h) or (not home and e >= cap_e):
            return False
        return all(abs(x - t) >= MIN_GAP for x in self.stm[(c, f)])

    # ---- mutation
    def add(self, c, f, t):
        self.stm[(c, f)].append(t)
        self.stm[(c, f)].sort()
        self.cap[t][0 if self.is_home(t, c) else 1] += 1

    def remove(self, c, f, t):
        self.stm[(c, f)].remove(t)
        self.cap[t][0 if self.is_home(t, c) else 1] -= 1

    # ---- evaluation
    def bins(self, d, exclude=None):
        out = set()
        for f in d.hdr:
            if f == exclude:
                continue
            ts = self.stm.get((d.client, f))
            if not ts:
                continue
            i = bisect_left(ts, d.last) - 1
            if i >= 0:
                b = age_bin_of(d.last - ts[i])
                # a fresh fact must come from this visit (not its request) or the one just before
                if b == "fresh" and (self.vis[ts[i]].idx not in (d.idx, d.idx - 1) or ts[i] == d.first):
                    b = "other"
                out.add(b)
        return out

    @staticmethod
    def required(d):
        r = {"fresh"}
        if d.last >= MEDIUM_REQUIRED_FROM:
            r.add("medium")
        if d.last >= OLD_REQUIRED_FROM:
            r.add("old")
        return r

    def missing(self, d, exclude=None):
        return self.required(d) - self.bins(d, exclude)

    def client_missing(self, c, upto=10 ** 9):
        return sum(len(self.missing(d)) for d in self.by_client[c] if d.last <= upto)

    def all_ok(self):
        return all(not self.missing(d) for d in self.delivs)

    def count(self):
        return sum(len(v) for v in self.stm.values())

    def supersessions(self):
        return sum(max(0, len(v) - 1) for v in self.stm.values())

    def thirds(self):
        out = [0, 0, 0]
        for (c, f), ts in self.stm.items():
            for t in ts:
                out[min(2, (t - 1) * 3 // self.n_turns)] += 1
        return out

    def embedded(self):
        return sum(1 for (c, f), ts in self.stm.items() for t in ts if not self.is_home(t, c))

    def can_insert(self, c, f, t, lo=None, hi=None):
        """t must be the newest statement of (c, f) below `hi` (so it is the value a checkpoint sees)."""
        ts = self.stm[(c, f)]
        if f in STABLE and ts:
            return False
        if hi is not None and any(t < x < hi for x in ts):
            return False
        return self.slot_ok(t, c, f)

    def weight(self, t, c, f):
        w = 1.0
        if not self.is_home(t, c):
            w *= 0.12
        if self.buried_capable(t, c):
            w *= 1.5
        if f in CHURN and any(x < t for x in self.stm[(c, f)]):
            w *= 1.8        # prefer a supersession over a first statement
        return w

    def window(self, d, bin_):
        T = d.last
        if bin_ == "fresh":
            ts = range(max(1, T - FRESH[1]), T - FRESH[0] + 1)
            return [t for t in ts if self.vis[t].idx in (d.idx, d.idx - 1)]
        if bin_ == "medium":
            return list(range(max(1, T - MEDIUM[1]), T - MEDIUM[0] + 1))
        return list(range(1, T - OLD_MIN + 1))

    def fix(self, d, bin_):
        """Add one statement that makes `bin_` available at deliverable d without breaking an earlier one."""
        c = d.client
        before = self.client_missing(c, d.last)
        cands = []
        for f in d.hdr:
            for t in self.window(d, bin_):
                if not self.can_insert(c, f, t, hi=d.last):
                    continue
                self.add(c, f, t)
                after = self.client_missing(c, d.last)
                ok = bin_ not in self.missing(d)
                self.remove(c, f, t)
                if ok and after < before:
                    cands.append((after, self.weight(t, c, f), f, t))
        if not cands:
            return False
        best = min(a for a, *_ in cands)
        pool = [x for x in cands if x[0] == best]
        _, _, f, t = self.rng.choices(pool, weights=[x[1] for x in pool])[0]
        self.add(c, f, t)
        return True


def plan_statements(rng, names, visits, n_target):
    plan = Plan(rng, names, visits)
    opening = {}
    for v in visits:
        if v.kind == "debrief" and v.client not in opening:
            opening[v.client] = v
    # Seeds: opening debriefs state most stable fields and some others.
    def seed_later(c, f, hi):
        home = rng.random() < 0.9
        for _ in range(60):
            t = rng.randint(9, hi)
            if t <= plan.n_turns and plan.is_home(t, c) == home and plan.slot_ok(t, c, f):
                plan.add(c, f, t)
                return

    for c in names:
        ov = opening[c]
        for f in FIELDS:
            p_open = 0.85 if f in ("currency", "term_months") else (0.5 if f in STABLE else 0.4)
            if rng.random() < p_open:
                for t in (ov.first, ov.first + 1):
                    if plan.slot_ok(t, c, f):
                        plan.add(c, f, t)
                        break
    # Local search: make every deliverable carry fresh, medium and old checks.
    for d in plan.delivs:
        for bin_ in ("old", "medium", "fresh"):
            while bin_ in plan.missing(d):
                if not plan.fix(d, bin_):
                    return None
    # Later first statements (a field can stay unknown for a while: UNK checks).
    for c in names:
        for f in FIELDS:
            if not plan.stm[(c, f)] and rng.random() < 0.6:
                seed_later(c, f, 50 if f in STABLE else 95)
                if plan.client_missing(c) > 0:
                    t = plan.stm[(c, f)].pop() if plan.stm[(c, f)] else None
                    if t is not None:
                        plan.cap[t][0 if plan.is_home(t, c) else 1] -= 1
    # Extras: reach the target count, supersessions spread over the whole session.
    desired = [0.30, 0.34, 0.36]
    tries = 0
    while plan.count() < n_target and tries < 6000:
        tries += 1
        th = plan.thirds()
        deficits = [max(0.05, desired[i] * n_target - th[i]) for i in range(3)]
        i = rng.choices(range(3), weights=deficits)[0]
        lo, hi = i * plan.n_turns // 3 + 1, (i + 1) * plan.n_turns // 3
        c = rng.choice(names)
        f = rng.choice(CHURN)
        home = rng.random() > 0.10
        pool = [t for t in range(lo, hi + 1) if plan.is_home(t, c) == home]
        if not pool:
            continue
        t = rng.choice(pool)
        if not plan.can_insert(c, f, t):
            continue
        plan.add(c, f, t)
        if plan.client_missing(c) > 0:
            plan.remove(c, f, t)
    return plan


def plan_rows(plan):
    """What the checks of a plan will be: rows (bin, field, embedded, buried_capable, statement key), the number of
    UNK checks, and the (deliverable, bin) requirements still missing."""
    if not hasattr(plan, "_dl"):
        plan._dl = [(d, [(f, f in d.hdr) for f in list(d.hdr) + (["style_rule"] if d.facing else [])],
                     plan.required(d)) for d in plan.delivs]
        plan._bc = {}
        plan._home = {}
    rows, unk, miss = [], 0, 0
    stm, vis, bc, home = plan.stm, plan.vis, plan._bc, plan._home
    for d, fl, req in plan._dl:
        c, T = d.client, d.last
        have = set()
        for f, in_hdr in fl:
            ts = stm.get((c, f))
            i = bisect_left(ts, T) - 1 if ts else -1
            if i < 0:
                unk += in_hdr
                continue
            t = ts[i]
            age = T - t
            b = "fresh" if 2 <= age <= 10 else "medium" if 15 <= age <= 45 else "old" if age >= 60 else "other"
            k = (t, c)
            if k not in bc:
                bc[k] = plan.buried_capable(t, c)
                home[k] = vis[t].client == c
            rows.append((b, f, not home[k], bc[k], (c, f, t)))
            if in_hdr:
                if b == "fresh" and (vis[t].idx not in (d.idx, d.idx - 1) or t == d.first):
                    b = "other"
                have.add(b)
        miss += len(req - have)
    return rows, unk, miss


def plan_cost(plan):
    """0 when every hard requirement and every balance criterion holds with margin; larger when not."""
    rows, unk, miss = plan_rows(plan)
    n = plan.count()
    th = plan.thirds()
    cost = 40.0 * miss
    cost += 8 * max(0, 83 - n) + 8 * max(0, n - 97)
    cost += 8 * max(0, MIN_SUPERSESSIONS + 2 - plan.supersessions())
    cost += 200 * max(0.0, th[0] / n - 0.37)
    sup_th = [0, 0, 0]
    for ts in plan.stm.values():
        for t in ts[1:]:
            sup_th[min(2, (t - 1) * 3 // plan.n_turns)] += 1
    cost += 6 * max(0, 7 - min(sup_th))
    cost += 400 * max(0.0, abs(plan.embedded() / n - 0.278) - 0.023) + 100 * max(0.0, plan.embedded() / n - 0.27)
    cost += 3 * max(0, 12 - unk) + 3 * max(0, unk - 45)
    by = {b: [r for r in rows if r[0] == b] for b in BAL_BINS}
    shares = {k: [] for k in ("sup", "emb", "el")}
    for b in BAL_BINS:
        x = by[b]
        m = len(x)
        cost += 2 * max(0, 48 - m)
        m = max(m, 1)
        shares["sup"].append(sum(r[1] in SUPERSEDABLE for r in x) / m)
        shares["emb"].append(sum(r[2] for r in x) / m)
        shares["el"].append(sum(r[3] for r in x) / m)
    cost += 300 * max(0.0, max(shares["sup"]) - min(shares["sup"]) - 0.07)
    cost += 300 * max(0.0, max(shares["emb"]) - min(shares["emb"]) - 0.05)
    cost += 100 * sum(max(0.0, abs(e - 0.29) - 0.05) for e in shares["emb"])
    cost += 100 * sum(max(0.0, 0.5 - e) for e in shares["el"])
    for f in FIELDS:
        cnt = [sum(1 for r in by[b] if r[1] == f) for b in BAL_BINS]
        cost += 12 * max(0, 2 - sum(1 for x in cnt if x >= 2))
    return cost


def anneal_plan(plan, rng, iters=30000, rounds=2):
    """Move, add and remove statements (keeping every slot rule) to drive plan_cost to 0."""
    cur = plan_cost(plan)
    n_t = plan.n_turns
    for it in range(iters * rounds):
        if cur < 6.0 and valid_plan(plan) and plan_balanced(plan):
            break
        temp = 3.0 * (0.03 / 3.0) ** ((it % iters) / iters)
        r = rng.random()
        allst = [(c, f, t) for (c, f), ts in plan.stm.items() for t in ts]
        if r < 0.55 and allst:
            c, f, t = rng.choice(allst)
            t2 = min(n_t, max(1, t + rng.randint(-25, 25))) if rng.random() < 0.5 else rng.randint(1, n_t)
            if t2 == t:
                continue
            plan.remove(c, f, t)
            if not plan.slot_ok(t2, c, f):
                plan.add(c, f, t)
                continue
            plan.add(c, f, t2)
            undo = lambda: (plan.remove(c, f, t2), plan.add(c, f, t))
        elif r < 0.8:
            c, f = rng.choice(plan.names), rng.choice(FIELDS)
            t2 = rng.randint(1, n_t)
            if (f in STABLE and plan.stm.get((c, f))) or not plan.slot_ok(t2, c, f):
                continue
            plan.add(c, f, t2)
            undo = lambda: plan.remove(c, f, t2)
        elif allst:
            c, f, t = rng.choice(allst)
            plan.remove(c, f, t)
            undo = lambda: plan.add(c, f, t)
        else:
            continue
        new = plan_cost(plan)
        if new <= cur or rng.random() < math.exp((cur - new) / temp):
            cur = new
        else:
            undo()
    return cur


def valid_plan(plan, n_range=(80, 100)):
    n = plan.count()
    th = plan.thirds()
    if not n_range[0] <= n <= n_range[1] or not plan.all_ok():
        return False
    if plan.supersessions() < MIN_SUPERSESSIONS or th[0] / n > 0.40:
        return False
    emb = plan.embedded() / n
    if not 0.255 <= emb <= 0.30:
        return False
    # supersessions spread over the session
    sup_by_third = [0, 0, 0]
    for (c, f), ts in plan.stm.items():
        for t in ts[1:]:
            sup_by_third[min(2, (t - 1) * 3 // plan.n_turns)] += 1
    return min(sup_by_third) >= 6


def plan_balanced(plan):
    """The balance criteria on the planned checks (CTRL not yet chosen), with the generator's margin."""
    rows, unk, miss = plan_rows(plan)
    if miss or not 8 <= unk <= 50:
        return False
    by = {b: [r for r in rows if r[0] == b] for b in BAL_BINS}
    if min(len(x) for x in by.values()) < 42:
        return False
    for i, lim in ((1, MAX_GROUP_SPREAD), (2, MAX_PLACE_SPREAD)):
        sh = [sum((r[1] in SUPERSEDABLE) if i == 1 else r[2] for r in by[b]) / len(by[b]) for b in BAL_BINS]
        if max(sh) - min(sh) > lim * GEN_MARGIN:
            return False
    for b in BAL_BINS:       # level, not only spread: at most 38% embedded in every bin
        if not 0.20 <= sum(r[2] for r in by[b]) / len(by[b]) <= 0.35:
            return False
    for f in FIELDS:
        if sum(1 for b in BAL_BINS if sum(1 for r in by[b] if r[1] == f) >= 2) < 2:
            return False
    return True


def choose_buried(plan, rng, stmts, eligible, want, iters=6000):
    """Pick `want` buried statements among `eligible` so the buried share of checks is the same in every age bin."""
    rows, _, _ = plan_rows(plan)
    use = defaultdict(lambda: [0, 0, 0])        # statement key -> checks per balanced bin
    tot = [0, 0, 0]
    for b, f, e, el, key in rows:
        if b in BAL_BINS:
            use[key][BAL_BINS.index(b)] += 1
            tot[BAL_BINS.index(b)] += 1
    elig = [(c, f, t) for (t, c, f) in eligible]
    inn = set(rng.sample(elig, want))
    cnt = [sum(use[k][i] for k in inn) for i in range(3)]

    def cost(cn):
        sh = [cn[i] / max(1, tot[i]) for i in range(3)]
        return 10 * (max(sh) - min(sh)) + sum(abs(x - 0.30) for x in sh)

    cur = cost(cnt)
    out = [k for k in elig if k not in inn]
    for _ in range(iters):
        if cur < 0.02:
            break
        a = rng.choice(sorted(inn))
        b = rng.choice(out)
        new = [cnt[i] - use[a][i] + use[b][i] for i in range(3)]
        nc = cost(new)
        if nc <= cur:
            inn.remove(a)
            inn.add(b)
            out.remove(b)
            out.append(a)
            cnt, cur = new, nc
    return {(t, c, f) for (c, f, t) in inn}


# =============================================================================================
# Shared facts (schedule v2, added Nov 19 2026): knowledge that applies to EVERY client.
#
# Two kinds, both said once (some changed later) during some client's visit and checked on deliverables for all
# clients: company-wide rules (graded as JSON header fields) and the user's own standards (graded in the body,
# like style rules). Tag G, check fields scope="shared" and cross_client (the current value was said during a visit
# of a different client than the deliverable's client). The shared layer is planned and inserted AFTER the
# per-client plan, with its own random stream, so it never changes the per-client facts, checks or balance.
# =============================================================================================

from itertools import combinations, product

SHARED_HDR = ["quote_valid_days", "approver", "payment_terms", "legal_footer_code"]
SHARED_STD = ["sign_off", "next_steps", "banned_phrase"]
SHARED_FIELDS = SHARED_HDR + SHARED_STD
# Every shared statement contains one of these (lower case) so that it clearly applies to every client.
SHARED_MARKERS = ["company-wide", "for every account", "for all our renewals", "for all customers",
                  "for every customer", "for all my clients", "for every client"]
SHARED_HDR_ALLOWED = {   # header fields a deliverable type may carry, and how many it carries
    "renewal_quote": (SHARED_HDR, (2,)),
    "followup_email": (["quote_valid_days", "payment_terms", "legal_footer_code"], (1, 2)),
    "meeting_brief": (["quote_valid_days", "approver", "payment_terms"], (1, 2)),
    "discount_approval": (["approver", "payment_terms", "quote_valid_days"], (1, 2)),
}
SHARED_STD_TYPES = {     # standards checked on a deliverable type
    "sign_off": ("renewal_quote", "followup_email", "meeting_brief"),
    "next_steps": tuple(V2_TYPES),
    "banned_phrase": ("renewal_quote", "followup_email"),
}
NOTE_TO_SELF = ["Book the Friday pipeline review room.", "Submit last month's expense report.",
                "Ask my manager about the conference budget.", "Update my forecast numbers before Thursday.",
                "Reply to the recruiting survey.", "Renew the parking pass."]
SHARED_TRIGGER = ("One more thing for the close: say that we will come back to the open points once their "
                  "team has had time to review it.")
# (field, history index, target position in the session as a share of the turns)
SHARED_TARGETS = [("quote_valid_days", 0, 0.07), ("payment_terms", 0, 0.10), ("approver", 0, 0.14),
                  ("sign_off", 0, 0.19), ("legal_footer_code", 0, 0.25), ("banned_phrase", 0, 0.31),
                  ("next_steps", 0, 0.38), ("payment_terms", 1, 0.58), ("quote_valid_days", 1, 0.68),
                  ("approver", 1, 0.77), ("sign_off", 1, 0.86)]
MIN_SHARED_GAP = 3          # turns between two shared statements
MIN_SHARED_SUPERSEDE_GAP = 22


def shared_values(rng):
    """The value history of each shared field (a seeded draw from small pools)."""
    pay = rng.choice([("Net 45", "Net 30"), ("Net 30", "Net 60"), ("Net 60", "Net 45")])
    return {
        "quote_valid_days": [rng.choice([30, 45, 60]), rng.choice([14, 21])],
        "approver": [rng.choice(["VP Sales", "Head of Deal Desk", "Regional Sales Director"]),
                     rng.choice(["CFO", "VP Finance", "Chief Revenue Officer"])],
        "payment_terms": list(pay),
        "legal_footer_code": [rng.choice(["LF-2026-B", "LF-2026-C", "LF-2027-A"])],
        "sign_off": ["The Northwind Accounts Team", "Northwind Customer Success"],
        "next_steps": ["Next steps"],
        "banned_phrase": [("circle back", "pick this up again")],
    }


def shared_sentence(rng, field, k, v, old=None):
    """One sentence stating shared field `field` (history index k) with value v. Always says it covers everyone."""
    if field == "quote_valid_days":
        if k:
            return rng.choice([f"Company-wide change: from now on every quote is valid for {v} days, not {old}.",
                               f"Finance changed the policy for every account: quote validity is now {v} days instead of {old}."])
        return rng.choice([f"Company-wide: every quote we send to any customer is valid for {v} days from the date it goes out.",
                           f"For all our renewals from now on, a quote stays valid for {v} days after we send it, whichever customer it is."])
    if field == "approver":
        if k:
            return rng.choice([f"Company-wide change: sign-off on quotes and discounts now goes to the {v} instead of the {old}, for every customer.",
                               f"Finance changed the policy for every account: the approver is now the {v}, not the {old}."])
        return rng.choice([f"Company-wide: any quote or discount request, for every customer, needs sign-off from the {v}.",
                           f"For all our renewals, the person who has to sign off on pricing is the {v}, no matter which account it is."])
    if field == "payment_terms":
        if k:
            return rng.choice([f"Finance changed the policy for every account: payment terms are now {v}, replacing {old}.",
                               f"Company-wide change: payment terms for all customers move from {old} to {v}."])
        return rng.choice([f"Company-wide: our standard payment terms for all customers are {v}.",
                           f"For all our renewals from now on, we invoice on {v} terms, whoever the customer is."])
    if field == "legal_footer_code":
        return rng.choice([f"Company-wide: legal wants every customer-facing document, for any account, to carry the footer code {v}.",
                           f"For all our renewals from now on, the legal footer code is {v}, on every quote and email."])
    if field == "sign_off":
        if k:
            return rng.choice([f"Change to my standing sign-off, for all my clients: use \"{v}\" instead of \"{old}\".",
                               f"From now on, for every client, I sign off as \"{v}\" and no longer as \"{old}\"."])
        return rng.choice([f"Standing rule for all my clients: end every email, quote and brief with the sign-off \"{v}\".",
                           f"For every client, I always sign off as \"{v}\", please do that on anything I send out."])
    if field == "next_steps":
        return rng.choice([f"Standing rule for every client: every document I hand over, whatever it is, has a section headed exactly \"{v}\".",
                           f"For all my clients, always include a section headed exactly \"{v}\" in anything you write for me."])
    if field == "banned_phrase":
        bad, good = v
        return rng.choice([f"Standing rule for all my clients: never write \"{bad}\" in anything you draft, say \"{good}\" instead.",
                           f"For every client, the words \"{bad}\" are banned in my documents, use \"{good}\" instead."])
    raise ValueError(field)


def shared_cur(stmts, field, T):
    """(statement, history index) of the value of `field` in force at turn T, or (None, None)."""
    hist = [s for s in stmts[field] if s["turn"] < T]
    return (hist[-1], len(hist) - 1) if hist else (None, None)


def shared_descriptor(stmts, d, field):
    """What a check of `field` on deliverable d would look like: None (field not applicable) or a small dict."""
    cur, idx = shared_cur(stmts, field, d.last)
    if cur is None:
        return None if field in SHARED_STD else {"field": field, "unk": True, "bin": None, "cross": False,
                                                  "buried": False, "d": False, "std": False}
    age = d.last - cur["turn"]
    return {"field": field, "unk": False, "bin": age_bin_of(age), "cross": cur["client"] != d.client,
            "buried": cur["form"] == "buried", "d": idx > 0, "std": field in SHARED_STD}


def shared_options(stmts, d):
    """Every way to pick the shared header fields and standards checked on deliverable d."""
    allowed, ks = SHARED_HDR_ALLOWED[d.dtype]
    hdr_opts = [c for k in ks for c in combinations(allowed, k)]
    std_ok = [f for f in SHARED_STD if d.dtype in SHARED_STD_TYPES[f] and shared_cur(stmts, f, d.last)[0] is not None]
    std_opts = [c for k in range(len(std_ok) + 1) for c in combinations(std_ok, k)]
    out = []
    for h, s in product(hdr_opts, std_opts):
        ds = [shared_descriptor(stmts, d, f) for f in list(h) + list(s)]
        out.append((h, s, ds))
    return out


SHARED_LIMITS = {"n": (53, 59), "bin": (13, 20), "other_max": 6, "cross": (0.70, 0.80), "cross_bin_min": 0.55,
                 "std": (18, 26), "unk": (4, 10), "same_min": 12, "d_min": 8, "field_min": 6, "buried": (0.22, 0.40),
                 "buried_bin_min": 0.12}


def shared_cost(chosen):
    """0 when the selection of shared checks meets every target; larger otherwise. chosen: list of descriptor lists."""
    L = SHARED_LIMITS
    ds = [x for lst in chosen for x in lst]
    st = [x for x in ds if not x["unk"]]
    n = len(st)
    dist = lambda v, lo, hi: max(0, lo - v) + max(0, v - hi)
    cost = 3.0 * dist(n, *L["n"])
    cnt = {b: sum(1 for x in st if x["bin"] == b) for b in BAL_BINS}
    for b in BAL_BINS:
        cost += 2.0 * dist(cnt[b], *L["bin"])
    cost += 2.0 * max(0, sum(1 for x in st if x["bin"] == "other") - L["other_max"])
    if n:
        cr = sum(x["cross"] for x in st) / n
        cost += 100 * dist(cr, *L["cross"])
        for b in BAL_BINS:
            xb = [x for x in st if x["bin"] == b]
            if xb:
                cost += 60 * max(0, L["cross_bin_min"] - sum(x["cross"] for x in xb) / len(xb))
                cost += 60 * max(0, L["buried_bin_min"] - sum(x["buried"] for x in xb) / len(xb))
        cost += 60 * dist(sum(x["buried"] for x in st) / n, *L["buried"])
    cost += 2.0 * dist(sum(1 for x in st if x["std"]), *L["std"])
    cost += 2.0 * dist(sum(1 for x in ds if x["unk"]), *L["unk"])
    cost += 2.0 * max(0, L["same_min"] - sum(1 for x in st if not x["cross"]))
    cost += 2.0 * max(0, L["d_min"] - sum(1 for x in st if x["d"]))
    for f in SHARED_FIELDS:
        fx = [x for x in st if x["field"] == f]
        cost += 3.0 * max(0, L["field_min"] - len(fx))
        cost += 4.0 * max(0, 2 - sum(1 for b in BAL_BINS if sum(1 for x in fx if x["bin"] == b) >= 2))
    return cost


def select_shared(stmts, delivs, rng, iters=2500):
    """Annealing over the per-deliverable option of shared checks. Returns (cost, {did: (hdr, std)})."""
    opts = [shared_options(stmts, d) for d in delivs]
    pick = [rng.randrange(len(o)) for o in opts]
    cur = shared_cost([o[p][2] for o, p in zip(opts, pick)])
    for it in range(iters):
        if cur == 0:
            break
        temp = 4.0 * (0.05 / 4.0) ** (it / iters)
        i = rng.randrange(len(delivs))
        old = pick[i]
        pick[i] = rng.randrange(len(opts[i]))
        new = shared_cost([o[p][2] for o, p in zip(opts, pick)])
        if new <= cur or rng.random() < math.exp((cur - new) / temp):
            cur = new
        else:
            pick[i] = old
    return cur, {d.did: (list(o[p][0]), list(o[p][1])) for d, o, p in zip(delivs, opts, pick)}


def plan_shared(rng, visits, names, tries=60):
    """Place the shared statements in visits and choose which shared checks each deliverable carries.
    Returns {"stmts": {field: [statement dicts]}, "assign": {did: (hdr fields, standards)}, "cost": float} or None."""
    n_turns = visits[-1].last
    vis = {}
    for v in visits:
        for t in range(v.first, v.last + 1):
            vis[t] = v
    slots = []                       # (turn, position in visit)
    for v in visits:
        for t in range(v.first, v.last + 1):
            p = t - v.first + 1
            if t >= 8 and not (v.kind == "deliverable" and p == v.n):
                slots.append((t, p))
    vals = shared_values(rng)
    delivs = [v for v in visits if v.kind == "deliverable"]
    best = None
    for _ in range(tries):
        n_bur = rng.choice([3, 4])
        buried_idx = set(rng.sample(range(len(SHARED_TARGETS)), n_bur))
        used, chosen, ok = [], [], True
        order = sorted(range(len(SHARED_TARGETS)), key=lambda i: SHARED_TARGETS[i][2])
        placed = {}
        for i in order:
            field, k, frac = SHARED_TARGETS[i]
            target = frac * n_turns + rng.uniform(-0.04, 0.04) * n_turns
            cands, w = [], []
            for t, p in slots:
                if any(abs(t - u) < MIN_SHARED_GAP for u in used):
                    continue
                if k and t - placed[(field, k - 1)] < MIN_SHARED_SUPERSEDE_GAP:
                    continue
                if i in buried_idx and p != 1:
                    continue
                cands.append(t)
                w.append(math.exp(-abs(t - target) / 5.0) * (3.0 if (p >= 2 and i not in buried_idx) else 1.0))
            if not cands:
                ok = False
                break
            t = rng.choices(cands, weights=w)[0]
            used.append(t)
            placed[(field, k)] = t
            chosen.append((field, k, t, i in buried_idx))
        if not ok:
            continue
        per_client = [sum(1 for (_, _, t, _) in chosen if vis[t].client == c) for c in names]
        if max(per_client) > 3:             # statements spread over the clients (so cross_client is not decided by one visit)
            continue
        stmts = {f: [] for f in SHARED_FIELDS}
        for field, k, t, bur in sorted(chosen, key=lambda x: (x[0], x[1])):
            v = vis[t]
            stmts[field].append({"field": field, "k": k, "value": vals[field][k],
                                 "old": vals[field][k - 1] if k else None, "turn": t, "visit": v.idx,
                                 "client": v.client, "pos": t - v.first + 1, "form": "buried" if bur else "sentence",
                                 "kind": "standard" if field in SHARED_STD else "rule"})
        cost, assign = select_shared(stmts, delivs, rng)
        if best is None or cost < best["cost"]:
            best = {"stmts": stmts, "assign": assign, "cost": cost}
        if cost == 0:
            break
    return best


def insert_shared(rng, text, v, p, st):
    """Return the message text with the shared statement st added (st['sentence'] is inserted verbatim)."""
    s = st["sentence"]
    if st["form"] == "buried":
        if v.kind == "debrief":
            marker = "\n\nCan you pull out the action items"
            head, sep, rest = text.partition(marker)
            lines = head.split("\n")
            idx = [i for i, l in enumerate(lines) if l.startswith("- ")]
            if sep and idx:
                lead = (rng.choice(["Note to self, this is not about this client: ", "For me, not about this client: "])
                        if st["kind"] == "standard" else
                        rng.choice(["Internal reminder from this week's team meeting, not about this client: ",
                                    "From the sales ops update this week, not about this client: "]))
                lines.insert(rng.randint(idx[0], idx[-1] + 1), "- " + lead + s)
                return "\n".join(lines) + sep + rest
            return text + "\n\n- " + s
        if st["kind"] == "standard":      # the user's own notes, not a forwarded message
            a_, b_ = rng.sample(NOTE_TO_SELF, 2)
            return (text + "\n\nPasting a few of my own working notes while I'm at it, not about this account:\n"
                    f"- {a_}\n- {s}\n- {b_}")
        who = f"{rng.choice(FIRST)} ({rng.choice(['Finance Ops', 'Sales Ops', 'Legal Ops'])})"
        pre = " ".join(rng.sample(THREAD_LINES, rng.randint(1, 2)))
        post = " ".join(rng.sample(THREAD_LINES, rng.randint(1, 2)))
        return (text + "\n\nAlso pasting an internal note from our own ops channel, not about this account:\n"
                f"> From: {who}\n> {pre} {s} {post}")
    if v.kind == "debrief" and p == 1:
        marker = "\n\nCan you pull out the action items"
        head, sep, rest = text.partition(marker)
        if sep:
            return head + "\n\n" + s + sep + rest
    return text + "\n\n" + s


def generate_v2(seed=0, n_clients=4, n_deliv=38, n_debriefs=11, notes_scale=1.0, report_rows=420,
                thread_prob=0.5, max_attempts=400):
    names = CLIENTS[:n_clients]
    for attempt in range(max_attempts):
        rng = random.Random(seed * 1_000_003 + attempt)
        visits = build_skeleton(rng, names, n_deliv, n_debriefs)
        n_target = rng.randint(84, 92)
        plan = plan_statements(rng, names, visits, n_target)
        if plan is None:
            continue
        anneal_plan(plan, rng)
        if not valid_plan(plan) or not plan_balanced(plan):
            continue
        # Forms: which statements are buried in pasted material, balanced over the age bins.
        stmts = sorted((t, c, f) for (c, f), ts in plan.stm.items() for t in ts)
        eligible = [s for s in stmts if plan.buried_capable(s[0], s[1])]
        want = round(BURIED_SHARE * len(stmts))
        if len(eligible) < want:
            continue
        buried = choose_buried(plan, rng, stmts, eligible, want)
        srng = random.Random(seed * 7_919 + attempt * 31 + 5)     # separate stream: the shared layer never moves per-client facts
        shared = plan_shared(srng, visits, names)
        if shared is None or shared["cost"] > 0:
            continue
        out = _render_v2(rng, names, visits, plan, stmts, buried, notes_scale, report_rows, thread_prob,
                         shared, srng)
        if out is None:
            continue
        if balance_problems(out[2], GEN_MARGIN):
            continue
        return out + (attempt,)
    raise RuntimeError("generate_v2: no valid plan in %d attempts" % max_attempts)


def _render_v2(rng, names, visits, plan, stmts, buried, notes_scale, report_rows, thread_prob, shared, srng):
    used = set()
    style_pool = rng.sample(STYLE_RULES, len(names))
    init = {}
    for i, c in enumerate(names):
        init[c] = {
            "contact_name": name(rng, used), "cc_name": name(rng, used),
            "currency": rng.choice(CURRENCIES), "seats": rng.randrange(40, 400, 5),
            "term_months": rng.choice(TERMS), "discount_pct": rng.randrange(5, 26),
            "renewal_deadline": f"2027-0{rng.randint(1, 6)}-{rng.randint(10, 28)}",
            "concession_ref": f"CX-{rng.randint(1000, 9999)}", "style_rule": style_pool[i]}
    clients = {c: Client(c, dict(init[c])) for c in names}

    # Values, in time order per field.
    stm_obj = {}      # (turn, client, field) -> dict(value, old, form, placement, visit)
    for (c, f), ts in sorted(plan.stm.items()):
        vals = []
        for k, t in enumerate(ts):
            v = init[c][f] if k == 0 else new_value_v2(rng, f, vals, used)
            old = vals[-1] if vals else None
            vals.append(v)
            stm_obj[(t, c, f)] = {"value": v, "old": old,
                                  "placement": "home" if plan.is_home(t, c) else "embedded",
                                  "form": "buried" if (t, c, f) in buried else "sentence",
                                  "visit": plan.vis[t].idx}
    # Sentences.
    by_turn = defaultdict(list)
    for (t, c, f), o in sorted(stm_obj.items()):
        v = plan.vis[t]
        email_voice = o["form"] == "buried" and v.kind == "deliverable"
        fn = email_sentence if email_voice else fact_sentence
        o["sentence"] = fn(rng, c, f, o["value"], o["old"])
        o["email_voice"] = email_voice
        by_turn[t].append((c, f, o))
    # Facts history.
    for (t, c, f), o in sorted(stm_obj.items()):
        clients[c].history.setdefault(f, []).append(
            FactValueV2(o["value"], t, o["visit"], o["placement"], o["sentence"], o["form"]))

    def cur_at(c, f, T):
        h = [x for x in clients[c].history.get(f, []) if x.turn < T]
        return h[-1] if h else None

    # CTRL deliverables: restate one field in the request, never the one a bin relies on.
    delivs = plan.delivs
    n_ctrl = round(CTRL_SHARE * len(delivs))
    order = delivs[:]
    rng.shuffle(order)
    ctrl = 0
    for d in order:
        if ctrl >= n_ctrl:
            break
        cands = []
        for f in d.hdr:
            if f == "concession_ref":
                continue
            cur = cur_at(d.client, f, d.last)
            if cur is None or cur.turn >= d.first:
                continue
            if not plan.missing(d, exclude=f):
                cands.append(f)
        if cands:
            d.ctrl_field = rng.choice(cands)
            ctrl += 1

    # Shared statements: sentences (own random stream) and the shared checks each deliverable carries.
    sh = shared["stmts"]
    sh_by_turn = {}
    for fld in SHARED_FIELDS:
        for st in sh[fld]:
            st["sentence"] = shared_sentence(srng, fld, st["k"], st["value"], st["old"])
            sh_by_turn[st["turn"]] = st
    for d in plan.delivs:
        d.shared_hdr, d.shared_std = shared["assign"][d.did]

    # Messages.
    messages, checks, visits_meta = [], [], []
    last_visit_of = {}
    seq = [(v.client, v.kind) for v in visits]
    for v in visits:
        c = v.client
        texts, planted = [], []
        stated = {p: by_turn.get(v.first + p - 1, []) for p in range(1, v.n + 1)}
        for p in range(1, v.n + 1):
            for (cc, f, o) in stated[p]:
                tag = o["placement"] + ("-supersede" if o["old"] is not None else "")
                planted.append((cc, f, tag, o["form"]))

        def lead_for(o):
            return rng.choice(["Unrelated, but before I forget: ", "Side note: ",
                               "Oh, and separately, ", "Quick aside, not about this: "])

        if v.kind == "debrief":
            heads, bullets, tail = [], [], []
            for (cc, f, o) in stated[1]:
                if o["form"] == "buried":
                    bullets.append(o["sentence"] if cc == c else "Side note: " + o["sentence"])
                elif cc == c:
                    heads.append(o["sentence"])
                else:
                    tail.append(lead_for(o) + o["sentence"])
            notes = call_notes(rng, c, bullets, notes_scale)
            body = notes + "\n\nCan you pull out the action items with an owner for each?"
            if heads:
                body = rng.choice(["Headlines from the call first: ", "Main takeaways before the notes: "]) \
                    + " ".join(heads) + "\n\n" + body
            if report_rows:
                body += "\n\n" + usage_report(rng, c, int(report_rows * notes_scale))
            if tail:
                body += "\n\n" + "\n\n".join(tail)
            texts.append(body)
            follow = ["Group them by who owns them.", "Which of these are blocking the renewal?",
                      "Turn that into a short checklist I can paste into our CRM.",
                      "Anything in there I should flag to my manager?"]
            for k, m in enumerate(rng.sample(follow, v.n - 1)):
                extra = []
                for (cc, f, o) in stated[k + 2]:
                    if cc == c:
                        extra.append(rng.choice(["One more thing from the call: ", "Also from the call: "]) + o["sentence"])
                    else:
                        extra.append(lead_for(o) + o["sentence"])
                texts.append(m + ("\n\n" + "\n\n".join(extra) if extra else ""))
        else:
            hdr, facing = v.hdr, v.facing
            opener = rng.choice(REQUEST_OPENERS[v.dtype]).format(c=c, cp=poss(c))
            if v.ctrl_field:
                cv = cur_at(c, v.ctrl_field, v.last).value
                opener += f" (Reminder: {v.ctrl_field.replace('_', ' ')} is {fmt_value(v.ctrl_field, cv)}.)"
            if facing:
                opener += f" Please {STYLE_TRIGGERS[clients[c].truth['style_rule'][0]]}."
                opener += " " + SHARED_TRIGGER
            if rng.random() < thread_prob:
                opener += "\n\n" + email_thread(rng, c, int(rng.randint(6, 14) * notes_scale))
            for (cc, f, o) in stated[1]:            # always another client's fact (embedded)
                if o["form"] == "buried":
                    who = f"{rng.choice(FIRST)} ({cc})"
                    pre = " ".join(rng.sample(THREAD_LINES, rng.randint(1, 2)))
                    post = " ".join(rng.sample(THREAD_LINES, rng.randint(1, 2)))
                    opener += (f"\n\nAlso, forwarding a note {poss(cc)} side just sent me, nothing to do with "
                               f"this one:\n> From: {who}\n> {pre} {o['sentence']} {post}")
                else:
                    opener += "\n\n" + lead_for(o) + o["sentence"]
            texts.append(opener)
            refs = rng.sample(REFINEMENTS[facing], v.n - 2)
            for k, r in enumerate(refs):
                extra = []
                for (cc, f, o) in stated[k + 2]:
                    if cc == c:
                        extra.append(rng.choice(["Oh, and an update from the client side: ",
                                                 "Also, they just told me: ",
                                                 "One more thing before you finalize: "]) + o["sentence"])
                    else:
                        extra.append(lead_for(o) + o["sentence"])
                texts.append(r + ("\n\n" + "\n\n".join(extra) if extra else ""))
            keys = ["deliverable_id", "client", "type"] + hdr + v.shared_hdr
            nums = "discount_pct and seats and term_months" + (" and quote_valid_days" if "quote_valid_days" in v.shared_hdr else "")
            texts.append(
                f"Good. Write the final version to deliverables/{v.did}.md. The file must start with a "
                f"```json block containing exactly these keys: {', '.join(keys)} "
                f"(deliverable_id is \"{v.did}\", type is \"{v.dtype}\", {nums} as numbers, "
                f"dates as YYYY-MM-DD, and null for any value I haven't told you), "
                f"then the body below it.")
        assert len(texts) == v.n, (v.idx, len(texts), v.n)
        for t in range(v.first, v.last + 1):
            if t in sh_by_turn:
                st = sh_by_turn[t]
                texts[t - v.first] = insert_shared(srng, texts[t - v.first], v, t - v.first + 1, st)
                planted.append(("(all clients)", st["field"], "shared" + ("-supersede" if st["k"] else ""), st["form"]))
        for k, t in enumerate(texts):
            messages.append({"turn": v.first + k, "visit": v.idx, "client": c, "visit_kind": v.kind, "text": t})
        # verbatim invariant
        for p in range(1, v.n + 1):
            for (cc, f, o) in stated[p]:
                if o["sentence"] not in texts[p - 1]:
                    return None
        for t in range(v.first, v.last + 1):
            if t in sh_by_turn and sh_by_turn[t]["sentence"] not in texts[t - v.first]:
                return None
        if v.kind == "deliverable":
            T = v.last
            vidx = v.idx
            for f in v.hdr:
                cur = cur_at(c, f, T)
                prev_vis = last_visit_of.get(c)
                away = (vidx - prev_vis - 1) if prev_vis is not None else None
                base = {"check_id": f"{v.did}.{f}", "deliverable_id": v.did, "client": c, "type": v.dtype,
                        "field": f, "turn": T, "session_bin": session_bin_of(T),
                        "visits_since_client_last_seen": away}
                if cur is None:
                    checks.append({**base, "kind": "unknown", "expected": None, "superseded_values": [],
                                   "stated_turn": None, "age_turns": None, "placement": "none",
                                   "other_visits_since_stated": None, "tags": ["UNK"],
                                   "age_bin": None, "form": None})
                    continue
                hist = [h for h in clients[c].history[f] if h.turn < T]
                other_visits = sum(1 for (cc, _) in seq[cur.visit + 1:vidx] if cc != c)
                age = T - cur.turn
                checks.append({**base, "kind": "field", "expected": cur.value,
                               "superseded_values": [h.value for h in hist[:-1] if h.value != cur.value],
                               "stated_turn": cur.turn, "age_turns": age, "placement": cur.placement,
                               "other_visits_since_stated": other_visits,
                               "tags": tags_for(f == v.ctrl_field, len(hist) > 1, age, away),
                               "age_bin": age_bin_of(age), "form": cur.form})
            if v.facing and cur_at(c, "style_rule", T) is not None:
                cur = cur_at(c, "style_rule", T)
                bad, good = cur.value
                prev_vis = last_visit_of.get(c)
                away = (vidx - prev_vis - 1) if prev_vis is not None else None
                age = T - cur.turn
                checks.append({
                    "check_id": f"{v.did}.style", "deliverable_id": v.did, "client": c, "type": v.dtype,
                    "kind": "style", "field": "style_rule", "expected": {"include": good, "exclude": bad},
                    "superseded_values": [], "turn": T, "stated_turn": cur.turn, "age_turns": age,
                    "placement": cur.placement,
                    "other_visits_since_stated": sum(1 for (cc, _) in seq[cur.visit + 1:vidx] if cc != c),
                    "visits_since_client_last_seen": away,
                    "tags": tags_for(False, False, age, away),
                    "session_bin": session_bin_of(T), "age_bin": age_bin_of(age), "form": cur.form})
            prev_vis = last_visit_of.get(c)
            away = (v.idx - prev_vis - 1) if prev_vis is not None else None
            for f in list(v.shared_hdr) + list(v.shared_std):
                cur, idx = shared_cur(sh, f, T)
                base = {"check_id": f"{v.did}.{f}", "deliverable_id": v.did, "client": c, "type": v.dtype,
                        "field": f, "turn": T, "session_bin": session_bin_of(T),
                        "visits_since_client_last_seen": away, "scope": "shared"}
                if cur is None:
                    checks.append({**base, "kind": "unknown", "expected": None, "superseded_values": [],
                                   "stated_turn": None, "age_turns": None, "placement": "none",
                                   "other_visits_since_stated": None, "tags": ["G", "UNK"],
                                   "age_bin": None, "form": None, "cross_client": False, "placement_client": None})
                    continue
                age = T - cur["turn"]
                prior = [x["value"] for x in sh[f] if x["turn"] < T]
                common = {**base, "stated_turn": cur["turn"], "age_turns": age, "placement": "shared",
                          "other_visits_since_stated": sum(1 for (cc, _) in seq[cur["visit"] + 1:v.idx] if cc != c),
                          "tags": ["G"] + (["D"] if idx > 0 else []), "age_bin": age_bin_of(age),
                          "form": cur["form"], "cross_client": cur["client"] != c,
                          "placement_client": cur["client"]}
                if f in SHARED_HDR:
                    checks.append({**common, "kind": "field", "expected": cur["value"],
                                   "superseded_values": [x for x in prior[:-1] if x != cur["value"]]})
                    continue
                if f == "sign_off":
                    exp = {"include": cur["value"], "exclude": cur["old"] or ""}
                    sup = [cur["old"]] if cur["old"] else []
                elif f == "next_steps":
                    exp, sup = {"include": cur["value"], "exclude": "", "mode": "heading"}, []
                else:
                    exp, sup = {"include": cur["value"][1], "exclude": cur["value"][0]}, []
                checks.append({**common, "kind": "style", "expected": exp, "superseded_values": sup})
        visits_meta.append({"visit": v.idx, "client": c, "kind": v.kind, "first_turn": v.first,
                            "last_turn": v.last, "deliverable": v.did, "planted": planted})
        last_visit_of[c] = v.idx
    for chk in checks:
        chk.setdefault("scope", "client")
    return clients, messages, checks, visits_meta, sh


def readable_v2(messages, visits_meta):
    out = ["# Scripted session (readable view, schedule v2)\n",
           "Planted facts are listed under each visit: client field [placement, form]. "
           "The agent sees only the message text.\n"]
    by_visit = defaultdict(list)
    for m in messages:
        by_visit[m["visit"]].append(m)
    for v in visits_meta:
        out.append(f"\n## Visit {v['visit']}: {v['client']} ({v['kind']}"
                   + (f", writes {v['deliverable']}" if v['deliverable'] else "") + ")\n")
        if v["planted"]:
            out.append("Planted: " + "; ".join(f"{c} {f} [{p}, {fm}]" for c, f, p, fm in v["planted"]) + "\n")
        for m in by_visit[v["visit"]]:
            out.append(f"\n**Turn {m['turn']}**\n\n{m['text']}\n")
    return "\n".join(out)


def write_session(out, messages, checks, clients, visits, readable_text, meta, shared=None):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "session.jsonl"), "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")
    with open(os.path.join(out, "checks.jsonl"), "w") as f:
        for c in checks:
            f.write(json.dumps(c) + "\n")
    with open(os.path.join(out, "facts.json"), "w") as f:
        facts = {c: {k: [vars(h) for h in v] for k, v in cl.history.items()} for c, cl in clients.items()}
        if shared:
            # "_shared": statements that apply to every client; placement_client is the client whose visit it was said in
            facts["_shared"] = {fld: [{"value": list(x["value"]) if isinstance(x["value"], tuple) else x["value"],
                                       "turn": x["turn"], "visit": x["visit"], "placement": "shared",
                                       "sentence": x["sentence"], "form": x["form"], "kind": x["kind"],
                                       "placement_client": x["client"]} for x in xs]
                                for fld, xs in shared.items()}
        json.dump(facts, f, indent=1)
    with open(os.path.join(out, "visits.json"), "w") as f:
        json.dump(visits, f, indent=1)
    with open(os.path.join(out, "readable.md"), "w") as f:
        f.write(readable_text)
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)


def count_by(checks, key):
    out = {}
    for c in checks:
        vals = c[key] if isinstance(c[key], list) else [c[key]]
        for t in vals:
            out[str(t)] = out.get(str(t), 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--schedule", choices=["v1", "v2"], default="v2",
                    help="v1: original pilot design; v2: fixed-age check schedule (default)")
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--visits", type=int, default=48, help="v1 only: number of visits")
    ap.add_argument("--deliverables", type=int, default=38, help="v2 only: number of deliverables")
    ap.add_argument("--debriefs", type=int, default=11, help="v2 only: number of debrief visits")
    ap.add_argument("--notes-scale", type=float, default=1.0,
                    help="multiplies filler lines in call notes, to tune session size")
    ap.add_argument("--report-rows", type=int, default=None,
                    help="rows in each pasted usage export (main lever for session size); "
                         "default 300 for v1, 420 for v2")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.schedule == "v1":
        rows = 300 if a.report_rows is None else a.report_rows
        clients, messages, checks, visits = generate(a.seed, a.clients, a.visits, a.notes_scale,
                                                    report_rows=rows)
        readable_text = readable(messages, visits, clients)
        extra = {}
    else:
        rows = 420 if a.report_rows is None else a.report_rows
        clients, messages, checks, visits, shared, attempt = generate_v2(
            a.seed, a.clients, a.deliverables, a.debriefs, a.notes_scale, report_rows=rows)
        readable_text = readable_v2(messages, visits)
        facts = [h for cl in clients.values() for hs in cl.history.values() for h in hs]
        n_turns = len(messages)
        third = lambda t: min(2, (t - 1) * 3 // n_turns)
        sh_checks = [c for c in checks if c["scope"] == "shared"]
        sh_stated = [c for c in sh_checks if c["kind"] != "unknown"]
        sh_stm = [x for xs in shared.values() for x in xs]
        extra = {
            "schedule": "v2", "plan_attempt": attempt,
            "shared_statements": len(sh_stm),
            "shared_supersessions": sum(1 for x in sh_stm if x["k"]),
            "shared_checks": len(sh_checks), "shared_checks_stated": len(sh_stated),
            "shared_cross_client_share": round(sum(c["cross_client"] for c in sh_stated) / len(sh_stated), 3),
            "shared_checks_by_age_bin": count_by(sh_stated, "age_bin"),
            "fact_statements": len(facts),
            "supersessions": sum(max(0, len(hs) - 1) for cl in clients.values() for hs in cl.history.values()),
            "embedded_statements": sum(h.placement == "embedded" for h in facts),
            "buried_statements": sum(h.form == "buried" for h in facts),
            "statements_by_third": [sum(third(h.turn) == i for h in facts) for i in range(3)],
            "checks_by_age_bin": count_by(checks, "age_bin"),
            "checks_by_session_bin": count_by(checks, "session_bin"),
        }
    chars = sum(len(m["text"]) for m in messages)
    plc = count_by(checks, "placement")
    meta = {"seed": a.seed, "schedule": a.schedule, "clients": a.clients,
            "visits": len(visits), "notes_scale": a.notes_scale, "report_rows": rows,
            "user_messages": len(messages), "deliverables": len({c["deliverable_id"] for c in checks}),
            "checks": len(checks), "checks_by_tag": count_by(checks, "tags"), "checks_by_placement": plc,
            "user_text_chars": chars, "user_text_tokens_est": chars // 4}
    if a.schedule == "v1":
        meta["visits"] = a.visits
    meta.update(extra)
    write_session(a.out, messages, checks, clients, visits, readable_text, meta,
                  shared if a.schedule == "v2" else None)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
