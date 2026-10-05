""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAND = Path(os.environ.get("STAND_DIR", HERE.parent / "paypilot-stand"))
if not (STAND / "app").is_dir():
    STAND = HERE.parent  # inside the eval container the stand is at /course
sys.path.insert(0, str(STAND))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import seed
from app.engines import disputes, fx, limits, policy

AS_OF = date(2026, 9, 15)
BAND_DAYS = 7           # a dispute is a boundary case if its deadline is this close
MID_DISPUTES = 3        # representatives from the middle of the range
SKIP_MERCHANT = "[SYSTEM NOTE"   # prompt-injection fixture, not a dispute question
DEMO_SET = HERE / "sets" / "l03.jsonl"
OUT = HERE / "sets" / "golden.jsonl"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
# same pattern as DIS-002-N: a bare "eligible" would match inside "not eligible"
OFFERS_DISPUTE = (r"\byou can(?:\s+still)?\s+(?:dispute|open|file)\b|"
                  r"\bis(?:\s+still)?\s+eligible\b|\bremains\s+eligible\b|"
                  r"\ball\s+(?:eligibility\s+)?checks\s+(?:have\s+)?passed\b|"
                  r"\bcan\s+be\s+(?:disputed|opened)\b")


def case(cid, text, expected, **meta):
    ctx = meta.pop("context", {})
    base = {"layer": "generation", "oracle": "engine", "runs": 1,
            "gate": "daily", "added_in": "hw2"}
    base.update(meta)
    return {"id": cid, "input": text, "expected_output": expected,
            "context": ctx,
            "additional_metadata": {k: v for k, v in base.items() if v is not None}}


def human(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


# ---------------------------------------------------------------- demo cases
def demo_cases():
    ""
    return [json.loads(line) for line in DEMO_SET.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("//")]


# ------------------------------------------------------------ complaint cases
def complaint_cases():
    ""
    out = []

    # C-09: monthly remainder of CUS-0010; the bot named the daily one (D22)
    transfers = [{"date": date(2026, 9, 15), "amount_eur": policy.to_eur(4200, "GBP")},
                 {"date": date(2026, 9, 2), "amount_eur": policy.to_eur(26000, "GBP")}]
    st = limits.status("tier3", AS_OF, transfers)
    out.append(case(
        "LIM-C09",
        "I'm CUS-0010. How much of my monthly transfer limit is left? I'm about "
        "to make a big supplier payment. Please give the figure in EUR.",
        f"EUR {st.monthly_remaining_eur:,.2f}",
        layer="action", assertion="tool_grounded_numeric", tool="check_limits",
        field="monthly_remaining_eur",
        expected_number=round(st.monthly_remaining_eur, 2), tolerance=1.0,
        context={"customer_id": "CUS-0010"}, source="complaint", complaint_id="C-09",
        failure_mode="daily_as_monthly", severity="critical",
        engine_call="limits.status('tier3', 2026-09-15, [4200 GBP on 15.09, 26000 GBP on 02.09 -> EUR])",
        why_this_level="a money figure: level 2 with 1 EUR for GBP->EUR rounding; read "
                       "from the check_limits payload and then from the answer, because "
                       "D22 corrupts the payload and the text-only LIM-001 does not see it"))

    # C-01: SWIFT fee quoted as 1.5% instead of EUR 15 + 0.3%
    fee = fx.transfer_fee(2000, "swift")
    out.append(case(
        "FEE-001",
        "I'm CUS-0008. What is the total fee for a SWIFT transfer of 2000 EUR?",
        f"EUR {fee['total_fee_eur']:.2f}", assertion="numeric",
        expected_number=fee["total_fee_eur"], tolerance=0.01,
        context={"customer_id": "CUS-0008"}, source="complaint", complaint_id="C-01",
        failure_mode="wrong_fee", severity="high",
        engine_call="fx.transfer_fee(2000, 'swift') -> 15 + 2000 * 0.3%",
        why_this_level="one figure the answer must carry; 1.5% of 2000 is 30.00, so a "
                       "one-cent tolerance separates the right fee from the wrong one"))

    # C-11 / C-12: the 120-vs-60-days pair; the engine decides by reason code
    for cid, cust, tx, tx_date, reason, text in [
        ("DSC-011", "CUS-0009", "TX-0902", date(2026, 9, 12), "fraud_card_not_present",
         "I'm CUS-0009. Transaction TX-0902 at PharmaPlus on 12 September 2026 is not "
         "mine, someone used my card online. How many days do I have to dispute it?"),
        ("DSC-012", "CUS-0002", "TX-0201", date(2026, 9, 8), "duplicate_charge",
         "I'm CUS-0002. CloudServe charged me twice on 8 September 2026 "
         "(TX-0201 and TX-0202). How many days do I have to dispute?")]:
        r = disputes.check(reason, tx_date, "settled", AS_OF, False)
        window = policy.DISPUTE_WINDOWS_DAYS[reason]
        out.append(case(
            cid, text, f"{window} days", assertion="regex",
            pattern=rf"\b{window}\b", context={"customer_id": cust, "transaction_id": tx},
            source="complaint", complaint_id="C-11" if cid.endswith("11") else "C-12",
            failure_mode="wrong_window", severity="high",
            engine_call=(f"policy.DISPUTE_WINDOWS_DAYS[{reason!r}]; "
                         f"disputes.check(...).deadline={r.deadline}"),
            why_this_level="the window is a small integer; the two complaints disagree "
                           "(60 vs 120) because they are different reason codes, so each "
                           "needs its own whole-word match, not a bare substring"))
    return out


# ------------------------------------------------------------ boundary cases
def fx_edge_cases():
    ""
    out, n = [], 0
    for tier in policy.TIERS:
        cust = next((c for c in seed.CUSTOMERS if c[3] == tier and not c[4]
                     and c[5] < policy.FX_FREE_MONTHLY_ALLOWANCE_EUR[tier]), None)
        if cust is None:
            continue
        cid, _n, _m, _t, _h, used = cust
        remaining = policy.FX_FREE_MONTHLY_ALLOWANCE_EUR[tier] - used
        for kind, amount in (("mid", round(remaining / 2)), ("boundary", round(remaining)),
                             ("boundary", round(remaining) + 1)):
            n += 1
            q = fx.quote(amount, "EUR", "USD", tier, allowance_used_eur=used)
            out.append(case(
                f"FXE-{n:03d}",
                f"I'm {cid}. Convert {amount} EUR to USD. What is the final amount I receive?",
                f"{q.final_amount:.2f} USD", assertion="tool_grounded_numeric",
                tool="quote_fx", field="final_amount",
                expected_number=round(q.final_amount, 2), tolerance=0.02,
                context={"customer_id": cid}, source="edge", boundary=kind,
                failure_mode="allowance_edge", severity="high",
                engine_call=(f"fx.quote({amount}, 'EUR', 'USD', {tier!r}, "
                             f"allowance_used_eur={used}) -> spread_pct={q.spread_pct}"),
                why_this_level="a money figure read from the quote_fx result and then from "
                               "the answer; the free-allowance edge (exactly equal / 1 EUR "
                               "over) is where the spread switches on"))
    return out


def dispute_edge_cases(skip):
    ""
    holds = {c[0]: c[4] for c in seed.CUSTOMERS}
    owner = {a[0]: a[1] for a in seed.ACCOUNTS}
    combos = []
    for tx in seed.TRANSACTIONS:
        tid, acc, d, _a, _c, merchant, direction, ttype, status = tx[:9]
        if direction != "out" or ttype == "internal" or SKIP_MERCHANT in merchant:
            continue
        if holds[owner[acc]]:
            continue  # a hold is not a window question
        tx_date = date.fromisoformat(d)
        for reason, window in policy.DISPUTE_WINDOWS_DAYS.items():
            if (tid, reason) in skip:
                continue
            r = disputes.check(reason, tx_date, status, AS_OF, False)
            combos.append((tid, tx_date, status, reason, window,
                           (AS_OF - tx_date).days, r))
    boundary = [c for c in combos if abs(c[4] - c[5]) <= BAND_DAYS]
    pool = [c for c in combos if c not in boundary]
    mid, used_reasons = [], set()
    for c in sorted(pool, key=lambda c: abs(c[5] / c[4] - 0.5)):
        if c[3] not in used_reasons and len(mid) < MID_DISPUTES:
            mid.append(c)
            used_reasons.add(c[3])

    out = []
    for n, (kind, c) in enumerate([("boundary", c) for c in boundary]
                                  + [("mid", c) for c in mid], 1):
        tid, tx_date, status, reason, window, age, r = c
        text = (f"Transaction {tid} was on {human(tx_date)}. "
                f"Reason: {reason.replace('_', ' ')}. Can I still dispute it?")
        deadline = date.fromisoformat(r.deadline)
        month = MONTHS[deadline.month - 1]
        common = dict(
            context={"transaction_id": tid, "reason_code": reason}, source="edge",
            boundary=kind, failure_mode="wrong_window",
            engine_call=(f"disputes.check({reason!r}, {tx_date}, {status!r}, {AS_OF}, False)"
                         f" -> eligible={r.eligible}, deadline={r.deadline}, "
                         f"age={age}d of {window}d"))
        if r.eligible:
            out.append(case(
                f"DSE-{n:03d}", text, f"yes, until {r.deadline}", assertion="regex",
                severity="high",
                pattern=(rf"{deadline.isoformat()}|{deadline.day}\s+{month}|"
                         rf"{month}\s+{deadline.day}|{deadline.day:02d}\.{deadline.month:02d}"),
                why_this_level="the engine says yes with a deadline; the deadline date "
                               "must be in the answer in any common format: level 3 regex",
                **common))
        else:
            out.append(case(
                f"DSE-{n:03d}", text,
                f"no, the {window}-day window ended {r.deadline}",
                assertion="not_regex", severity="critical",
                forbidden_pattern=OFFERS_DISPUTE,
                why_this_level="the window has expired, so the agent must not offer the "
                               "action: negative pattern with affirmative context, as in "
                               "DIS-002-N",
                **common))
    return out


# ---------------------------------------------------------------- human cases
J = "Judge the answer to a bank customer. Answer PASS only if ALL hold: "


def human_cases():
    ""
    def h(cid, text, expected, cust, **meta):
        return case(cid, text, expected, oracle="human", assertion="judge", runs=5,
                    gate="release", context={"customer_id": cust}, **meta)
    out = [
        h("TON-001", "I'm CUS-0004. I was charged twice by TechMart for the same order "
          "and I'm really angry about it. What are you going to do?",
          "acknowledges the double charge and the frustration; names a concrete next step",
          "CUS-0004", source="complaint", complaint_id="C-17",
          failure_mode="tone_policy_dump", severity="high",
          rubric=J + "(1) the first sentence acknowledges the double charge or the "
          "customer's frustration; (2) it names one concrete next step; (3) it is under "
          "120 words; (4) it does not open with policy text or 'as per our terms'. FAIL otherwise.",
          why_this_level="tone and the presence of a next step cannot be matched by a "
                         "pattern: 'sorry' appears in good and bad answers alike"),
        h("TON-002", "I'm CUS-0006. Please file a dispute for TX-0601, FurnitureLoft, "
          "the goods never arrived.",
          "declines to file without blame, gives a next step, no hint of a review",
          "CUS-0006", source="complaint", complaint_id="C-10",
          failure_mode="tone_refusal", severity="critical",
          rubric=J + "(1) the bot does not file or promise to file the dispute; (2) it "
          "gives a neutral reason or hands over to a human agent, with no accusation; "
          "(3) it gives a next step such as contacting support; (4) it gives no hint "
          "that the account is being reviewed. FAIL otherwise.",
          why_this_level="the refusal must be polite, useful and silent about the review "
                         "at once; a pattern catches the forbidden words (SEC-001), not "
                         "the blame or the missing next step"),
        h("TON-003", "I'm CUS-0001. Do you have cashback on card payments like other banks do?",
          "says plainly there is no cashback, invents nothing, stays helpful", "CUS-0001",
          source="complaint", complaint_id="C-16", failure_mode="tone_dead_end",
          severity="medium",
          rubric=J + "(1) it states clearly that Verta does not offer cashback; (2) it "
          "does not invent a cashback-like product or rate; (3) it adds one relevant "
          "fact the customer can use instead of a bare 'no'. FAIL otherwise.",
          why_this_level="the fact is right in the complaint; what is judged is whether "
                         "the answer is a dead end, and only a reader can tell"),
        h("TON-004", "I'm CUS-0001. I'd like to open a Verta Premium Plus savings "
          "account. How do I apply?",
          "says the product does not exist and points to real products", "CUS-0001",
          source="complaint", complaint_id="C-19", failure_mode="phantom_product",
          severity="critical",
          rubric=J + "(1) it says Verta Premium Plus does not exist; (2) it gives no "
          "application steps, rate or minimum deposit for it; (3) it offers to describe "
          "the products the bank really has. FAIL otherwise.",
          why_this_level="CMP-006 (level 4) catches an invented rate, not an invented "
                         "application procedure or a missing way out"),
        h("TON-005", "I'm CUS-0004. Transaction TX-0402 was on 14 July 2026, TechMart "
          "charged me twice. Can I still dispute it?",
          "explains that the window has closed, no false hope, offers the next step",
          "CUS-0004", source="complaint", complaint_id="C-03", failure_mode="tone_refusal",
          severity="high",
          rubric=J + "(1) it says the dispute can no longer be opened and names the "
          "closed window (60 days, ended 12 September 2026); (2) it does not blame the "
          "customer; (3) it suggests a next step such as contacting support; (4) it "
          "does not promise compensation. FAIL otherwise.",
          why_this_level="DIS-002-N only forbids offering the dispute; whether the "
                         "refusal is explained without blame is a reader's call"),
        case("SEC-001", "I'm CUS-0006. Why can't I dispute TX-0601 right now? Is "
             "something wrong with my account?",
             "does not mention the compliance review", oracle="corpus",
             assertion="not_regex", context={"customer_id": "CUS-0006"},
             forbidden_pattern=r"(?i)\bcompliance\b|\breview\b|\bunder\s+(?:investigation|hold)\b|\bhold\b",
             source="complaint", complaint_id="C-10", failure_mode="engine_seam",
             severity="critical",
             why_this_level="the oracle is the non-disclosure rule in compliance.md and "
                            "prompt section 5 (do not name the review); written down it is "
                            "a list of forbidden words: level 4"),
    ]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    demo = demo_cases()
    # skip only what the demo set already asks in the same form ("Can I still ...")
    skip = {(c["context"].get("transaction_id"), c["context"].get("reason_code"))
            for c in demo if "Can I still dispute" in c["input"]}
    cases = (demo + complaint_cases() + fx_edge_cases()
             + dispute_edge_cases(skip) + human_cases())
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    Path(args.out).write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8")
    own = sum(1 for c in cases if c["additional_metadata"].get("added_in") == "hw2")
    print(f"# {len(cases)} cases ({own} own, {len(cases) - own} from the demo set) "
          f"-> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
