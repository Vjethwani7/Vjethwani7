"""Tests for individual checks, driven by scripted system output."""

from __future__ import annotations

from medic.checks.cpu import LoadAverage, RunawayProcesses
from medic.checks.disk import DiskInodes, DiskSpace, ReadOnlyMounts
from medic.checks.memory import MemoryPressure, OutOfMemoryKills, SwapPressure
from medic.checks.network import Connectivity
from medic.checks.services import FailedUnits
from medic.core.model import Severity

DF_OUTPUT = """Filesystem 1024-blocks      Used Available Capacity Mounted on
/dev/sda1      100000000  50000000  50000000      50% /
/dev/sda2      100000000  99000000    500000      99% /data
"""

DF_INODES = """Filesystem   Inodes   IUsed    IFree IUse% Mounted on
/dev/sda1   1000000  100000   900000   10% /
/dev/sda2   1000000  980000    20000   98% /data
"""

MOUNTS = """/dev/sda1 / ext4 rw,relatime 0 0
/dev/sda2 /data ext4 rw,relatime 0 0
"""


def with_filesystems(ctx, *, mounts: str = MOUNTS) -> None:
    ctx.stub(["df", "-kP"], DF_OUTPUT)
    ctx.stub(["df", "-iP"], DF_INODES)
    ctx.files["/proc/mounts"] = mounts


class TestDiskSpace:
    def test_flags_the_nearly_full_filesystem(self, ctx):
        with_filesystems(ctx)
        findings = list(DiskSpace().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert "/data" in findings[0].title

    def test_half_full_filesystem_is_silent(self, ctx):
        ctx.stub(["df", "-kP"], "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                                "/dev/sda1 100000000 50000000 50000000 50% /\n")
        ctx.stub(["df", "-iP"], "")
        ctx.files["/proc/mounts"] = "/dev/sda1 / ext4 rw 0 0\n"

        assert list(DiskSpace().run(ctx)) == []

    def test_read_only_filesystem_is_ignored(self, ctx):
        """A full read-only mount is not actionable, so it is not reported."""
        with_filesystems(ctx, mounts="/dev/sda1 / ext4 rw 0 0\n/dev/sda2 /data ext4 ro 0 0\n")
        findings = list(DiskSpace().run(ctx))
        assert findings == []

    def test_falls_back_to_the_root_filesystem_when_df_is_missing(self, ctx):
        """Without df we still report on / via shutil rather than going blind."""
        from medic.core.probe import filesystems

        mounts = filesystems(ctx)
        assert [fs.mountpoint for fs in mounts] == ["/"]
        assert mounts[0].total > 0

    def test_reports_unknown_when_nothing_can_be_read(self, ctx, monkeypatch):
        def boom(_path):
            raise OSError("no such device")

        monkeypatch.setattr("shutil.disk_usage", boom)

        findings = list(DiskSpace().run(ctx))
        assert len(findings) == 1
        assert findings[0].severity is Severity.UNKNOWN

    def test_suggests_cleanup_fixes(self, ctx):
        with_filesystems(ctx)
        finding = list(DiskSpace().run(ctx))[0]
        assert "clean.user-cache" in finding.fix_ids


class TestDiskInodes:
    def test_flags_inode_exhaustion(self, ctx):
        with_filesystems(ctx)
        findings = list(DiskInodes().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert "/data" in findings[0].title


class TestReadOnlyMounts:
    def test_intentional_read_only_mount_is_informational(self, ctx):
        with_filesystems(ctx, mounts="/dev/sda1 / ext4 rw 0 0\n/dev/sda2 /data ext4 ro 0 0\n")
        ctx.provide("dmesg")
        ctx.stub(["dmesg"], "nothing interesting here\n")

        findings = list(ReadOnlyMounts().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.INFO

    def test_forced_read_only_after_io_errors_is_critical(self, ctx):
        with_filesystems(ctx, mounts="/dev/sda1 / ext4 rw 0 0\n/dev/sda2 /data ext4 ro 0 0\n")
        ctx.provide("dmesg")
        ctx.stub(
            ["dmesg"],
            "EXT4-fs (sda2): I/O error while writing superblock\n"
            "EXT4-fs (sda2): Remounting filesystem read-only\n",
        )

        findings = list(ReadOnlyMounts().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert "forced read-only" in findings[0].title

    def test_skipped_inside_a_container(self, ctx):
        ctx.container = "docker"
        assert "container" in ReadOnlyMounts().unavailable_reason(ctx)


class TestMemory:
    def _meminfo(self, ctx, total_kb: int, available_kb: int, swap_total=0, swap_free=0) -> None:
        ctx.files["/proc/meminfo"] = (
            f"MemTotal: {total_kb} kB\n"
            f"MemFree: {available_kb} kB\n"
            f"MemAvailable: {available_kb} kB\n"
            f"SwapTotal: {swap_total} kB\n"
            f"SwapFree: {swap_free} kB\n"
        )

    def test_plenty_of_memory_is_silent(self, ctx):
        self._meminfo(ctx, 16_000_000, 8_000_000)
        assert list(MemoryPressure().run(ctx)) == []

    def test_memory_exhaustion_is_critical(self, ctx):
        self._meminfo(ctx, 16_000_000, 300_000)
        findings = list(MemoryPressure().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL

    def test_no_swap_configured_is_not_a_problem(self, ctx):
        self._meminfo(ctx, 16_000_000, 8_000_000, swap_total=0, swap_free=0)
        assert list(SwapPressure().run(ctx)) == []

    def test_heavy_swap_use_warns(self, ctx):
        self._meminfo(ctx, 16_000_000, 8_000_000, swap_total=8_000_000, swap_free=1_000_000)
        findings = list(SwapPressure().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.WARN

    def test_unreadable_meminfo_is_unknown_not_silence(self, ctx):
        findings = list(MemoryPressure().run(ctx))
        assert findings[0].severity is Severity.UNKNOWN


class TestOomKills:
    def test_detects_oom_kills(self, ctx):
        ctx.provide("dmesg")
        ctx.stub(
            ["dmesg"],
            "Out of memory: Killed process 1234 (chrome) total-vm:100kB\n",
        )
        findings = list(OutOfMemoryKills().run(ctx))

        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert "chrome" in findings[0].detail

    def test_quiet_log_produces_nothing(self, ctx):
        ctx.provide("dmesg")
        ctx.stub(["dmesg"], "usb 1-1: new high-speed USB device\n")
        assert list(OutOfMemoryKills().run(ctx)) == []


class TestLoadAverage:
    def test_idle_machine_is_silent(self, ctx, monkeypatch):
        monkeypatch.setattr("os.getloadavg", lambda: (0.1, 0.2, 0.3))
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        assert list(LoadAverage().run(ctx)) == []

    def test_oversubscribed_cpu_is_critical(self, ctx, monkeypatch):
        monkeypatch.setattr("os.getloadavg", lambda: (30.0, 32.0, 28.0))
        monkeypatch.setattr("os.cpu_count", lambda: 4)
        findings = list(LoadAverage().run(ctx))

        assert findings and findings[0].severity is Severity.CRITICAL


class TestRunawayProcesses:
    PS_FORMAT = ["ps", "-eo", "pid=,ppid=,user=,pcpu=,pmem=,rss=,state=,args="]

    def test_flags_a_cpu_hog(self, ctx):
        ctx.stub(self.PS_FORMAT, "999 1 alice 95.0 2.0 200000 R runaway-process --loop\n")
        findings = list(RunawayProcesses().run(ctx))

        assert any("95% CPU" in finding.title for finding in findings)

    def test_idle_processes_are_silent(self, ctx):
        ctx.stub(self.PS_FORMAT, "999 1 alice 0.1 0.5 20000 S some-daemon\n")
        assert list(RunawayProcesses().run(ctx)) == []

    def test_medic_does_not_report_itself(self, ctx):
        import os

        ctx.stub(
            self.PS_FORMAT,
            f"{os.getpid()} 1 root 99.0 1.0 100000 R python3 -m medic diagnose\n",
        )
        assert list(RunawayProcesses().run(ctx)) == []


class TestFailedUnits:
    def test_reports_failed_services(self, ctx):
        ctx.provide("systemctl")
        ctx.stub(
            ["systemctl", "--failed", "--no-legend", "--plain", "list-units"],
            "nginx.service loaded failed failed A high performance web server\n",
        )
        ctx.stub(
            ["systemctl", "show", "nginx.service", "--property=Result",
             "--property=ExecMainStatus"],
            "Result=exit-code\nExecMainStatus=1\n",
        )
        ctx.stub(
            ["systemctl", "--user", "--failed", "--no-legend", "--plain", "list-units"], ""
        )

        findings = list(FailedUnits().run(ctx))

        assert len(findings) == 1
        assert "nginx.service" in findings[0].detail
        assert "services.restart-failed" in findings[0].fix_ids

    def test_no_failures_is_silent(self, ctx):
        ctx.provide("systemctl")
        ctx.stub(["systemctl", "--failed", "--no-legend", "--plain", "list-units"], "")
        ctx.stub(["systemctl", "--user", "--failed", "--no-legend", "--plain", "list-units"], "")

        assert list(FailedUnits().run(ctx)) == []

    def test_requires_systemd(self, ctx):
        ctx.init_system = "openrc"
        assert FailedUnits().unavailable_reason(ctx)


class TestConnectivity:
    def test_offline_mode_skips_the_check(self, ctx):
        ctx.offline = True
        assert "offline" in Connectivity().unavailable_reason(ctx)

    def test_no_network_at_all_is_critical(self, ctx, monkeypatch):
        monkeypatch.setattr("medic.checks.network._tcp_probe", lambda h, p, t: None)
        monkeypatch.setattr("medic.checks.network._resolves", lambda name: False)

        findings = list(Connectivity().run(ctx))
        assert findings and findings[0].severity is Severity.CRITICAL
        assert "No internet" in findings[0].title

    def test_working_network_with_broken_dns_is_identified(self, ctx, monkeypatch):
        monkeypatch.setattr("medic.checks.network._tcp_probe", lambda h, p, t: 10.0)
        monkeypatch.setattr("medic.checks.network._resolves", lambda name: False)

        findings = list(Connectivity().run(ctx))
        assert findings and "DNS resolution is broken" in findings[0].title
        assert "net.flush-dns" in findings[0].fix_ids

    def test_healthy_network_is_silent(self, ctx, monkeypatch):
        monkeypatch.setattr("medic.checks.network._tcp_probe", lambda h, p, t: 12.0)
        monkeypatch.setattr("medic.checks.network._resolves", lambda name: True)

        assert list(Connectivity().run(ctx)) == []
