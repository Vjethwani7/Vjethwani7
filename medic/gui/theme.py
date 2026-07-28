"""Colour palettes and ttk styling for the desktop app.

Tk has no notion of a system colour scheme, so the palette is explicit and
the app picks one at startup with a best-effort look at the desktop
settings. The View menu overrides it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_text: str
    critical: str
    warn: str
    info: str
    ok: str
    unknown: str
    critical_bg: str
    warn_bg: str
    info_bg: str
    ok_bg: str
    unknown_bg: str

    def severity(self, level: str) -> str:
        return {
            "critical": self.critical,
            "warn": self.warn,
            "info": self.info,
            "ok": self.ok,
            "unknown": self.unknown,
        }.get(level, self.text_dim)

    def severity_bg(self, level: str) -> str:
        return {
            "critical": self.critical_bg,
            "warn": self.warn_bg,
            "info": self.info_bg,
            "ok": self.ok_bg,
            "unknown": self.unknown_bg,
        }.get(level, self.surface_alt)

    def risk(self, level: str) -> str:
        return {"safe": self.ok, "moderate": self.warn, "risky": self.critical}.get(
            level, self.text_dim
        )

    def risk_bg(self, level: str) -> str:
        return {"safe": self.ok_bg, "moderate": self.warn_bg, "risky": self.critical_bg}.get(
            level, self.surface_alt
        )


LIGHT = Palette(
    name="light",
    bg="#f4f5f7",
    surface="#ffffff",
    surface_alt="#f7f8fa",
    border="#dfe3e8",
    text="#14181f",
    text_dim="#59616e",
    text_faint="#8b929d",
    accent="#1f5fa8",
    accent_text="#ffffff",
    critical="#b3271f",
    warn="#8a5900",
    info="#1f5fa8",
    ok="#1a6f45",
    unknown="#5f4390",
    critical_bg="#fbe9e8",
    warn_bg="#fbf1de",
    info_bg="#e9f0fa",
    ok_bg="#e6f4ec",
    unknown_bg="#f0eaf9",
)

DARK = Palette(
    name="dark",
    bg="#0f1319",
    surface="#171d26",
    surface_alt="#1d242e",
    border="#2a323d",
    text="#e5e9ef",
    text_dim="#98a2b0",
    text_faint="#6c7686",
    accent="#3178c6",
    accent_text="#ffffff",
    critical="#ff8177",
    warn="#e0b13f",
    info="#79b8ff",
    ok="#57d199",
    unknown="#b392f0",
    critical_bg="#2e1719",
    warn_bg="#2b2317",
    info_bg="#152131",
    ok_bg="#12241d",
    unknown_bg="#201a2e",
)


def detect_palette() -> Palette:
    """Guess the desktop's colour scheme. Falls back to light."""
    if os.environ.get("MEDIC_THEME", "").lower() in ("dark", "light"):
        return DARK if os.environ["MEDIC_THEME"].lower() == "dark" else LIGHT

    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return DARK if "dark" in result.stdout.lower() else LIGHT

        if sys.platform.startswith("linux"):
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if "dark" in result.stdout.lower():
                return DARK

        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return LIGHT if value else DARK
    except Exception:
        # Any failure just means we do not know; light is the safe default.
        pass

    return LIGHT


def mono_font() -> tuple[str, int]:
    """A monospace family that actually exists on this platform."""
    if sys.platform == "darwin":
        return ("SF Mono", 11)
    if sys.platform == "win32":
        return ("Consolas", 9)
    return ("DejaVu Sans Mono", 9)


def ui_font() -> tuple[str, int]:
    if sys.platform == "darwin":
        return ("SF Pro Text", 13)
    if sys.platform == "win32":
        return ("Segoe UI", 9)
    return ("DejaVu Sans", 10)


def apply_theme(root, style, palette: Palette) -> None:
    """Configure ttk widgets to match the palette.

    ttk's default themes hard-code colours that clash badly with a dark
    palette, so 'clam' is used as the base - it is the one built-in theme
    that honours nearly everything set here.
    """
    family, size = ui_font()

    style.theme_use("clam")
    root.configure(background=palette.bg)

    style.configure(".", background=palette.bg, foreground=palette.text, font=(family, size))
    style.configure("TFrame", background=palette.bg)
    style.configure("Surface.TFrame", background=palette.surface)
    style.configure("Alt.TFrame", background=palette.surface_alt)
    style.configure("TLabel", background=palette.bg, foreground=palette.text)
    style.configure("Surface.TLabel", background=palette.surface, foreground=palette.text)
    style.configure("Dim.TLabel", background=palette.bg, foreground=palette.text_dim)
    style.configure(
        "SurfaceDim.TLabel", background=palette.surface, foreground=palette.text_dim
    )
    style.configure("Faint.TLabel", background=palette.surface, foreground=palette.text_faint)
    style.configure(
        "Title.TLabel", background=palette.bg, foreground=palette.text, font=(family, size + 6, "bold")
    )
    style.configure(
        "Heading.TLabel",
        background=palette.surface,
        foreground=palette.text,
        font=(family, size + 1, "bold"),
    )
    style.configure(
        "Stat.TLabel",
        background=palette.surface,
        foreground=palette.text,
        font=(family, size + 8, "bold"),
    )

    style.configure(
        "TButton",
        background=palette.surface_alt,
        foreground=palette.text,
        bordercolor=palette.border,
        lightcolor=palette.surface_alt,
        darkcolor=palette.surface_alt,
        focuscolor=palette.accent,
        padding=(12, 6),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("active", palette.border), ("disabled", palette.surface)],
        foreground=[("disabled", palette.text_faint)],
    )

    style.configure(
        "Accent.TButton",
        background=palette.accent,
        foreground=palette.accent_text,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        bordercolor=palette.accent,
    )
    style.map(
        "Accent.TButton",
        background=[("active", palette.accent), ("disabled", palette.border)],
        foreground=[("disabled", palette.text_faint)],
    )

    style.configure(
        "Danger.TButton",
        background=palette.critical,
        foreground="#ffffff",
        lightcolor=palette.critical,
        darkcolor=palette.critical,
        bordercolor=palette.critical,
    )
    style.map("Danger.TButton", background=[("active", palette.critical)])

    style.configure(
        "TNotebook", background=palette.bg, bordercolor=palette.border, tabmargins=(6, 6, 6, 0)
    )
    style.configure(
        "TNotebook.Tab",
        background=palette.bg,
        foreground=palette.text_dim,
        padding=(16, 8),
        bordercolor=palette.border,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette.surface)],
        foreground=[("selected", palette.text)],
        expand=[("selected", (0, 0, 0, 0))],
    )

    style.configure(
        "TProgressbar",
        background=palette.accent,
        troughcolor=palette.border,
        bordercolor=palette.border,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        thickness=6,
    )

    style.configure(
        "TCombobox",
        fieldbackground=palette.surface,
        background=palette.surface_alt,
        foreground=palette.text,
        arrowcolor=palette.text_dim,
        bordercolor=palette.border,
        selectbackground=palette.surface,
        selectforeground=palette.text,
        padding=(6, 4),
    )
    # A readonly combobox is a separate ttk state, and it ignores the
    # configure() above - without this the text is white on white in dark
    # mode. The dropdown list is a Tk widget, not ttk, so it is set through
    # the option database.
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.surface), ("disabled", palette.bg)],
        foreground=[("readonly", palette.text), ("disabled", palette.text_faint)],
        selectbackground=[("readonly", palette.surface)],
        selectforeground=[("readonly", palette.text)],
        background=[("readonly", palette.surface_alt), ("active", palette.border)],
        arrowcolor=[("readonly", palette.text_dim)],
    )
    root.option_add("*TCombobox*Listbox.background", palette.surface)
    root.option_add("*TCombobox*Listbox.foreground", palette.text)
    root.option_add("*TCombobox*Listbox.selectBackground", palette.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", palette.accent_text)

    style.configure(
        "Vertical.TScrollbar",
        background=palette.surface_alt,
        troughcolor=palette.bg,
        bordercolor=palette.bg,
        arrowcolor=palette.text_dim,
        relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", palette.border)])

    style.configure("TSeparator", background=palette.border)


def style_menu(menu, palette: Palette) -> None:
    """Colour a tk.Menu to match the palette.

    Menus are classic Tk widgets that ttk styling does not reach. macOS
    draws the menu bar natively and ignores all of this, which is correct
    behaviour there, so failures are not worth reporting.
    """
    family, size = ui_font()
    with contextlib.suppress(Exception):
        menu.configure(
            background=palette.surface,
            foreground=palette.text,
            activebackground=palette.accent,
            activeforeground=palette.accent_text,
            selectcolor=palette.accent,
            borderwidth=0,
            relief="flat",
            font=(family, size),
        )
