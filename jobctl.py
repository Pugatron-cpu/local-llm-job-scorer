"""
jobctl.py — the on/off switch for the daily scrape.

Schedules a_scrape.py (STEP A only: scrape + score) as a systemd USER timer, so it runs every
morning at 06:00 Europe/Copenhagen without you being logged in. Nothing else is automated: the
shortlist is for you to read, and c_prepare.py stays manual on purpose.

USAGE
    python jobctl.py on              # arm it: 06:00 Europe/Copenhagen, daily (idempotent)
    python jobctl.py on weekly       # ...Mondays instead    (e.g. once you've landed a job)
    python jobctl.py on monthly      # ...the 1st instead
    python jobctl.py off             # disarm. Units stay on disk; `on` re-arms them.
    python jobctl.py status          # armed? what cadence? next run? did the last one find anything?
    python jobctl.py run             # run the scrape NOW, through systemd (the real thing)
    python jobctl.py logs            # tail the scrape's output

OFF MEANS OFF. Persistent=true does NOT keep it ticking while disarmed — it only means that a run
missed because the machine was POWERED DOWN happens once at next boot, while the timer is on.

WHY A USER TIMER (not cron, not a system unit)
  - No sudo, ever: `systemctl --user enable/disable` is yours to run. That's the whole point of
    an easy switch. (It survives logout because linger is enabled for this user.)
  - Persistent=true: if the box is off or rebooting at 06:00, the run happens on next boot.
    Cron would silently skip the day.
  - The timer won't start a second run while one is still going.
  - Output lands in the journal, not in cron's mail-to-nowhere.

THE TWO THINGS THAT BREAK SCHEDULED JOBS, both handled at install time:
  - systemd does NOT source ~/.bashrc, so JOBSEARCH_OWNER (config.py reads it to pick the
    profile) is captured into the unit here. Without it the run dies resolving a profile.
  - `python` is not the venv python under systemd. The absolute interpreter path is baked in.

a_scrape.py scores through Ollama, which is a SYSTEM service this user unit can't order itself
after. Instead the unit waits (up to 60s) for Ollama to answer before starting, so a run that
fires during a slow boot waits rather than dying.
"""

import os
import re
import sys
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
NAME = "jobscrape"
TIMER = f"{NAME}.timer"
SERVICE = f"{NAME}.service"

# The cadence lives ONLY in the unit file — never mirrored into a config, so the two can't drift.
# `status` reads it back out of systemd, so what it prints is what will actually happen.
# The timezone is pinned in the calendar spec: 06:00 stays 06:00 in Denmark across DST, and even
# if the host is ever set back to UTC.
TZ = "Europe/Copenhagen"
SCHEDULES = {
    "daily":   f"*-*-* 06:00:00 {TZ}",       # every morning
    "weekly":  f"Mon *-*-* 06:00:00 {TZ}",   # Mondays
    "monthly": f"*-*-01 06:00:00 {TZ}",      # the 1st
}
DEFAULT_CADENCE = "daily"
JITTER_SEC = 300

VENV_PYTHON = os.path.join(os.path.dirname(SCRIPT_DIR), "venv", "bin", "python")
SCRAPE = os.path.join(SCRIPT_DIR, "a_scrape.py")
# Main (3090-pool) instance. jobctl schedules the DEFAULT "fast" preset, so :11434 is correct
# here. A fallback-preset schedule would need the A4000 instance's :11435 probe instead.
OLLAMA_PROBE = "http://localhost:11434/api/tags"


def _sc(*args, check=False, capture=True):
    """Run systemctl --user."""
    return subprocess.run(["systemctl", "--user", *args], check=check,
                          capture_output=capture, text=True)


def _show(prop: str) -> str:
    r = _sc("show", TIMER, "-p", prop, "--value")
    return (r.stdout or "").strip()


def _current_cadence() -> str:
    """The cadence systemd is ACTUALLY armed with, read back from the unit.

    Parses the OnCalendar value out EXACTLY rather than substring-matching it: the daily spec
    ("*-*-* 06:00:00") is a substring of the weekly one ("Mon *-*-* 06:00:00"), so a loose match
    reports a weekly timer as daily — the precise lie this read-back exists to prevent.
    Falls back to the raw spec if the unit was hand-edited to something we have no name for."""
    # e.g. "{ OnCalendar=Mon *-*-* 06:00:00 Europe/Copenhagen ; next_elapse=... }"
    cal = _show("TimersCalendar")
    m = re.search(r"OnCalendar=(.*?)\s*;", cal)
    if not m:
        return cal or "?"
    spec = m.group(1).strip()
    for name, known in SCHEDULES.items():
        if spec == known:
            return name
    return f"custom ({spec})"


def _write_units(cadence: str):
    """(Re)write the unit files. Idempotent — safe to run on every `on`."""
    owner = os.environ.get("JOBSEARCH_OWNER", "").strip()
    if not owner:
        sys.exit("JOBSEARCH_OWNER is unset in this shell. `export JOBSEARCH_OWNER=<name>` first "
                 "(it's in your ~/.bashrc) — it must be captured into the unit, because systemd "
                 "never sources ~/.bashrc.")
    for path, ok in ((VENV_PYTHON, os.path.isfile), (SCRAPE, os.path.isfile)):
        if not ok(path):
            sys.exit(f"Not found: {path}")

    # Wait for Ollama rather than dying on it: a 06:00 run during a slow boot should wait.
    wait = (f"/bin/sh -c 'for i in $(seq 30); do curl -sf {OLLAMA_PROBE} >/dev/null && exit 0; "
            f"sleep 2; done; echo \"ollama not answering at {OLLAMA_PROBE}\" >&2; exit 1'")

    os.makedirs(UNIT_DIR, exist_ok=True)
    with open(os.path.join(UNIT_DIR, SERVICE), "w") as f:
        f.write(f"""[Unit]
Description=Job scrape + score (a_scrape.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={SCRIPT_DIR}
Environment=JOBSEARCH_OWNER={owner}
ExecStartPre={wait}
ExecStart={VENV_PYTHON} {SCRAPE}
TimeoutStartSec=2h
""")
    with open(os.path.join(UNIT_DIR, TIMER), "w") as f:
        f.write(f"""[Unit]
Description=Job scrape ({cadence}) at 06:00 {TZ}

[Timer]
OnCalendar={SCHEDULES[cadence]}
Persistent=true
RandomizedDelaySec={JITTER_SEC}
Unit={SERVICE}

[Install]
WantedBy=timers.target
""")
    _sc("daemon-reload")


def cmd_on(cadence: str = DEFAULT_CADENCE):
    if cadence not in SCHEDULES:
        sys.exit(f"Unknown cadence '{cadence}'. Pick one of: {', '.join(SCHEDULES)}")
    _write_units(cadence)
    _sc("enable", "--now", TIMER, check=True)     # rewriting + re-enabling switches cadence
    print(f"ON ({cadence}) — armed. Next run: {_show('NextElapseUSecRealtime') or 'pending'}")
    print(f"     {SCHEDULES[cadence]}  (+ up to {JITTER_SEC // 60} min jitter), "
          f"catches up after downtime.")
    print(f"     Change cadence: python jobctl.py on [{' | '.join(SCHEDULES)}]")
    print("     Turn it off:    python jobctl.py off")


def cmd_off():
    if _sc("is-enabled", TIMER).stdout.strip() != "enabled":
        print("OFF — it wasn't armed.")
        return
    _sc("disable", "--now", TIMER, check=True)
    print("OFF — disarmed. No scrape will run. The units stay on disk; `python jobctl.py on` "
          "re-arms them.")


def cmd_status():
    enabled = _sc("is-enabled", TIMER).stdout.strip() or "not installed"
    print(f"timer      : {'ON' if enabled == 'enabled' else f'OFF ({enabled})'}")
    if enabled == "enabled":
        print(f"cadence    : {_current_cadence()}")   # read from systemd, not from a config
        print(f"next run   : {_show('NextElapseUSecRealtime') or '?'}")

    # Only report a result if the unit has ACTUALLY run. systemd reports Result=success for a
    # service that has never started, which would read as "yesterday's scrape was fine".
    last_trigger = _show("LastTriggerUSecRealtime")
    ran = _sc("show", SERVICE, "-p", "ExecMainStartTimestamp", "--value").stdout.strip()
    if not (last_trigger or ran):
        print("last run   : never (since this boot)")
    else:
        print(f"last run   : {last_trigger or ran}")
        res = _sc("show", SERVICE, "-p", "Result", "--value").stdout.strip()
        print(f"last result: {res or '?'}"
              + ("   <- check `python jobctl.py logs`" if res != "success" else ""))

    # The real health signal. systemd only knows whether the process exited 0; runs.csv knows
    # whether a scrape actually produced anything. A run that "succeeds" but scrapes 0 roles
    # (a job board changed its markup) is the failure mode that goes unnoticed for weeks.
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import csv
        import config
        with open(config.RUNS_LOG, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            r = rows[-1]
            print(f"last scrape: {r.get('run_ts', '?')}  "
                  f"{r.get('scored', '?')} scored, {r.get('matches', '?')} matches, "
                  f"{r.get('errors', '?')} errors, {float(r.get('duration_s') or 0) / 60:.0f} min"
                  f"   ({len(rows)} runs logged)")
        else:
            print("last scrape: runs.csv is empty")
    except Exception as e:
        print(f"last scrape: runs.csv unreadable ({e})")


def cmd_run():
    print("Starting the scrape now (same unit the timer uses)...")
    _sc("start", SERVICE, check=True)
    print("Started. Follow it with:  python jobctl.py logs")


def cmd_logs():
    subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n", "40", "--no-pager"])


CMDS = {"on": cmd_on, "off": cmd_off, "status": cmd_status, "run": cmd_run, "logs": cmd_logs}

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    if arg not in CMDS:
        sys.exit(f"Usage: python jobctl.py [{' | '.join(CMDS)}]\n"
                 f"       python jobctl.py on [{' | '.join(SCHEDULES)}]   (default: {DEFAULT_CADENCE})")
    if arg == "on":
        cmd_on(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CADENCE)
    else:
        CMDS[arg]()
