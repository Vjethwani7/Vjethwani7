# medic

A tool that inspects your computer, explains what is wrong in plain language,
and repairs what it safely can. Use it from the terminal, or from a local web
dashboard.

```
$ medic diagnose

medic  laptop  Ubuntu 24.04 LTS  x86_64
───────────────────────────────────────
  CRIT  Filesystem / is 96% full
          /  438.2 GB used of 468.0 GB  (96%), 18.7 GB free
          → Free space now - below a few percent, writes start failing and
            applications behave unpredictably.
          → medic fix clean.package-cache, medic fix clean.user-cache

  warn  2 failed system service(s)
          bluetooth.service  -  exit-code (exit 1)
          docker.service     -  timeout
          → Inspect one with `systemctl status bluetooth.service`.

  warn  Swap is 82% used
          6.5 GB of 8.0 GB swap in use. Heavy swapping makes everything feel
          slow because the disk is far slower than RAM.

  1 critical, 2 warn  (24 checks in 4231 ms)

  Suggested repairs
    medic fix clean.package-cache clean.user-cache          # preview
    medic fix clean.package-cache clean.user-cache --apply  # actually do it
```

Prefer a UI? `medic serve` puts the same engine behind a local web page —
see [the dashboard](#the-dashboard).

## Install

Requires Python 3.9 or newer. No third-party dependencies — a tool you reach
for when the machine is already misbehaving should not need a working package
installer first.

```bash
git clone https://github.com/Vjethwani7/Vjethwani7.git
cd Vjethwani7
pip install -e .
```

Or run it straight from the source tree without installing anything:

```bash
python3 -m medic diagnose
```

## How it works

`medic` is built from two kinds of component:

- **Checks** are strictly read-only. They inspect disks, memory, CPU,
  services, logs, network, and hardware, and produce *findings*.
- **Fixes** are repairs. Each one is rated for risk, previews itself before
  doing anything, and records what it did.

Findings point at the fixes that address them, so the report tells you what to
run next rather than leaving you to work it out.

## Usage

### Diagnose

```bash
medic diagnose                      # everything (default)
medic diagnose --profile quick      # skip the slower checks
medic diagnose --only disk          # just the disk checks
medic diagnose --only disk.space -v # one check, with raw evidence
medic diagnose --skip net           # everything except network checks
medic diagnose --offline            # never touch the network
medic diagnose --json               # machine-readable
medic diagnose --markdown report.md # a file you can send to someone
```

`diagnose` never modifies anything. That is enforced by a test, not just by
convention: the suite spies on every command a diagnostic run issues and fails
if any of them could change state.

Exit codes make it usable from scripts and cron:

| Code | Meaning |
| --- | --- |
| `0` | nothing worse than informational |
| `1` | warnings found |
| `2` | critical problems found |
| `3` | usage error |

### Fix

**Fixes preview by default.** Running `medic fix` shows you exactly what would
happen and changes nothing. Adding `--apply` is what actually does it.

```bash
medic fix clean.user-cache            # preview a single repair
medic fix clean.user-cache --apply    # apply it, confirming interactively
medic fix --all                       # preview every safe repair
medic fix --all --risk moderate       # include moderate-risk repairs
medic fix clean.tmp --apply --yes     # no confirmation prompt
```

A preview reports the exact commands, the space each step reclaims, and how to
undo it:

```
  Remove stale application caches  [safe]  clean.user-cache
    note: Only entries untouched for 30+ days are removed; active caches are
          left alone.
    would do:
      - Clear 12 stale cache director(ies) in ~/.cache  (~3.4 GB)
        undo: not reversible - these files are deleted, not moved to trash

  Preview only - nothing was changed.
  Applying these would free about 3.4 GB.
```

### The dashboard

If you would rather click than type, `medic serve` puts the same engine
behind a local web page:

```bash
medic serve                 # read-only dashboard on localhost
medic serve --open          # …and open a browser
medic serve --allow-fixes   # permit applying repairs from the page
```

It prints a URL containing a one-off session token:

```
medic dashboard
───────────────
  http://localhost:8765/?token=szjj8RcSX1HfffMLI7P2xdB2aXtQd3GZ

  Read-only: previews work, applying is disabled.
  Restart with --allow-fixes to enable repairs.
```

The page shows findings as severity-coded cards with the same detail and
advice as the terminal, a live progress bar while checks run, and a plan
dialog that lists the exact commands a repair would issue before you
approve it. It follows your system light/dark setting.

Because this exposes a "modify my computer" API over a socket, it is
locked down harder than a normal web app:

- **Loopback only.** Binding any other address is refused outright; there
  is a hidden override flag, and using it is your problem.
- **Token required.** Every API call must carry the token minted at
  startup, compared in constant time. The page strips it from the address
  bar on load so it does not linger in history.
- **`Host` header validated.** This is what stops a malicious website from
  pointing a hostname at `127.0.0.1` and driving your dashboard through
  your own browser — a DNS-rebinding attack.
- **Same-origin required** for anything that changes state.
- **Read-only by default.** `/api/fix/apply` returns 403 unless the server
  was started with `--allow-fixes`. Previewing is always allowed, because
  a preview cannot change anything.
- **Strict CSP** with `default-src 'none'` and `connect-src 'self'`, so a
  diagnostic report cannot be exfiltrated even if the page were tampered
  with. No external fonts, scripts, or images — the front end has no build
  step and no dependencies, same as the tool it fronts.

### Everything else

```bash
medic list                # all checks and fixes
medic explain disk.space  # what a check looks at, and what fixes it
medic history             # everything medic has ever changed on this machine
medic config              # current thresholds
medic config --init       # write them to ~/.config/medic/config.json to edit
```

## Safety

This tool takes real access to your computer, so here is precisely what it
will and will not do.

**Nothing runs without you asking.** There is no background agent, no
scheduled task, and no auto-start. `medic diagnose` and `medic fix` do exactly
one run per command and then exit. `medic serve` is the one long-running mode,
and it only lives as long as you leave that terminal open — it listens on
localhost, holds no privileges you did not already have, and stops on Ctrl+C.

**Nothing leaves your machine.** No telemetry, no uploads, no analytics. The
only network traffic `medic` ever generates is the connectivity check, which
opens a TCP connection to `1.1.1.1:53` and `8.8.8.8:53` and resolves two
hostnames, purely to tell "the network is down" apart from "DNS is broken". It
sends no payload and identifies nothing. `--offline` disables even that.

**Preview is the default, everywhere.** No fix changes anything unless you
pass `--apply`. With `--apply` you are still asked to confirm each fix
individually, after seeing its full plan, unless you pass `--yes`.

**Risk is stated up front.** Every fix carries a rating:

| Rating | Meaning | Examples |
| --- | --- | --- |
| `safe` | removes only regenerable data | `clean.user-cache`, `clean.package-cache`, `clean.journal` |
| `moderate` | irreversible, but limited to caches, temp files, and already-deleted items | `clean.tmp`, `clean.trash`, `clean.docker`, `services.restart-failed` |
| `risky` | can change how your system behaves | `updates.apply` |

`medic fix --all` only runs `safe` fixes unless you raise the limit with
`--risk`.

**Deletion is fenced in.** Every path a fix would remove passes through a set
of guardrails first. A path is refused unless it is absolute, exists, resolves
*after following symlinks* to somewhere inside an explicitly allowed root, is
not a mount point, is not a system directory, and is not your home directory
itself. A symlink inside a cache pointing at your documents does not get
followed — that case has a test.

**Privileges are never escalated silently.** `medic` never invokes `sudo` for
you. A fix that needs root says so and stops, and you decide whether to re-run
it with `sudo medic fix ...`.

**Everything is recorded.** Every applied change is appended to
`~/.local/state/medic/journal.jsonl` and readable with `medic history`.

**What it will not do:** kill your processes, edit config files, change
firewall rules, remove kernels, touch Docker named volumes, or empty your trash
without being asked by name.

## What it checks

| Area | Checks |
| --- | --- |
| Disk | free space, inode exhaustion, filesystems forced read-only by I/O errors, the largest directories in your home |
| Memory | RAM pressure with the processes responsible, swap thrashing, kernel out-of-memory kills |
| CPU | load average per core, runaway processes, zombies, tasks stuck in uninterruptible I/O |
| Services | failed systemd units (system and user), restart loops, failed launchd agents on macOS |
| Logs | journal and `/var/log` size, repeated errors in the last 24 hours grouped by pattern |
| Network | connectivity vs. DNS failure, resolver configuration, services listening on all interfaces |
| Hardware | SMART drive health and failure-predicting attributes, temperature, battery wear |
| System | uptime, pending reboots, firewall state, world-writable directories in `PATH`, pending and security updates, clock synchronisation |

Checks that cannot run — wrong OS, missing tool, insufficient privileges —
report *why* they were skipped rather than silently passing. A check that
cannot determine an answer says "unknown"; it never reports healthy by
default.

## Platform support

Linux is the primary target and has the deepest coverage. macOS is well
supported for disk, memory, CPU, network, launchd, battery, and time
synchronisation. Windows currently covers disk and memory only; the remaining
checks report themselves as unsupported rather than guessing.

## Configuration

Thresholds live in `~/.config/medic/config.json`. Write the current values out
with `medic config --init`, then edit. Anything not mentioned keeps its
default, and unknown keys are ignored so a config file survives upgrades.

```json
{
  "disk_warn_percent": 85.0,
  "disk_critical_percent": 93.0,
  "cache_stale_days": 30.0,
  "uptime_warn_days": 45.0
}
```

Defaults are deliberately quiet. A check that fires on every healthy laptop is
noise, and noise is what makes people stop reading diagnostics.

## Extending it

Add a check by dropping a module in `medic/checks/`:

```python
from medic.core.base import Check
from medic.core.probe import processes
from medic.core.registry import register_check


@register_check
class TooManyBrowserTabs(Check):
    id = "fun.tabs"
    name = "Browser tabs"
    description = "Counts browser processes."
    category = "fun"

    def run(self, ctx):
        tabs = sum(1 for p in processes(ctx) if "renderer" in p.command)
        if tabs > 100:
            yield self.warn(
                f"{tabs} browser tabs open",
                advice="This is why your laptop is warm.",
            )
```

It is discovered automatically. Fixes work the same way with
`medic/fixes/`, subclassing `Fix` and returning a `FixPlan` from `plan()`.
The one rule for fixes: `plan()` must not change anything. All mutation
belongs in the actions it returns, so that previewing is always safe.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

245 tests, no network access required. The suite scripts all system command
output through a fake context, so tests never depend on the machine running
them — except for two areas where being sure matters more than being fast:
the guardrail tests use real temporary directories, and the dashboard tests
start a real HTTP server on an ephemeral port and drive it over the wire.

## License

MIT — see [LICENSE](LICENSE).
