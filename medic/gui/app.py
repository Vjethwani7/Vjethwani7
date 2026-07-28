"""The medic desktop application.

A single window over the same engine the CLI uses. Diagnostics run on a
worker thread and report progress; repairs always show their plan and ask
for confirmation before anything is touched, exactly as on the command
line.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import __version__
from ..core.report import to_markdown
from ..core.util import human_bytes
from . import controller as ctrl
from .theme import (
    DARK,
    LIGHT,
    Palette,
    apply_theme,
    detect_palette,
    mono_font,
    style_menu,
    ui_font,
)

POLL_MS = 80
SEVERITY_ORDER = ["critical", "warn", "unknown", "info", "ok"]


class ScrollableFrame(ttk.Frame):
    """A vertically scrolling container.

    Tk has no such widget, so this is the usual canvas-plus-inner-frame
    arrangement, with the wheel bound only while the pointer is inside so
    it does not steal scrolling from the rest of the window.
    """

    def __init__(self, parent, palette: Palette, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self.palette = palette

        self.canvas = tk.Canvas(
            self, borderwidth=0, highlightthickness=0, background=palette.bg
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)

        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body_configure(self, _event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        # Keep the inner frame exactly as wide as the viewport so text wraps.
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(sequence)

    def _on_wheel(self, event) -> None:
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


class MedicApp:
    """The main window."""

    def __init__(self, root: tk.Tk, *, offline: bool = False, palette: Palette | None = None) -> None:
        self.root = root
        self.palette = palette or detect_palette()
        self.style = ttk.Style(root)
        self.controller = ctrl.Controller(offline=offline)
        self.pending_plans: ctrl.PlanBundle | None = None
        self.plan_window: tk.Toplevel | None = None
        self.system = self.controller.describe_system()

        root.title("medic")
        root.geometry("980x740")
        root.minsize(720, 520)

        apply_theme(root, self.style, self.palette)
        self._build_menu()
        self._build_layout()
        self._render_fixes()

        root.after(POLL_MS, self._pump)

    # -- construction -----------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        self.menus = [menubar]

        run_menu = tk.Menu(menubar, tearoff=0)
        self.menus.append(run_menu)
        run_menu.add_command(label="Run diagnostics", accelerator="Ctrl+R", command=self.run_diagnostics)
        run_menu.add_separator()
        run_menu.add_command(label="Export report…", command=self.export_report)
        run_menu.add_separator()
        run_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=run_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self.menus.append(view_menu)
        self.theme_var = tk.StringVar(value=self.palette.name)
        view_menu.add_radiobutton(
            label="Light", variable=self.theme_var, value="light", command=self._switch_theme
        )
        view_menu.add_radiobutton(
            label="Dark", variable=self.theme_var, value="dark", command=self._switch_theme
        )
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        self.menus.append(help_menu)
        help_menu.add_command(label="About medic", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)

        for menu in self.menus:
            style_menu(menu, self.palette)

        self.root.config(menu=menubar)
        self.root.bind("<Control-r>", lambda _e: self.run_diagnostics())
        self.root.bind("<Control-q>", lambda _e: self.root.destroy())

    def _build_layout(self) -> None:
        palette = self.palette

        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill="x")
        # Held so the progress strip can be packed directly beneath it;
        # winfo_children()[0] is the menu, which is not packed at all.
        self.header = header

        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="medic", style="Title.TLabel").pack(anchor="w")
        self.host_label = ttk.Label(
            left,
            text=f"{self.system['host']} · {self.system['platform']}"
            + (" · root" if self.system["is_root"] else ""),
            style="Dim.TLabel",
        )
        self.host_label.pack(anchor="w")

        right = ttk.Frame(header)
        right.pack(side="right")
        self.profile_var = tk.StringVar(value="full")
        profile = ttk.Combobox(
            right,
            textvariable=self.profile_var,
            values=("full", "quick"),
            state="readonly",
            width=8,
        )
        profile.pack(side="left", padx=(0, 10))
        self.run_button = ttk.Button(
            right, text="Run diagnostics", style="Accent.TButton", command=self.run_diagnostics
        )
        self.run_button.pack(side="left")

        # Progress strip, only visible while something is running.
        self.progress_frame = ttk.Frame(self.root, padding=(18, 0, 18, 10))
        self.progress_label = ttk.Label(self.progress_frame, text="", style="Dim.TLabel")
        self.progress_label.pack(anchor="w", pady=(0, 4))
        self.progress = ttk.Progressbar(self.progress_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        # Summary tiles.
        self.summary_frame = ttk.Frame(self.root, padding=(18, 0, 18, 8))
        self.summary_tiles: dict[str, ttk.Label] = {}
        for key, caption in (
            ("critical", "critical"),
            ("warn", "warnings"),
            ("info", "notes"),
            ("unknown", "unknown"),
            ("checks_run", "checks run"),
        ):
            tile = tk.Frame(
                self.summary_frame,
                background=palette.surface,
                highlightbackground=palette.border,
                highlightthickness=1,
                bd=0,
            )
            tile.pack(side="left", fill="x", expand=True, padx=(0, 8))
            value = ttk.Label(tile, text="0", style="Stat.TLabel")
            value.configure(foreground=palette.severity(key) if key != "checks_run" else palette.text_dim)
            value.pack(anchor="w", padx=14, pady=(10, 0))
            ttk.Label(tile, text=caption, style="Faint.TLabel").pack(anchor="w", padx=14, pady=(0, 10))
            self.summary_tiles[key] = value

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=18, pady=(6, 0))
        self.notebook = notebook

        self.findings_view = ScrollableFrame(notebook, palette)
        notebook.add(self.findings_view, text="Findings")

        self.fixes_view = ScrollableFrame(notebook, palette)
        notebook.add(self.fixes_view, text="Repairs")

        self.status = ttk.Label(
            self.root,
            text="Ready. Nothing on this machine changes until you approve a repair.",
            style="Dim.TLabel",
            padding=(18, 8),
        )
        self.status.pack(fill="x")

        self._show_placeholder()

    # -- rendering helpers ------------------------------------------------

    def _card(self, parent, *, accent: str | None = None) -> tk.Frame:
        """A surface-coloured panel, optionally with a coloured left edge."""
        palette = self.palette
        outer = tk.Frame(parent, background=palette.surface, highlightbackground=palette.border,
                         highlightthickness=1, bd=0)
        outer.pack(fill="x", pady=(0, 10), padx=2)

        if accent:
            stripe = tk.Frame(outer, background=accent, width=3)
            stripe.pack(side="left", fill="y")

        inner = tk.Frame(outer, background=palette.surface)
        inner.pack(side="left", fill="both", expand=True, padx=14, pady=12)
        return inner

    def _chip(self, parent, text: str, fg: str, bg: str) -> tk.Label:
        family, size = ui_font()
        return tk.Label(
            parent,
            text=text.upper(),
            background=bg,
            foreground=fg,
            font=(family, max(size - 3, 7), "bold"),
            padx=6,
            pady=1,
        )

    def _show_placeholder(self) -> None:
        self.findings_view.clear()
        inner = self._card(self.findings_view.body)
        ttk.Label(inner, text="Nothing checked yet", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            inner,
            text=(
                "Run the diagnostics to inspect disks, memory, CPU, services, logs,\n"
                "network and hardware. Every check is read-only."
            ),
            style="SurfaceDim.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    # -- findings ---------------------------------------------------------

    def _render_diagnosis(self, diagnosis) -> None:
        counts = diagnosis.counts()
        for key, label in self.summary_tiles.items():
            label.configure(text=str(counts.get(key, 0)))

        self.summary_frame.pack(fill="x", padx=18, pady=(0, 8), before=self.notebook)

        view = self.findings_view
        view.clear()

        rows: list[dict[str, Any]] = []
        for report in diagnosis.reports:
            for finding in report.findings:
                rows.append(
                    {
                        "severity": finding.severity.label,
                        "title": finding.title,
                        "detail": finding.detail,
                        "advice": finding.advice,
                        "check_id": finding.check_id,
                        "fix_ids": finding.fix_ids,
                    }
                )
            if report.error:
                rows.append(
                    {
                        "severity": "unknown",
                        "title": f"{report.name} could not complete",
                        "detail": report.error,
                        "advice": "This is a fault in the check itself.",
                        "check_id": report.check_id,
                        "fix_ids": [],
                    }
                )

        rows.sort(key=lambda row: SEVERITY_ORDER.index(row["severity"]))

        if not rows:
            inner = self._card(self.findings_view.body, accent=self.palette.ok)
            ttk.Label(inner, text="No problems found", style="Heading.TLabel").pack(anchor="w")
            ttk.Label(
                inner,
                text="Every check that could run on this machine came back healthy.",
                style="SurfaceDim.TLabel",
            ).pack(anchor="w", pady=(4, 0))
        else:
            for row in rows:
                self._render_finding(row)

        skipped = [r for r in diagnosis.reports if r.skipped_reason]
        if skipped:
            inner = self._card(self.findings_view.body)
            ttk.Label(
                inner, text=f"{len(skipped)} checks did not run", style="Heading.TLabel"
            ).pack(anchor="w")
            for report in skipped:
                ttk.Label(
                    inner,
                    text=f"{report.check_id} — {report.skipped_reason}",
                    style="Faint.TLabel",
                ).pack(anchor="w", pady=(2, 0))

        view.scroll_to_top()
        self._render_fixes()

    def _render_finding(self, row: dict[str, Any]) -> None:
        palette = self.palette
        severity = row["severity"]
        inner = self._card(self.findings_view.body, accent=palette.severity(severity))

        head = tk.Frame(inner, background=palette.surface)
        head.pack(fill="x")

        self._chip(
            head, severity, palette.severity(severity), palette.severity_bg(severity)
        ).pack(side="left", padx=(0, 8))

        family, size = ui_font()
        tk.Label(
            head,
            text=row["title"],
            background=palette.surface,
            foreground=palette.text,
            font=(family, size, "bold"),
            anchor="w",
            justify="left",
            wraplength=620,
        ).pack(side="left", fill="x", expand=True)

        mono_family, mono_size = mono_font()
        tk.Label(
            head,
            text=row["check_id"],
            background=palette.surface,
            foreground=palette.text_faint,
            font=(mono_family, mono_size),
        ).pack(side="right")

        if row["detail"]:
            detail = tk.Label(
                inner,
                text=row["detail"],
                background=palette.surface_alt,
                foreground=palette.text_dim,
                font=(mono_family, mono_size),
                justify="left",
                anchor="w",
                padx=10,
                pady=8,
            )
            detail.pack(fill="x", pady=(8, 0))

        if row["advice"]:
            tk.Label(
                inner,
                text=row["advice"],
                background=palette.surface,
                foreground=palette.text_dim,
                font=(family, size),
                justify="left",
                anchor="w",
                wraplength=760,
            ).pack(fill="x", pady=(8, 0))

        known = {fix["id"] for fix in self.controller.fixes()}
        available = [fix_id for fix_id in row["fix_ids"] if fix_id in known]
        if available:
            actions = tk.Frame(inner, background=palette.surface)
            actions.pack(fill="x", pady=(10, 0))
            for fix_id in available:
                ttk.Button(
                    actions,
                    text=f"Preview: {fix_id}",
                    command=lambda ids=[fix_id]: self.preview(ids),
                ).pack(side="left", padx=(0, 8))

    # -- repairs ----------------------------------------------------------

    def _render_fixes(self) -> None:
        palette = self.palette
        view = self.fixes_view
        view.clear()

        note = self._card(view.body)
        ttk.Label(note, text="Repairs", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            note,
            text=(
                "Every repair shows exactly what it would do and asks before doing it.\n"
                "Risk ratings say how much damage a repair could cause if it went wrong."
            ),
            style="SurfaceDim.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        family, size = ui_font()

        for fix in self.controller.fixes():
            inner = self._card(view.body)
            head = tk.Frame(inner, background=palette.surface)
            head.pack(fill="x")

            tk.Label(
                head,
                text=fix["name"],
                background=palette.surface,
                foreground=palette.text if fix["available"] else palette.text_faint,
                font=(family, size, "bold"),
            ).pack(side="left", padx=(0, 8))

            self._chip(
                head, fix["risk"], palette.risk(fix["risk"]), palette.risk_bg(fix["risk"])
            ).pack(side="left", padx=(0, 6))

            if fix["requires_root"]:
                self._chip(head, "needs root", palette.text_faint, palette.surface_alt).pack(
                    side="left"
                )

            ttk.Button(
                head,
                text="Preview",
                state="normal" if fix["available"] else "disabled",
                command=lambda fix_id=fix["id"]: self.preview([fix_id]),
            ).pack(side="right")

            tk.Label(
                inner,
                text=fix["description"] if fix["available"] else fix["reason"],
                background=palette.surface,
                foreground=palette.text_dim,
                font=(family, size),
                justify="left",
                anchor="w",
                wraplength=740,
            ).pack(fill="x", pady=(4, 0))

    # -- actions ----------------------------------------------------------

    def run_diagnostics(self) -> None:
        if self.controller.busy:
            return
        self._set_status("Running diagnostics…")
        self.progress_frame.pack(fill="x", padx=18, pady=(0, 10), after=self.header)
        self.progress.configure(value=0)
        self.controller.start_diagnose(profile=self.profile_var.get())

    def preview(self, fix_ids: list[str]) -> None:
        if self.controller.busy:
            self._set_status("Busy — wait for the current task to finish.")
            return
        self._set_status("Working out what that would do…")
        self.controller.start_preview(fix_ids)

    def export_report(self) -> None:
        if self.controller.diagnosis is None:
            messagebox.showinfo("medic", "Run the diagnostics first.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export report",
            defaultextension=".md",
            initialfile="medic-report.md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(to_markdown(self.controller.diagnosis))
        except OSError as exc:
            messagebox.showerror("medic", f"Could not write the report:\n{exc}", parent=self.root)
            return
        self._set_status(f"Report written to {path}")

    # -- plan dialog ------------------------------------------------------

    def _show_plans(self, bundle: ctrl.PlanBundle) -> None:
        palette = self.palette
        window = tk.Toplevel(self.root)
        self.plan_window = window
        window.title("Repair preview")
        window.configure(background=palette.bg)
        window.transient(self.root)
        self._centre_over_parent(window, 700, 560)

        body = ScrollableFrame(window, palette)
        body.pack(fill="both", expand=True, padx=16, pady=(16, 8))

        family, size = ui_font()
        mono_family, mono_size = mono_font()

        for plan in bundle.plans:
            inner = self._card(body.body)
            head = tk.Frame(inner, background=palette.surface)
            head.pack(fill="x")
            tk.Label(
                head,
                text=plan.name,
                background=palette.surface,
                foreground=palette.text,
                font=(family, size, "bold"),
            ).pack(side="left", padx=(0, 8))
            self._chip(
                head, plan.risk.label, palette.risk(plan.risk.label), palette.risk_bg(plan.risk.label)
            ).pack(side="left")

            if plan.blocked:
                ttk.Label(
                    inner, text=f"Cannot run: {plan.blocked_reason}", style="Faint.TLabel"
                ).pack(anchor="w", pady=(6, 0))
                continue

            if not plan.actions:
                ttk.Label(
                    inner, text="Nothing to do — found nothing to change.", style="Faint.TLabel"
                ).pack(anchor="w", pady=(6, 0))
                continue

            for note in plan.notes:
                tk.Label(
                    inner,
                    text=note,
                    background=palette.warn_bg,
                    foreground=palette.text_dim,
                    font=(family, size),
                    justify="left",
                    anchor="w",
                    wraplength=580,
                    padx=10,
                    pady=6,
                ).pack(fill="x", pady=(8, 0))

            for action in plan.actions:
                block = tk.Frame(
                    inner,
                    background=palette.surface_alt,
                    highlightbackground=palette.border,
                    highlightthickness=1,
                )
                block.pack(fill="x", pady=(8, 0))
                label = action.description
                if action.est_bytes:
                    label += f"   (~{human_bytes(action.est_bytes)})"
                tk.Label(
                    block,
                    text=label,
                    background=palette.surface_alt,
                    foreground=palette.text,
                    font=(family, size),
                    justify="left",
                    anchor="w",
                    wraplength=580,
                ).pack(fill="x", padx=10, pady=(8, 0))

                if action.argv:
                    tk.Label(
                        block,
                        text="$ " + " ".join(action.argv),
                        background=palette.surface_alt,
                        foreground=palette.text_dim,
                        font=(mono_family, mono_size),
                        justify="left",
                        anchor="w",
                        wraplength=580,
                    ).pack(fill="x", padx=10, pady=(4, 0))

                if action.undo_note:
                    tk.Label(
                        block,
                        text=f"undo: {action.undo_note}",
                        background=palette.surface_alt,
                        foreground=palette.text_faint,
                        font=(family, max(size - 1, 8)),
                        justify="left",
                        anchor="w",
                        wraplength=580,
                    ).pack(fill="x", padx=10, pady=(4, 8))
                else:
                    tk.Frame(block, background=palette.surface_alt, height=8).pack()

        footer = ttk.Frame(window, padding=(16, 8, 16, 16))
        footer.pack(fill="x")

        if bundle.actionable:
            summary = f"Highest risk: {bundle.highest_risk}."
            if bundle.est_bytes:
                summary = f"Frees about {human_bytes(bundle.est_bytes)}. " + summary
        else:
            summary = "Nothing to apply."

        ttk.Label(footer, text=summary, style="Dim.TLabel").pack(side="left")

        ttk.Button(footer, text="Close", command=window.destroy).pack(side="right")

        if bundle.actionable:
            self.pending_plans = bundle
            ttk.Button(
                footer,
                text="Apply these changes",
                style="Danger.TButton" if bundle.highest_risk != "safe" else "Accent.TButton",
                command=lambda: self._confirm_apply(window, bundle),
            ).pack(side="right", padx=(0, 8))

    def _centre_over_parent(self, window: tk.Toplevel, width: int, height: int) -> None:
        """Place a dialog over the middle of the main window.

        Tk puts new toplevels wherever the window manager likes, which on a
        bare session means the top-left corner.
        """
        self.root.update_idletasks()
        parent_x = self.root.winfo_rootx()
        parent_y = self.root.winfo_rooty()
        parent_w = self.root.winfo_width()
        parent_h = self.root.winfo_height()

        x = max(0, parent_x + (parent_w - width) // 2)
        y = max(0, parent_y + (parent_h - height) // 3)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _confirm_apply(self, window: tk.Toplevel, bundle: ctrl.PlanBundle) -> None:
        """Second gate. The plan is on screen; this is the point of no return."""
        message = (
            "Apply these changes to this computer?\n\n"
            f"Highest risk: {bundle.highest_risk}\n"
        )
        if bundle.est_bytes:
            message += f"Frees about {human_bytes(bundle.est_bytes)}\n"
        message += "\nThis cannot be undone automatically."

        if not messagebox.askyesno("Confirm repair", message, parent=window, icon="warning"):
            return

        window.destroy()
        self.plan_window = None
        self._set_status("Applying repairs…")
        self.progress.configure(value=0)
        self.progress_frame.pack(fill="x", padx=18, pady=(0, 10), after=self.header)
        self.controller.start_apply(bundle.fix_ids)

    def _show_results(self, results: list) -> None:
        lines = []
        freed = 0
        for result in results:
            lines.append(result.name)
            for item in result.results:
                mark = "✓" if item.ok else "✗"
                suffix = f" — {item.message}" if item.message else ""
                lines.append(f"   {mark} {item.description}{suffix}")
            if result.blocked_reason:
                lines.append(f"   blocked: {result.blocked_reason}")
            if result.skipped_reason:
                lines.append(f"   skipped: {result.skipped_reason}")
            freed += result.bytes_freed

        summary = "\n".join(lines) or "Nothing was changed."
        if freed:
            summary += f"\n\nReclaimed about {human_bytes(freed)}."
        summary += "\n\nRecorded in the journal (medic history)."

        messagebox.showinfo("Repair results", summary, parent=self.root)
        self._set_status(
            f"Repairs finished. Reclaimed {human_bytes(freed)}." if freed else "Repairs finished."
        )
        self._render_fixes()

    # -- event pump -------------------------------------------------------

    def _pump(self) -> None:
        """Drain worker events on the UI thread. Tk requires this."""
        for event in self.controller.drain():
            self._handle(event)
        self.root.after(POLL_MS, self._pump)

    def _handle(self, event: ctrl.Event) -> None:
        if event.kind == ctrl.PROGRESS:
            payload = event.payload or {}
            total = payload.get("total") or 0
            current = payload.get("current") or 0
            self.progress.configure(value=(current / total * 100) if total else 0)
            self.progress_label.configure(
                text=f"{payload.get('label', '')}   ({current}/{total})" if total else ""
            )

        elif event.kind == ctrl.BUSY:
            running = bool(event.payload)
            self.run_button.configure(state="disabled" if running else "normal")
            if not running:
                self.progress_frame.pack_forget()

        elif event.kind == ctrl.DIAGNOSIS:
            self._render_diagnosis(event.payload)
            counts = event.payload.counts()
            worst = event.payload.severity.label
            self._set_status(
                f"Done — {counts['critical']} critical, {counts['warn']} warnings "
                f"({counts['checks_run']} checks). Overall: {worst}."
            )

        elif event.kind == ctrl.PLANS:
            self._set_status("Ready.")
            self._show_plans(event.payload)

        elif event.kind == ctrl.RESULTS:
            self._show_results(event.payload)

        elif event.kind == ctrl.FAILED:
            self._set_status("Something went wrong.")
            messagebox.showerror("medic", str(event.payload), parent=self.root)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # -- misc -------------------------------------------------------------

    def _switch_theme(self) -> None:
        self.palette = DARK if self.theme_var.get() == "dark" else LIGHT
        apply_theme(self.root, self.style, self.palette)
        for menu in self.menus:
            style_menu(menu, self.palette)
        messagebox.showinfo(
            "medic",
            "The theme applies fully once the view is redrawn.\nRe-run the diagnostics to refresh.",
            parent=self.root,
        )
        self._render_fixes()

    def _about(self) -> None:
        messagebox.showinfo(
            "About medic",
            f"medic {__version__}\n\n"
            "Diagnoses and repairs common problems on this computer.\n\n"
            "Everything runs locally. Nothing is uploaded anywhere.\n"
            "Repairs always show their plan and ask before running.",
            parent=self.root,
        )


def launch(*, offline: bool = False) -> int:
    """Open the desktop app. Returns a process exit code."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: could not open a window: {exc}", file=sys.stderr)
        print("       (is a display available? try `medic serve` instead)", file=sys.stderr)
        return 1

    MedicApp(root, offline=offline)
    root.mainloop()
    return 0
