"""Command line interface."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence

from . import __version__
from .core import registry
from .core import report as reporting
from .core.base import Fix
from .core.config import Config, config_path
from .core.context import Context
from .core.journal import Journal
from .core.model import FixPlan, Risk, Severity
from .core.report import Printer
from .core.runner import apply_plan, plan_fix, run_checks
from .core.util import human_bytes

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CRITICAL = 2
EXIT_USAGE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medic",
        description=(
            "Diagnose and repair common problems on this computer. "
            "Runs locally; nothing is uploaded anywhere."
        ),
        epilog="Start with `medic diagnose`. Fixes preview by default and need --apply to run.",
    )
    parser.add_argument("--version", action="version", version=f"medic {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="show evidence and skipped checks")
    common.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    common.add_argument("--no-color", action="store_true", help="disable coloured output")
    common.add_argument("--offline", action="store_true", help="never make network connections")
    common.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="per-command timeout (default 15)",
    )
    common.add_argument(
        "--config", default=None, metavar="PATH", help=f"config file (default {config_path()})"
    )

    subparsers = parser.add_subparsers(dest="command")

    diagnose = subparsers.add_parser(
        "diagnose",
        parents=[common],
        help="inspect the system and report problems (read-only)",
        description="Run diagnostic checks. This never modifies anything.",
    )
    diagnose.add_argument(
        "--profile",
        choices=("quick", "full"),
        default="full",
        help="quick skips slower checks (default: full)",
    )
    diagnose.add_argument("--only", nargs="+", metavar="ID", help="run only these checks")
    diagnose.add_argument("--skip", nargs="+", metavar="ID", help="skip these checks")
    diagnose.add_argument("--show-ok", action="store_true", help="list checks that passed")
    diagnose.add_argument(
        "--min-severity",
        default="info",
        choices=[level.label for level in Severity],
        help="hide findings below this severity (default: info)",
    )
    diagnose.add_argument(
        "--markdown", metavar="PATH", help="also write a Markdown report to this file"
    )
    diagnose.set_defaults(func=cmd_diagnose)

    fix = subparsers.add_parser(
        "fix",
        parents=[common],
        help="repair problems (previews unless --apply is given)",
        description=(
            "Preview or apply repairs. Without --apply nothing is changed: you see "
            "exactly what would happen first."
        ),
    )
    fix.add_argument("ids", nargs="*", metavar="FIX_ID", help="fixes to run; omit to use --all")
    fix.add_argument("--all", action="store_true", help="every fix within the risk limit")
    fix.add_argument("--apply", action="store_true", help="actually make the changes")
    fix.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    fix.add_argument(
        "--risk",
        default="safe",
        choices=[level.label for level in Risk],
        help="highest risk level allowed (default: safe)",
    )
    fix.set_defaults(func=cmd_fix)

    listing = subparsers.add_parser(
        "list", parents=[common], help="show available checks and fixes"
    )
    listing.add_argument(
        "what", nargs="?", choices=("checks", "fixes", "all"), default="all"
    )
    listing.set_defaults(func=cmd_list)

    explain = subparsers.add_parser(
        "explain", parents=[common], help="describe what a check or fix does"
    )
    explain.add_argument("id", metavar="ID")
    explain.set_defaults(func=cmd_explain)

    history = subparsers.add_parser(
        "history", parents=[common], help="show what medic has changed on this machine"
    )
    history.add_argument("-n", "--count", type=int, default=20, help="entries to show")
    history.add_argument(
        "--changes-only", action="store_true", help="hide diagnostic runs, show only fixes"
    )
    history.set_defaults(func=cmd_history)

    config_cmd = subparsers.add_parser(
        "config", parents=[common], help="show or initialise the config file"
    )
    config_cmd.add_argument(
        "--init", action="store_true", help="write the current settings to the config file"
    )
    config_cmd.set_defaults(func=cmd_config)

    serve_cmd = subparsers.add_parser(
        "serve",
        parents=[common],
        help="open the dashboard in a browser",
        description=(
            "Serve the medic dashboard on this machine. Binds to localhost only, "
            "and is read-only unless --allow-fixes is given."
        ),
    )
    serve_cmd.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    serve_cmd.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind (default 127.0.0.1; anything else needs --i-know-what-im-doing)",
    )
    serve_cmd.add_argument(
        "--allow-fixes",
        action="store_true",
        help="permit applying repairs from the browser (previews always work)",
    )
    serve_cmd.add_argument(
        "--open", dest="open_browser", action="store_true", help="open a browser window"
    )
    serve_cmd.add_argument(
        "--i-know-what-im-doing",
        dest="force_bind",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    serve_cmd.set_defaults(func=cmd_serve)

    return parser


def make_context(args: argparse.Namespace) -> Context:
    config = Config.load(getattr(args, "config", None))
    ctx = Context.detect()
    ctx.config = config
    ctx.verbose = getattr(args, "verbose", False)
    ctx.offline = getattr(args, "offline", False)
    ctx.timeout = getattr(args, "timeout", None) or config.command_timeout
    ctx.journal = Journal()
    ctx.dry_run = not getattr(args, "apply", False)
    ctx.assume_yes = getattr(args, "yes", False)
    return ctx


def make_printer(args: argparse.Namespace) -> Printer:
    color = False if getattr(args, "no_color", False) else None
    return Printer(color=color)


# -- commands --------------------------------------------------------------


def cmd_diagnose(args: argparse.Namespace) -> int:
    ctx = make_context(args)
    printer = make_printer(args)

    checks = registry.all_checks()
    known = [check.id for check in checks]

    if args.only:
        bad = registry.unmatched(args.only, known)
        if bad:
            printer.write(f"error: unknown check(s): {', '.join(bad)}")
            printer.write("run `medic list checks` to see what is available")
            return EXIT_USAGE
        wanted = set(registry.resolve(args.only, known))
        checks = [check for check in checks if check.id in wanted]
    else:
        checks = [check for check in checks if args.profile in check.profiles]

    if args.skip:
        unwanted = set(registry.resolve(args.skip, known))
        checks = [check for check in checks if check.id not in unwanted]

    if not checks:
        printer.write("error: no checks selected")
        return EXIT_USAGE

    progress = _make_progress(printer, enabled=not args.json)
    diagnosis = run_checks(ctx, checks, on_start=progress)
    _clear_progress(printer, enabled=not args.json)

    if args.json:
        print(reporting.to_json(diagnosis.to_dict()))
    else:
        reporting.render_diagnosis(
            diagnosis,
            printer,
            verbose=args.verbose,
            show_ok=args.show_ok,
            minimum=Severity.parse(args.min_severity),
        )

    if args.markdown:
        try:
            with open(args.markdown, "w", encoding="utf-8") as handle:
                handle.write(reporting.to_markdown(diagnosis))
            if not args.json:
                printer.write(f"\n  Wrote report to {args.markdown}")
        except OSError as exc:
            printer.write(f"error: could not write {args.markdown}: {exc}")
            return EXIT_USAGE

    if diagnosis.severity >= Severity.CRITICAL:
        return EXIT_CRITICAL
    if diagnosis.severity >= Severity.WARN:
        return EXIT_PROBLEMS
    return EXIT_OK


def cmd_fix(args: argparse.Namespace) -> int:
    ctx = make_context(args)
    printer = make_printer(args)
    dry_run = not args.apply

    all_fixes = registry.all_fixes()
    known = [fix.id for fix in all_fixes]

    if args.ids:
        bad = registry.unmatched(args.ids, known)
        if bad:
            printer.write(f"error: unknown fix(es): {', '.join(bad)}")
            printer.write("run `medic list fixes` to see what is available")
            return EXIT_USAGE
        wanted = registry.resolve(args.ids, known)
        selected = [fix for fix in all_fixes if fix.id in wanted]
        # Explicitly named fixes are allowed up to their own risk level;
        # asking for a fix by name is consent to that fix's risk.
        max_risk = max((fix.risk for fix in selected), default=Risk.SAFE)
        if args.risk:
            max_risk = max(max_risk, Risk.parse(args.risk))
    elif args.all:
        selected = all_fixes
        max_risk = Risk.parse(args.risk)
    else:
        printer.write("error: name at least one fix, or pass --all")
        printer.write("run `medic list fixes` to see what is available")
        return EXIT_USAGE

    runnable, excluded = _split(ctx, selected, max_risk)

    if not args.json:
        printer.header("Repair preview" if dry_run else "Applying repairs")

    plans: list[tuple[Fix, FixPlan]] = [(fix, plan_fix(ctx, fix)) for fix in runnable]
    actionable = [(fix, plan) for fix, plan in plans if not plan.blocked and not plan.empty]

    if args.json:
        return _fix_json(ctx, plans, excluded, dry_run=dry_run, printer=printer)

    for fix, plan in plans:
        if plan.empty and not plan.blocked and not args.verbose:
            continue
        reporting.render_plan(fix, plan, printer, dry_run=dry_run)

    for fix, reason in excluded:
        if args.verbose or args.ids:
            printer.write()
            printer.write(f"  {fix.name}  {printer.paint(fix.id, reporting.DIM)}")
            printer.write(f"    {printer.paint('skipped: ' + reason, reporting.DIM)}")

    if not actionable:
        printer.write()
        printer.write("  Nothing to do - no selected fix found anything to change.")
        return EXIT_OK

    confirm = None if args.yes else _make_confirmer(printer)
    results = []
    for fix, plan in actionable:
        result = apply_plan(ctx, fix, plan, dry_run=dry_run, confirm=confirm)
        if not dry_run:
            reporting.render_fix_result(result, printer)
        results.append(result)

    reporting.render_fix_summary(results, printer, dry_run=dry_run)

    if not dry_run and any(result.applied and not result.ok for result in results):
        return EXIT_PROBLEMS
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    printer = make_printer(args)
    checks = registry.all_checks()
    fixes = registry.all_fixes()

    if args.json:
        print(
            reporting.to_json(
                {
                    "checks": [
                        {
                            "id": check.id,
                            "name": check.name,
                            "category": check.category,
                            "description": check.description,
                            "platforms": list(check.platforms),
                            "profiles": list(check.profiles),
                        }
                        for check in checks
                    ],
                    "fixes": [
                        {
                            "id": fix.id,
                            "name": fix.name,
                            "category": fix.category,
                            "description": fix.description,
                            "risk": fix.risk.label,
                            "requires_root": fix.requires_root,
                            "platforms": list(fix.platforms),
                        }
                        for fix in fixes
                    ],
                }
            )
        )
        return EXIT_OK

    if args.what in ("checks", "all"):
        printer.header(f"Checks ({len(checks)})")
        width = max((len(check.id) for check in checks), default=0)
        for check in checks:
            printer.write(f"  {check.id.ljust(width)}  {check.description}")

    if args.what in ("fixes", "all"):
        printer.header(f"Fixes ({len(fixes)})")
        width = max((len(fix.id) for fix in fixes), default=0)
        for fix in fixes:
            root = " (root)" if fix.requires_root else ""
            tag = f"[{fix.risk.label}{root}]"
            printer.write(f"  {fix.id.ljust(width)}  {tag:<18}  {fix.description}")
        printer.write()
        printer.dim("  Fixes preview by default. Add --apply to make changes.")

    return EXIT_OK


def cmd_explain(args: argparse.Namespace) -> int:
    printer = make_printer(args)
    check = registry.get_check(args.id)
    fix = registry.get_fix(args.id)

    if not check and not fix:
        printer.write(f"error: no check or fix called {args.id!r}")
        return EXIT_USAGE

    if check:
        printer.header(f"Check: {check.id}")
        printer.write(f"  {check.name}")
        printer.write(f"  {check.description}")
        printer.write()
        printer.write(f"  category:  {check.category}")
        printer.write(f"  platforms: {', '.join(check.platforms)}")
        printer.write(f"  profiles:  {', '.join(check.profiles)}")
        printer.write()
        printer.dim(f"  run it with: medic diagnose --only {check.id} --verbose")

        related = [item for item in registry.all_fixes() if check.id in item.addresses]
        if related:
            printer.write()
            printer.write("  Fixes that address this:")
            for item in related:
                printer.write(f"    {item.id}  [{item.risk.label}]  {item.name}")

    if fix:
        printer.header(f"Fix: {fix.id}")
        printer.write(f"  {fix.name}")
        printer.write(f"  {fix.description}")
        printer.write()
        printer.write(f"  risk:      {fix.risk.label}")
        printer.write(f"  needs root: {'yes' if fix.requires_root else 'no'}")
        printer.write(f"  platforms: {', '.join(fix.platforms)}")
        if fix.addresses:
            printer.write(f"  addresses: {', '.join(fix.addresses)}")
        printer.write()
        printer.dim(f"  preview it with: medic fix {fix.id}")
        printer.dim(f"  apply it with:   medic fix {fix.id} --apply")

    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    printer = make_printer(args)
    journal = Journal()
    entries = journal.read()

    if args.changes_only:
        entries = [entry for entry in entries if entry.get("event", "").startswith("fix.")]

    entries = entries[-args.count :]

    if args.json:
        print(reporting.to_json(entries))
        return EXIT_OK

    if not entries:
        printer.write(f"  No history yet ({journal.path})")
        return EXIT_OK

    printer.header(f"History  {journal.path}")
    for entry in entries:
        stamp = entry.get("time", "?")
        event = entry.get("event", "?")
        if event == "diagnose":
            counts = entry.get("counts", {})
            summary = ", ".join(
                f"{counts.get(level, 0)} {level}"
                for level in ("critical", "warn")
                if counts.get(level)
            )
            printer.write(f"  {stamp}  diagnose  {summary or 'all clear'}")
        elif event == "fix.begin":
            printer.write(f"  {stamp}  fix       {entry.get('fix_id')}  starting")
            for description in entry.get("actions", []):
                printer.dim(f"                              - {description}")
        elif event == "fix.end":
            freed = entry.get("bytes_freed") or 0
            status = "ok" if entry.get("ok") else "FAILED"
            extra = f", freed {human_bytes(freed)}" if freed else ""
            printer.write(f"  {stamp}  fix       {entry.get('fix_id')}  {status}{extra}")
        else:
            printer.write(f"  {stamp}  {event}")

    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    printer = make_printer(args)
    path = args.config or config_path()
    config = Config.load(path)

    if args.init:
        written = config.save(path)
        printer.write(f"  Wrote {written}")
        return EXIT_OK

    if args.json:
        print(reporting.to_json(config.to_dict()))
        return EXIT_OK

    printer.header(f"Configuration  {path}")
    if not os.path.exists(path):
        printer.dim("  (file does not exist; these are the defaults)")
    for key, value in sorted(config.to_dict().items()):
        printer.write(f"  {key:<32} {value}")
    printer.write()
    printer.dim("  medic config --init writes these to disk so you can edit them.")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import serve

    printer = make_printer(args)
    config = Config.load(getattr(args, "config", None))

    # Binding off-loopback exposes an API that can modify this machine to
    # anyone who can reach the port. Refuse unless the user really insists.
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.force_bind:
        printer.write(f"error: refusing to bind {args.host} — that exposes this machine")
        printer.write("       the dashboard is designed for localhost only")
        printer.write("       pass --i-know-what-im-doing to override")
        return EXIT_USAGE

    def announce(app) -> None:
        printer.header("medic dashboard")
        printer.write(f"  {printer.paint(app.url, reporting.BOLD)}")
        printer.write()
        if app.allow_fixes:
            printer.write(
                f"  {printer.paint('Repairs can be applied from this page.', reporting.BOLD)}"
                " Each one still previews first."
            )
        else:
            printer.dim("  Read-only: previews work, applying is disabled.")
            printer.dim("  Restart with --allow-fixes to enable repairs.")
        printer.write()
        printer.dim("  The URL contains a session token. Anyone with it can use this")
        printer.dim("  dashboard, so do not share it. Press Ctrl+C to stop.")
        printer.write()

    try:
        serve(
            host=args.host,
            port=args.port,
            allow_fixes=args.allow_fixes,
            offline=args.offline,
            open_browser=args.open_browser,
            config=config,
            on_ready=announce,
        )
    except OSError as exc:
        if getattr(exc, "errno", None) in (48, 98):  # EADDRINUSE
            printer.write(f"error: port {args.port} is already in use")
            printer.write("       pick another with --port, or stop whatever is using it")
            return EXIT_USAGE
        printer.write(f"error: could not start the server: {exc}")
        return EXIT_USAGE

    printer.write("  dashboard stopped")
    return EXIT_OK


# -- helpers ---------------------------------------------------------------


def _split(ctx: Context, fixes: list[Fix], max_risk: Risk) -> tuple[list[Fix], list[tuple[Fix, str]]]:
    runnable: list[Fix] = []
    excluded: list[tuple[Fix, str]] = []
    for fix in fixes:
        if fix.risk > max_risk:
            excluded.append(
                (fix, f"risk '{fix.risk.label}' above limit; add --risk {fix.risk.label}")
            )
            continue
        reason = fix.unavailable_reason(ctx)
        if reason:
            excluded.append((fix, reason))
            continue
        runnable.append(fix)
    return runnable, excluded


def _make_confirmer(printer: Printer):
    def confirm(fix: Fix, plan: FixPlan) -> bool:
        summary = f"{len(plan.actions)} action(s)"
        if plan.est_bytes:
            summary += f", frees about {human_bytes(plan.est_bytes)}"
        prompt = f"  Apply '{fix.name}' [{plan.risk.label}] - {summary}? [y/N] "
        try:
            answer = input(prompt)
        except (EOFError, KeyboardInterrupt):
            printer.write()
            return False
        return answer.strip().lower() in ("y", "yes")

    return confirm


def _make_progress(printer: Printer, *, enabled: bool):
    if not enabled or not printer.color:
        return None

    def progress(check, index: int, total: int) -> None:
        printer.stream.write(f"\r  checking {index + 1}/{total}: {check.name[:40]:<42}")
        printer.stream.flush()

    return progress


def _clear_progress(printer: Printer, *, enabled: bool) -> None:
    if enabled and printer.color:
        printer.stream.write("\r" + " " * 60 + "\r")
        printer.stream.flush()


def _fix_json(ctx, plans, excluded, *, dry_run: bool, printer: Printer) -> int:
    results = []
    for fix, plan in plans:
        if plan.blocked or plan.empty:
            continue
        results.append(apply_plan(ctx, fix, plan, dry_run=dry_run, confirm=None).to_dict())
    print(
        reporting.to_json(
            {
                "dry_run": dry_run,
                "plans": [plan.to_dict() for _fix, plan in plans],
                "excluded": [{"fix_id": fix.id, "reason": reason} for fix, reason in excluded],
                "results": results,
            }
        )
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    # --apply without --yes in a non-interactive shell would hang on input().
    if (
        getattr(args, "apply", False)
        and not getattr(args, "yes", False)
        and not sys.stdin.isatty()
    ):
        print(
            "error: --apply needs a terminal to confirm each fix.\n"
            "       Pass --yes to skip confirmation (be sure you have previewed first).",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Someone piped us into `head`; that is not an error.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
