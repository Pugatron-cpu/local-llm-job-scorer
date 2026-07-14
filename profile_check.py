"""
profile_check.py — dry-run a profile's keyword gate against real postings. No scrape, no LLM.

    python profile_check.py            # check the active profile
    python profile_check.py finance    # check profiles/finance.toml before you trust it

WHY THIS EXISTS
A profile in another field (finance, treasury, law) inherits TECH-SHAPED defaults: TECH_TERMS is
full of "kubernetes" and "mlops". Point it at treasury queries without giving it treasury
vocabulary and the pipeline does NOT fail — it scrapes fine, drops ~everything at the keyword
gate, scores nothing, and hands back an empty shortlist. You cannot tell that from the outside:
"no results" looks identical to "no such jobs in Denmark". That silent failure is the single
biggest trap in running this for someone else.

This replays the REAL gate (core.passes_prefilter, not a copy of it) over postings the scraper
has actually seen, and tells you what a run would find — in about a second.

It reads raw_teasers.csv (every posting seen, WITH snippets → an exact replay). If that doesn't
exist yet (it's written from the first scrape after 2026-07-14), it falls back to the archive,
which stores titles but no snippets — so the gate is replayed on titles alone and the real run
will match somewhat MORE than reported. The fallback warns you.
"""

import csv
import os
import sys
import collections

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Resolve the profile BEFORE importing config: config reads the profile at import time, so this
# is what makes `profile_check.py finance` evaluate finance's terms rather than the active one.
# Importing config this way (rather than re-deriving the term lists here) means the check can
# never drift from what a real run would do — same resolution code, same overrides, same defaults.
_name = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
if _name:
    os.environ["JOBSEARCH_PROFILE"] = _name

import config     # noqa: E402  (must follow the env var above)
import core       # noqa: E402  (imports config's terms via `from config import *`)

# The postings to replay against always come from the OWNER's data dir — that's where the scraped
# record of the market lives. A brand-new profile has no data of its own yet, and evaluating its
# terms against its own empty folder would tell you nothing.
OWNER_DATA = os.path.join(SCRIPT_DIR, "job_market_data")
RAW = os.path.join(OWNER_DATA, "raw_teasers.csv")
ARCHIVE = os.path.join(OWNER_DATA, "job_market_data.csv")


def _load_postings():
    """(rows, source_label, has_snippets). Prefer raw_teasers (exact); fall back to the archive."""
    if os.path.isfile(RAW):
        with open(RAW, encoding="utf-8") as f:
            rows = [{"title": r["title"], "snippet": r.get("snippet", ""),
                     "company": r.get("company", ""), "url": r.get("url", "")}
                    for r in csv.DictReader(f)]
        return rows, f"raw_teasers.csv ({len(rows)} sightings — everything the scraper saw)", True
    if os.path.isfile(ARCHIVE):
        with open(ARCHIVE, encoding="utf-8") as f:
            rows = [{"title": r["title"], "snippet": "",       # archive stores no snippet
                     "company": r.get("company", ""), "url": r.get("url", "")}
                    for r in csv.DictReader(f)]
        return rows, f"job_market_data.csv ({len(rows)} scored roles)", False
    sys.exit(f"No postings to check against. Run a scrape first ({ARCHIVE} not found).")


def main():
    rows, source, has_snips = _load_postings()

    print(f"profile      : {config.ACTIVE_PROFILE}")
    print(f"queries      : {len(config.TARGET_QUERIES)}")
    print(f"INCLUDE terms: {len(config.INCLUDE_TERMS)}   EXCLUDE terms: {len(config.EXCLUDE_TERMS)}")
    print(f"checked vs   : {source}")
    if not has_snips:
        # Two distinct biases, and the second one is the nastier: the archive is what SURVIVED the
        # owner's keyword gate and got scored. It is not a sample of the market, it's a sample of
        # the owner's search. So the denominator is already filtered and the percentage below is
        # NOT "share of postings out there" — treat it as directional only. raw_teasers.csv (from
        # the first scrape after 2026-07-14) is the unfiltered record and makes this exact.
        print("               ⚠ FALLBACK — no raw_teasers.csv yet, so this is approximate twice over:")
        print("                 (a) no snippets: the gate is replayed on TITLES only, but a real")
        print("                     run reads title+snippet, so it will match somewhat more.")
        print("                 (b) the archive is POST-gate: it only holds roles that already")
        print("                     passed the OWNER's keyword filter and got scored. So the % is")
        print("                     not a share of the market — it's directional. A near-zero")
        print("                     result is still damning; a high one is not proof of health.")
        print("                 Re-run after the next scrape for the exact answer.")
    if not rows:
        sys.exit("No rows to check.")

    # ---- THE PRIMARY CHECK: do the profile's QUERIES and its keyword GATE agree? --------------
    # Corpus stats alone cannot answer this. A finance profile that inherits the default TECH
    # terms matches plenty of the archive — because the archive is full of tech roles — and looks
    # "healthy" while being completely broken for its owner. So ask the corpus-free question
    # instead: would this profile's own search queries survive its own gate? If you search for
    # "cash management" and your INCLUDE list has no idea what that is, the gate eats what the
    # queries fetch, and the run returns nothing while looking perfectly fine.
    q_pass = [q for q in config.TARGET_QUERIES
              if core.passes_prefilter({"title": q, "snippet": ""})]
    q_pct = 100 * len(q_pass) / max(len(config.TARGET_QUERIES), 1)
    q_fail = [q for q in config.TARGET_QUERIES if q not in q_pass]
    print(f"\nqueries surviving this profile's OWN keyword gate: "
          f"{len(q_pass)}/{len(config.TARGET_QUERIES)}  ({q_pct:.0f}%)")
    if q_fail:
        print("  queries your own gate would REJECT:")
        for q in q_fail[:8]:
            print(f"    {q!r}")
        if len(q_fail) > 8:
            print(f"    ... and {len(q_fail) - 8} more")

    # Is the profile searching in one domain while filtering with inherited defaults from another?
    custom_terms = any(k in config._prof for k in ("tech_terms", "include_terms", "bridge_terms"))
    inherited = bool(config._prof.get("queries")) and not custom_terms
    if inherited:
        print("\n  ⚠ This profile sets its own `queries` but inherits the DEFAULT (tech-shaped)")
        print("    INCLUDE_TERMS. If it isn't a tech profile, the gate is filtering for a")
        print("    different field than the queries are searching in.")

    passed = [r for r in rows if core.passes_prefilter(r)]
    pct = 100 * len(passed) / len(rows)
    print(f"\nwould reach the LLM: {len(passed)} / {len(rows)}  ({pct:.1f}%)")

    # Which terms are actually doing the work, and which are dead weight.
    fired = collections.Counter()
    for r in passed:
        text = f" {r['title']} {r['snippet']} ".lower()
        for t in config.INCLUDE_TERMS:
            if t in text:
                fired[t] += 1
    dead = [t for t in config.INCLUDE_TERMS if not fired[t]]

    if passed:
        print("\ntop INCLUDE terms doing the work:")
        for t, n in fired.most_common(8):
            print(f"    {n:>5}  {t!r}")
        if dead:
            print(f"\nINCLUDE terms that never matched ({len(dead)}): "
                  f"{', '.join(repr(t) for t in dead[:12])}"
                  + (" ..." if len(dead) > 12 else ""))
        print("\nsample of what WOULD be scored:")
        for r in passed[:6]:
            print(f"    {r['title'][:60]:<60} {r['company'][:22]}")

    rejected = [r for r in rows if r not in passed]
    if rejected:
        print("\nsample of what would be DROPPED:")
        for r in rejected[:6]:
            print(f"    {r['title'][:60]:<60} {r['company'][:22]}")

    # The verdict is the whole point: turn a silent empty shortlist into a loud diagnosis.
    # Query/gate coherence outranks corpus share, because corpus share can look great while the
    # profile is searching for treasury jobs and filtering for Kubernetes.
    print()
    if q_pct < 60 or inherited:
        print("VERDICT: BROKEN — the queries and the keyword gate disagree.")
        print(f"         {len(q_fail)} of {len(config.TARGET_QUERIES)} of this profile's own search "
              f"queries would be thrown away by its")
        print("         own INCLUDE_TERMS. Whatever those queries scrape, the gate eats. The run")
        print("         will NOT error — it will just return nothing, and look like an empty market.")
        print(f"\n         Fix: give profiles/{config.ACTIVE_PROFILE}.toml its own vocabulary, e.g.")
        print('           tech_terms   = ["treasury", "cash management", "fp&a", "liquidity", ...]')
        print('           bridge_terms = []        # or the adjacent roles worth taking')
        print('           exclude_terms = [...]    # careful: EXCLUDE is a hard veto')
        print("\n         NOTE: the SCORING rubric (core.py) is still tech-shaped — Track A means")
        print("         'SOFTWARE/DATA/IT technical' and finance roles are capped at <=35 there.")
        print("         Fixing the gate gets postings to the scorer; it does not yet make the")
        print("         scorer judge them fairly. That's the next piece of work.")
        sys.exit(1)
    # A profile from a DIFFERENT field, checked against the owner's corpus, will always score low
    # on corpus share — not because its gate is bad, but because the corpus contains none of the
    # roles it's looking for (the owner's queries never searched for them). Judging it on that
    # number would be exactly the mistake this tool exists to prevent, in reverse. So when the
    # profile isn't the corpus owner, the corpus figure is informational and coherence decides.
    if not config.IS_OWNER:
        print("VERDICT: COHERENT — queries and keyword gate agree, which is the part that can be")
        print("         checked without her own data.")
        print(f"\n         The {pct:.1f}% above is NOT a verdict on this profile: the corpus is the")
        print("         OWNER's archive, which contains no roles from her field (his queries never")
        print("         searched for them). Expect it to be low, and ignore it. The real number")
        print("         arrives after her first scrape:")
        print(f"             python a_scrape.py --profile {config.ACTIVE_PROFILE}")
        print(f"             python profile_check.py {config.ACTIVE_PROFILE}   # now exact")
        return
    if pct < 5:
        print("VERDICT: THIN. It matches, but barely. Check the dropped sample above — if roles")
        print("         you'd want are in there, widen INCLUDE_TERMS. Remember INCLUDE is cheap")
        print("         (a false positive costs one LLM call); EXCLUDE is the dangerous list.")
        return
    print(f"VERDICT: HEALTHY. {pct:.0f}% of the checked postings reach the scorer. The LLM does the")
    print("         rest of the filtering.")
    if not has_snips:
        print("         (But see the fallback caveat above — this % is directional, not exact.)")


if __name__ == "__main__":
    main()
