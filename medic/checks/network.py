"""Connectivity, name resolution, and listening sockets.

The connectivity check is the only part of medic that sends traffic off
the machine, and it only ever opens a TCP connection to a public DNS
resolver and resolves a hostname - no payload, no telemetry. Pass
``--offline`` to skip it entirely.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.registry import register_check

#: Anycast DNS resolvers, used purely as "is the internet reachable" targets.
PROBE_HOSTS = (("1.1.1.1", 53), ("8.8.8.8", 53))
PROBE_NAMES = ("cloudflare.com", "example.com")


def _tcp_probe(host: str, port: int, timeout: float) -> float | None:
    """Round-trip time in milliseconds, or None when unreachable."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - started) * 1000.0
    except OSError:
        return None


def _resolves(name: str) -> bool:
    """Whether a hostname resolves. The resolver has its own timeout."""
    try:
        socket.getaddrinfo(name, None)
        return True
    except OSError:
        return False


@register_check
class Connectivity(Check):
    id = "net.connectivity"
    name = "Internet connectivity"
    description = "Distinguishes 'no network' from 'network but broken DNS'."
    category = "network"

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if ctx.offline:
            return "skipped in offline mode"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        # Probes run concurrently: when the network is down every one of them
        # blocks for the full timeout, and doing that serially made this the
        # slowest check by an order of magnitude.
        timeout = min(ctx.timeout, 3.0)

        with ThreadPoolExecutor(max_workers=len(PROBE_HOSTS) + len(PROBE_NAMES)) as pool:
            tcp_futures = [
                pool.submit(_tcp_probe, host, port, timeout) for host, port in PROBE_HOSTS
            ]
            dns_futures = {
                name: pool.submit(_resolves, name) for name in PROBE_NAMES
            }
            latencies = [
                latency for future in tcp_futures if (latency := future.result()) is not None
            ]
            resolved = [name for name, future in dns_futures.items() if future.result()]

        reachable = bool(latencies)

        if not reachable and not resolved:
            yield self.critical(
                "No internet connectivity",
                detail=(
                    "Could not open a TCP connection to "
                    + ", ".join(f"{host}:{port}" for host, port in PROBE_HOSTS)
                    + ", and no hostname resolved."
                ),
                evidence={"tcp_reachable": False, "dns_working": False},
                advice=(
                    "Check Wi-Fi or cable first, then your router. If other devices work, "
                    "the problem is local: check your IP address and default route."
                ),
            )
            return

        if reachable and not resolved:
            yield self.critical(
                "The network works but DNS resolution is broken",
                detail=(
                    "Raw connections to public IP addresses succeed, but no hostname could "
                    "be resolved. This is why sites fail to load while the connection "
                    "'looks fine'."
                ),
                evidence={"tcp_reachable": True, "dns_working": False},
                fix_ids=["net.flush-dns"],
                advice="Flush the DNS cache, then check your configured resolvers.",
            )
            return

        if not reachable and resolved:
            yield self.warn(
                "DNS resolves but outbound connections are being blocked",
                detail="Name lookups succeed while direct TCP connections fail.",
                evidence={"tcp_reachable": False, "dns_working": True},
                advice=(
                    "A firewall, VPN, or captive portal is likely intercepting traffic. "
                    "If you are on public Wi-Fi, open a browser to sign in."
                ),
            )
            return

        if latencies and min(latencies) > 400:
            yield self.warn(
                f"Network latency is high ({min(latencies):.0f} ms to the nearest probe)",
                detail="Connections work but are slow to establish.",
                evidence={"latency_ms": round(min(latencies), 1)},
            )


@register_check
class Resolvers(Check):
    id = "net.dns"
    name = "DNS configuration"
    description = "Sanity-checks the configured nameservers."
    category = "network"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        content = ctx.read_text("/etc/resolv.conf")
        if content is None:
            yield self.unknown("Could not read /etc/resolv.conf")
            return

        servers = [
            line.split()[1]
            for line in content.splitlines()
            if line.strip().startswith("nameserver") and len(line.split()) > 1
        ]

        if not servers:
            yield self.critical(
                "No DNS nameservers are configured",
                detail="/etc/resolv.conf lists no nameserver entries.",
                evidence={"resolvers": []},
                advice=(
                    "Nothing will resolve hostnames. If you use NetworkManager or "
                    "systemd-resolved, restarting it usually rewrites this file."
                ),
            )
            return

        # A stub resolver on loopback is normal; a *only*-loopback config with
        # the stub service dead is a common broken state.
        loopback_only = all(server.startswith(("127.", "::1")) for server in servers)
        if loopback_only and ctx.has_systemd:
            status = ctx.run(["systemctl", "is-active", "systemd-resolved"])
            if status.stdout.strip() not in ("active", "activating"):
                yield self.warn(
                    "DNS points at a local stub resolver that is not running",
                    detail=(
                        f"resolv.conf lists {', '.join(servers)} but systemd-resolved is "
                        f"'{status.stdout.strip() or 'unknown'}'."
                    ),
                    evidence={"resolvers": servers},
                    fix_ids=["net.flush-dns"],
                    advice="Start it with `sudo systemctl start systemd-resolved`.",
                )
                return

        if ctx.verbose:
            yield self.info(
                f"{len(servers)} DNS resolver(s) configured",
                detail=", ".join(servers),
                evidence={"resolvers": servers},
            )


@register_check
class ListeningPorts(Check):
    id = "net.listening"
    name = "Listening network services"
    description = "Lists services accepting connections from outside this machine."
    category = "network"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has("ss") and not ctx.has("netstat"):
            return "needs ss or netstat"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        listeners = self._listeners(ctx)
        if listeners is None:
            yield self.unknown("Could not enumerate listening sockets")
            return

        external = sorted({entry for entry in listeners if not self._is_local(entry)})
        if not external:
            return

        yield self.info(
            f"{len(external)} service(s) listening on all network interfaces",
            detail="\n".join(external[:15]),
            evidence={"listeners": external},
            advice=(
                "Anything on this list is reachable by other machines on your network. "
                "If you did not intend that, bind the service to 127.0.0.1 or enable a "
                "firewall."
            ),
        )

    def _listeners(self, ctx: Context) -> list[str] | None:
        if ctx.has("ss"):
            result = ctx.run(["ss", "-tulnH"])
            if result.ok:
                return [
                    " ".join(line.split()[:5]) for line in result.lines() if line.split()
                ]
        if ctx.has("netstat"):
            result = ctx.run(["netstat", "-an"])
            if result.ok:
                return [line for line in result.lines() if "LISTEN" in line]
        return None

    @staticmethod
    def _is_local(entry: str) -> bool:
        return any(marker in entry for marker in ("127.0.0.1", "[::1]", "::1:", "/run/", "unix"))
