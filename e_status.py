"""
e_status.py — STEP E: see where every application stands (read-only, run anytime).

Reads the tracker (applications/applications.csv, the file c_prepare appends to) and prints a
terminal status board so nothing quietly slips:
  • FUNNEL       — how many roles sit at each stage right now (interested → applied →
                   interview → offer → hired, plus rejected / rejected_after_interview /
                   skipped), a live-vs-closed summary, and an interview→outcome breakdown;
  • FOLLOW-UPS   — active roles whose next_followup date has already passed (the thing that
                   actually gets forgotten), plus active roles with no follow-up date set;
  • DEADLINES    — open roles with an application deadline still ahead, soonest first, with
                   missed deadlines on roles you never applied to called out separately.

    python e_status.py             # the full board
    python e_status.py --funnel    # just the pipeline counts
    python e_status.py --followups # just the chasing list
    python e_status.py --deadlines # just the deadline list

READ-ONLY: e_status never writes the tracker. Status changes are c_prepare's job
(`python c_prepare.py --status <url> applied`). Settings come from config.py; personal data
from profiles/<name>.toml. See README.md.
"""

import os
import sys
import csv
from collections import Counter
from datetime import datetime, date

import config

# The pipeline, in order, then the terminal states. Mirrors c_prepare.STATUSES; kept local so
# this read-only view has no reason to import the writer module.
FUNNEL   = ["interested", "applied", "interview", "offer", "hired"]
TERMINAL = ["rejected", "rejected_after_interview", "skipped"]
ACTIVE   = {"interested", "applied", "interview"}   # still live -> follow-ups / deadlines matter
# Statuses that mean an interview actually happened (for the interview-conversion summary).
REACHED_INTERVIEW = ["interview", "offer", "hired", "rejected_after_interview"]

URGENT_DAYS = 3   # a deadline this close (or closer) gets a ⚠


# --- loading + parsing (pure) -------------------------------------------------------------
def load_tracker(path=None):
    path = path or config.TRACKER_CSV
    if not os.path.isfile(path):
        sys.exit(f"No tracker at {path}.\n"
                 f"Prep a role first:  python c_prepare.py <shortlist-number | url>")
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _status(row):
    return (row.get("status") or "").strip().lower()


def _date(s):
    """Parse an ISO date; return None for blank, 'N/A', or anything unparseable."""
    s = (s or "").strip()
    if not s or s.upper() == "N/A":
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


# --- selections (pure; unit-tested in tests/test_core.py) ---------------------------------
def funnel_counts(rows):
    """Current-state count per status (what stage each role sits at *now* — not cumulative
    flow, which the tracker does not record)."""
    return Counter(_status(r) for r in rows)


def overdue_followups(rows, today):
    """Active roles whose next_followup date has passed, most overdue first."""
    out = []
    for r in rows:
        if _status(r) not in ACTIVE:
            continue
        d = _date(r.get("next_followup"))
        if d and d < today:
            out.append((d, r))
    out.sort(key=lambda t: t[0])
    return out


def followups_unset(rows):
    """Active roles with no follow-up date at all — invisible to the overdue list, so worth
    surfacing on their own."""
    return [r for r in rows if _status(r) in ACTIVE and _date(r.get("next_followup")) is None]


def upcoming_deadlines(rows, today):
    """Still-active roles (interested/applied/interview) with an application deadline still
    ahead, soonest first. Once a role reaches offer/hired or is closed, its deadline is moot."""
    out = []
    for r in rows:
        if _status(r) not in ACTIVE:      # only interested/applied/interview still chase a deadline;
            continue                      # offer/hired/rejected/skipped make it moot
        d = _date(r.get("deadline"))
        if d and d >= today:
            out.append((d, r))
    out.sort(key=lambda t: t[0])
    return out


def missed_deadlines(rows, today):
    """Roles still marked 'interested' whose deadline has already passed — you never applied
    and the window has closed. Not an error, but easy to miss."""
    out = []
    for r in rows:
        if _status(r) != "interested":
            continue
        d = _date(r.get("deadline"))
        if d and d < today:
            out.append((d, r))
    out.sort(key=lambda t: t[0])
    return out


# --- printing -----------------------------------------------------------------------------
def _bar(count, maxcount, width=28):
    if maxcount <= 0:
        return ""
    return "█" * max(0, int(round(width * count / maxcount)))


def _who(r, w=44):
    who = f"{(r.get('company') or '?')} — {(r.get('role') or '?')}"
    return who[:w]


def print_header(rows):
    print("=" * 60)
    print(f"APPLICATION TRACKER — STATUS   (profile: {config.ACTIVE_PROFILE})")
    print("=" * 60)
    print(f"Tracked roles : {len(rows)}   ({config.TRACKER_CSV})")


def print_funnel(rows):
    c = funnel_counts(rows)
    known = set(FUNNEL) | set(TERMINAL)
    w = max(len(s) for s in FUNNEL + TERMINAL)          # align to the longest status label
    mx = max((c.get(s, 0) for s in FUNNEL), default=0)  # scale bars to the live pipeline
    print("\nFUNNEL  (current stage of each role)")
    for s in FUNNEL:
        print(f"  {s:<{w}}  {c.get(s, 0):>4}  {_bar(c.get(s, 0), mx)}")
    print("  " + "-" * (w + 8))
    for s in TERMINAL:
        print(f"  {s:<{w}}  {c.get(s, 0):>4}")
    for s in sorted(k for k in c if k not in known):    # any non-standard status, kept visible
        print(f"  {s:<{w}}  {c.get(s, 0):>4}  (non-standard)")

    live       = sum(c.get(s, 0) for s in ACTIVE)
    closed     = sum(c.get(s, 0) for s in TERMINAL)
    reached_iv = sum(c.get(s, 0) for s in REACHED_INTERVIEW)
    print(f"\n  live (interested/applied/interview): {live}"
          f"   ·   hired: {c.get('hired', 0)}   ·   closed: {closed}")
    if reached_iv:                                      # interview -> outcome, once any happen
        print(f"  reached interview: {reached_iv}  "
              f"(hired {c.get('hired', 0)} · offer open {c.get('offer', 0)} · "
              f"still interviewing {c.get('interview', 0)} · "
              f"rejected after interview {c.get('rejected_after_interview', 0)})")


def print_followups(rows, today):
    overdue = overdue_followups(rows, today)
    print(f"\nOVERDUE FOLLOW-UPS  ({len(overdue)})")
    if not overdue:
        print("  none — every active role's follow-up is in the future or unset.")
    for d, r in overdue:
        days = (today - d).days
        print(f"  {days:>3}d overdue  {d}  {_status(r):<10} {_who(r)}")
    unset = followups_unset(rows)
    if unset:
        print(f"\n  no follow-up date set ({len(unset)} active role(s)):")
        for r in unset:
            print(f"    {_status(r):<10} {_who(r)}")


def print_deadlines(rows, today):
    upcoming = upcoming_deadlines(rows, today)
    print(f"\nUPCOMING DEADLINES  ({len(upcoming)})")
    if not upcoming:
        print("  none — no open role has a future application deadline recorded.")
    for d, r in upcoming:
        days = (d - today).days
        flag = "⚠ " if days <= URGENT_DAYS else "  "
        when = "today" if days == 0 else f"{days:>2}d"
        print(f"  {flag}{when:>5}  {d}  {_status(r):<10} {_who(r)}")
    missed = missed_deadlines(rows, today)
    if missed:
        print(f"\n  passed, still only 'interested' ({len(missed)} — window closed):")
        for d, r in missed:
            print(f"    {d}  {_who(r)}")


def main(argv):
    today = date.today()
    rows = load_tracker()
    want = set(argv)
    show_all = not (want & {"--funnel", "--followups", "--deadlines"})

    print_header(rows)
    if show_all or "--funnel" in want:
        print_funnel(rows)
    if show_all or "--followups" in want:
        print_followups(rows, today)
    if show_all or "--deadlines" in want:
        print_deadlines(rows, today)
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
