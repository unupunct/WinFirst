"""Network inventory, diagnostics and confirmed repair actions."""
from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import psutil

from winfirst.core.command_runner import CommandRunner, as_list, ps_quote
from winfirst.core.operation import ApplyOutcome, OperationSpec, VerifyOutcome
from winfirst.core.risk_engine import assess_action
from winfirst.services.common import ServiceError, ps_json, require_windows, run_ok
from winfirst.services.win_registry import read_value

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,253}$")


@dataclass
class NetworkAdapter:
    name: str
    description: str = ""
    status: str = ""
    link_speed: str = ""
    mac: str = ""
    index: int = 0
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    dns: list[str] = field(default_factory=list)
    dhcp: str = ""
    virtual: bool = False
    kind: str = "Ethernet"


@dataclass
class ProxySettings:
    enabled: bool = False
    server: str = ""
    override: str = ""
    auto_config_url: str = ""
    winhttp: str = ""

    @property
    def summary(self) -> str:
        if self.auto_config_url:
            return f"Auto-config: {self.auto_config_url}"
        if self.enabled:
            return f"Manual proxy: {self.server}"
        return "No user proxy configured"


@dataclass
class NetworkSnapshot:
    adapters: list[NetworkAdapter] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    vpns: list[dict[str, Any]] = field(default_factory=list)
    proxy: ProxySettings = field(default_factory=ProxySettings)
    errors: list[str] = field(default_factory=list)

    @property
    def active_adapters(self) -> list[NetworkAdapter]:
        return [adapter for adapter in self.adapters if adapter.status.lower() == "up"]

    @property
    def default_gateway(self) -> str:
        for adapter in self.active_adapters:
            for gateway in adapter.gateways:
                if gateway and gateway != "0.0.0.0":
                    return gateway
        for route in self.routes:
            next_hop = str(route.get("NextHop") or "")
            if next_hop and next_hop != "0.0.0.0":
                return next_hop
        return ""

    @property
    def dns_servers(self) -> list[str]:
        servers: list[str] = []
        for adapter in self.active_adapters:
            for server in adapter.dns:
                if server and server not in servers:
                    servers.append(server)
        return servers


_SCRIPT = r"""
$result = [ordered]@{ Adapters = @(); Configs = @(); Routes = @(); Vpns = @(); Error = '' }
try {
    $result.Adapters = @(Get-NetAdapter -ErrorAction Stop | ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name; Description = [string]$_.InterfaceDescription;
            Status = [string]$_.Status; LinkSpeed = [string]$_.LinkSpeed;
            Mac = [string]$_.MacAddress; Index = [int]$_.ifIndex;
            Virtual = [bool]$_.Virtual; Media = [string]$_.MediaType
        }
    })
    $result.Configs = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{
            Index = [int]$_.InterfaceIndex
            IPv4 = @($_.IPv4Address | ForEach-Object { [string]$_.IPAddress })
            IPv6 = @($_.IPv6Address | ForEach-Object { [string]$_.IPAddress })
            Gateway = @($_.IPv4DefaultGateway | ForEach-Object { [string]$_.NextHop })
            DNS = @($_.DNSServer | ForEach-Object { @($_.ServerAddresses) })
            DHCP = [string]$_.NetIPv4Interface.Dhcp
        }
    })
    $result.Routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{ Interface = [string]$_.InterfaceAlias; NextHop = [string]$_.NextHop; Metric = $_.RouteMetric }
    })
    $result.Vpns = @(Get-VpnConnection -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{ Name = [string]$_.Name; Server = [string]$_.ServerAddress; Status = [string]$_.ConnectionStatus }
    })
} catch { $result.Error = $_.Exception.Message }
$result | ConvertTo-Json -Depth 6 -Compress
"""


def _fallback_adapters() -> list[NetworkAdapter]:
    adapters: list[NetworkAdapter] = []
    try:
        stats = psutil.net_if_stats()
        addresses = psutil.net_if_addrs()
        for name, addrs in addresses.items():
            stat = stats.get(name)
            adapter = NetworkAdapter(
                name=name,
                status="Up" if stat and stat.isup else "Down",
                link_speed=f"{stat.speed} Mbps" if stat and stat.speed else "",
                mac="",
            )
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    adapter.ipv4.append(addr.address)
                elif addr.family == socket.AF_INET6:
                    adapter.ipv6.append(addr.address)
                elif getattr(addr.family, "name", "") == "AF_LINK":
                    adapter.mac = addr.address
            if adapter.ipv4 or adapter.ipv6:
                adapters.append(adapter)
    except Exception:
        pass
    return adapters


def get_proxy_settings(runner: CommandRunner | None = None) -> ProxySettings:
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    enable = read_value("HKCU", path, "ProxyEnable")
    server = read_value("HKCU", path, "ProxyServer")
    override = read_value("HKCU", path, "ProxyOverride")
    autoconfig = read_value("HKCU", path, "AutoConfigURL")
    winhttp = ""
    if runner is not None:
        try:
            result = runner.run(["netsh.exe", "winhttp", "show", "proxy"], timeout=20)
            if result.ok:
                winhttp = result.stdout.strip()[:600]
        except Exception:
            pass
    return ProxySettings(
        enabled=bool(int(enable.value or 0)) if enable.exists else False,
        server=str(server.value or "") if server.exists else "",
        override=str(override.value or "") if override.exists else "",
        auto_config_url=str(autoconfig.value or "") if autoconfig.exists else "",
        winhttp=winhttp,
    )


def get_network_snapshot(runner: CommandRunner | None = None) -> NetworkSnapshot:
    require_windows("Network inspection")
    runner = runner or CommandRunner()
    snapshot = NetworkSnapshot()
    try:
        data = ps_json(runner, _SCRIPT, timeout=90, default={}) or {}
    except Exception as exc:
        snapshot.errors.append(f"Network cmdlets failed: {exc}")
        data = {}
    if data.get("Error"):
        snapshot.errors.append(str(data.get("Error")))
    configs = {int(row.get("Index") or 0): row for row in as_list(data.get("Configs"))}
    for row in as_list(data.get("Adapters")):
        index = int(row.get("Index") or 0)
        config = configs.get(index, {})
        adapter = NetworkAdapter(
            name=str(row.get("Name") or ""),
            description=str(row.get("Description") or ""),
            status=str(row.get("Status") or ""),
            link_speed=str(row.get("LinkSpeed") or ""),
            mac=str(row.get("Mac") or ""),
            index=index,
            virtual=bool(row.get("Virtual")),
            ipv4=[str(value) for value in (config.get("IPv4") or []) if value],
            ipv6=[str(value) for value in (config.get("IPv6") or []) if value and not str(value).startswith("fe80")],
            gateways=[str(value) for value in (config.get("Gateway") or []) if value],
            dns=[str(value) for value in _flatten(config.get("DNS")) if value],
            dhcp=str(config.get("DHCP") or ""),
        )
        media = str(row.get("Media") or "").lower()
        name_lower = adapter.name.lower()
        if "wireless" in media or "802.11" in media or "wi-fi" in name_lower or "wifi" in name_lower:
            adapter.kind = "Wi-Fi"
        elif adapter.virtual or "vpn" in name_lower or "tap" in name_lower or "wireguard" in name_lower:
            adapter.kind = "Virtual / VPN"
        snapshot.adapters.append(adapter)
    if not snapshot.adapters:
        snapshot.adapters = _fallback_adapters()
        if snapshot.adapters:
            snapshot.errors.append(
                "Detailed adapter configuration was unavailable; basic addresses are shown from psutil."
            )
    for row in as_list(data.get("Routes")):
        snapshot.routes.append(
            {
                "Interface": row.get("Interface", ""),
                "NextHop": row.get("NextHop", ""),
                "Metric": row.get("Metric", ""),
            }
        )
    for row in as_list(data.get("Vpns")):
        snapshot.vpns.append(
            {
                "Name": row.get("Name", ""),
                "Server": row.get("Server", ""),
                "Status": row.get("Status", ""),
            }
        )
    snapshot.proxy = get_proxy_settings(runner)
    return snapshot


def _flatten(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            if isinstance(item, list):
                out.extend(_flatten(item))
            else:
                out.append(item)
        return out
    return [value]


@dataclass
class NetworkCheck:
    name: str
    target: str
    status: str  # pass | fail | warning | skipped
    detail: str = ""
    duration_ms: int = 0


def _validate_host(host: str) -> str:
    host = (host or "").strip()
    if not host or not _HOST_RE.match(host):
        raise ServiceError(f"Invalid network test target: {host!r}")
    return host


def ping_host(host: str, *, count: int = 4, runner: CommandRunner | None = None, timeout: float = 30) -> NetworkCheck:
    host = _validate_host(host)
    runner = runner or CommandRunner()
    started = time.monotonic()
    result = runner.run(
        ["ping.exe", "-n", str(max(1, min(count, 10))), "-w", "1500", host],
        timeout=timeout,
    )
    duration = int((time.monotonic() - started) * 1000)
    if not result.launched:
        return NetworkCheck("Ping", host, "skipped", result.error, duration)
    if result.timed_out:
        return NetworkCheck("Ping", host, "fail", "Ping timed out.", duration)
    detail = _last_line(result.stdout) or _last_line(result.stderr)
    return NetworkCheck(
        "Ping",
        host,
        "pass" if result.exit_code == 0 else "fail",
        detail or f"ping exited with code {result.exit_code}",
        duration,
    )


def resolve_dns(name: str, *, runner: CommandRunner | None = None, timeout: float = 30) -> tuple[NetworkCheck, list[str]]:
    name = _validate_host(name)
    runner = runner or CommandRunner()
    started = time.monotonic()
    script = (
        "try { $addresses = [System.Net.Dns]::GetHostAddresses("
        + ps_quote(name)
        + ") | ForEach-Object { $_.IPAddressToString }; $addresses | ConvertTo-Json -Compress } "
        "catch { '' }"
    )
    try:
        data = runner.powershell_json(script, timeout=timeout, default=[])
    except Exception as exc:
        return NetworkCheck("DNS", name, "fail", str(exc), int((time.monotonic() - started) * 1000)), []
    addresses = [str(item) for item in (data if isinstance(data, list) else ([data] if data else []))]
    duration = int((time.monotonic() - started) * 1000)
    if addresses:
        return NetworkCheck("DNS resolution", name, "pass", ", ".join(addresses[:6]), duration), addresses
    return NetworkCheck("DNS resolution", name, "fail", "The name did not resolve.", duration), []


def tcp_test(host: str, port: int, *, runner: CommandRunner | None = None, timeout: float = 20) -> NetworkCheck:
    host = _validate_host(host)
    port = int(port)
    if port < 1 or port > 65535:
        raise ServiceError("Invalid TCP port.")
    runner = runner or CommandRunner()
    started = time.monotonic()
    script = (
        f"$client = New-Object System.Net.Sockets.TcpClient\n"
        f"try {{\n"
        f"  $task = $client.ConnectAsync({ps_quote(host)}, {port})\n"
        f"  if ($task.Wait(5000) -and $client.Connected) {{ 'open' }} else {{ 'closed' }}\n"
        f"}} catch {{ 'closed' }} finally {{ $client.Close() }}\n"
    )
    try:
        result = runner.powershell(script, timeout=timeout)
    except Exception as exc:
        return NetworkCheck("TCP connectivity", f"{host}:{port}", "fail", str(exc), int((time.monotonic() - started) * 1000))
    duration = int((time.monotonic() - started) * 1000)
    state = result.stdout.strip().splitlines()[-1].lower() if result.stdout.strip() else "unknown"
    return NetworkCheck(
        "TCP connectivity",
        f"{host}:{port}",
        "pass" if state == "open" else "fail",
        "TCP connection succeeded." if state == "open" else "TCP connection was refused or timed out.",
        duration,
    )


def traceroute(host: str, *, runner: CommandRunner | None = None, timeout: float = 120) -> NetworkCheck:
    host = _validate_host(host)
    runner = runner or CommandRunner()
    started = time.monotonic()
    result = runner.run(
        ["tracert.exe", "-d", "-h", "15", "-w", "1000", host], timeout=timeout
    )
    duration = int((time.monotonic() - started) * 1000)
    if result.timed_out:
        return NetworkCheck("Traceroute", host, "warning", "Traceroute timed out.", duration)
    text = result.combined.strip()
    return NetworkCheck(
        "Traceroute",
        host,
        "pass" if result.exit_code == 0 else "warning",
        text[-3000:] if text else "No traceroute output.",
        duration,
    )


def run_diagnostics(
    *,
    gateway: str = "",
    dns_name: str = "example.com",
    tcp_host: str = "1.1.1.1",
    tcp_port: int = 443,
    include_ping: bool = True,
    include_traceroute: bool = False,
    runner: CommandRunner | None = None,
    progress: Callable[[str], None] | None = None,
    cancel_event=None,
) -> list[NetworkCheck]:
    runner = runner or CommandRunner()
    report = progress or (lambda _text: None)
    checks: list[NetworkCheck] = []
    if gateway:
        report(f"Testing gateway {gateway}")
        checks.append(ping_host(gateway, runner=runner))
    else:
        checks.append(NetworkCheck("Gateway", "", "skipped", "No default gateway was detected."))
    report(f"Resolving {dns_name}")
    dns_check, _addresses = resolve_dns(dns_name, runner=runner)
    checks.append(dns_check)
    report(f"Testing TCP {tcp_host}:{tcp_port}")
    checks.append(tcp_test(tcp_host, tcp_port, runner=runner))
    if include_ping:
        report("Testing internet ICMP connectivity")
        checks.append(ping_host(tcp_host, count=3, runner=runner))
    if include_traceroute:
        report(f"Tracing route to {tcp_host}")
        checks.append(traceroute(tcp_host, runner=runner))
    if cancel_event is not None and cancel_event.is_set():
        checks.append(NetworkCheck("Diagnostics", "", "skipped", "Cancelled by the user."))
    return checks


def get_dns_cache_entries(runner: CommandRunner | None = None, limit: int = 300) -> tuple[list[dict[str, str]], str]:
    runner = runner or CommandRunner()
    script = r"""
try {
    $items = @(Get-DnsClientCache -ErrorAction Stop | Select-Object -First __LIMIT__ |
        ForEach-Object { [pscustomobject]@{ Entry = [string]$_.Entry; Type = [string]$_.Type; Data = [string]$_.Data } })
    $items | ConvertTo-Json -Depth 3 -Compress
} catch { '' }
""".replace("__LIMIT__", str(max(1, limit)))
    try:
        data = ps_json(runner, script, timeout=45, default=[])
    except Exception as exc:
        return [], str(exc)
    rows = [
        {
            "Entry": str(row.get("Entry") or ""),
            "Type": str(row.get("Type") or ""),
            "Data": str(row.get("Data") or ""),
        }
        for row in as_list(data)
    ]
    return rows, ""


# --------------------------------------------------------------------- repairs
def build_flush_dns_spec() -> OperationSpec:
    action = assess_action("network.flush_dns")

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        before, _ = get_dns_cache_entries(runner)
        progress("Flushing the DNS resolver cache")
        result = run_ok(
            runner, ["ipconfig.exe", "/flushdns"], timeout=30,
            error_prefix="Could not flush the DNS cache", cancel_event=cancel,
        )
        return ApplyOutcome(
            success=True,
            previous_state=f"{len(before)} cached entries",
            requested_state="Empty resolver cache",
            actual_state="Flush command completed",
            stdout=result.stdout,
            stderr=result.stderr,
            details={"cache_entries_before": len(before)},
        )

    def verify() -> VerifyOutcome:
        runner = CommandRunner()
        entries, error = get_dns_cache_entries(runner)
        if error:
            return VerifyOutcome(
                ok=True,
                actual_state="Flush command completed",
                message=f"Cache contents could not be read back ({error}).",
            )
        return VerifyOutcome(
            ok=True,
            actual_state=f"{len(entries)} cached entries after flush",
            message="ipconfig /flushdns completed. The DNS cache repopulates as names are resolved.",
        )

    return OperationSpec(
        operation_id="network.flush_dns",
        name="Flush DNS resolver cache",
        category="network",
        description="Clears the local DNS resolver cache with ipconfig /flushdns.",
        risk=action.level,
        requires_admin=False,
        reversible=action.reversible,
        apply=apply,
        verify=verify,
        before=lambda: f"{len(get_dns_cache_entries()[0])} cached entries",
        preview_text="ipconfig /flushdns",
        warnings=["Name resolution may be briefly slower while the cache repopulates."],
        verification_method="Runs ipconfig /flushdns and reads the cache size afterwards",
    )


def build_renew_dhcp_spec() -> OperationSpec:
    action = assess_action("network.renew_dhcp")

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress("Releasing the DHCP lease (the connection may drop)")
        release = runner.run(["ipconfig.exe", "/release"], timeout=60, cancel_event=cancel)
        progress("Requesting a new DHCP lease")
        renew = runner.run(["ipconfig.exe", "/renew"], timeout=120, cancel_event=cancel)
        success = renew.launched and renew.exit_code == 0
        return ApplyOutcome(
            success=success,
            previous_state="Existing DHCP lease",
            requested_state="New DHCP lease",
            actual_state="Renew command completed" if success else "Renew failed",
            stdout=(release.stdout or "") + "\n" + (renew.stdout or ""),
            stderr=(release.stderr or "") + "\n" + (renew.stderr or ""),
            exit_code=renew.exit_code,
            warnings=["The network connection may drop during lease renewal."],
            error="" if success else (renew.tail(600) or "DHCP renewal failed."),
        )

    def verify() -> VerifyOutcome:
        snapshot = get_network_snapshot()
        active = [adapter for adapter in snapshot.active_adapters if adapter.ipv4]
        return VerifyOutcome(
            ok=bool(active),
            actual_state=f"{len(active)} active adapter(s) with IPv4",
            message="Read back the active adapter list after renewal.",
        )

    return OperationSpec(
        operation_id="network.renew_dhcp",
        name="Renew DHCP lease",
        category="network",
        description="Releases and renews the DHCP lease on all adapters.",
        risk=action.level,
        requires_admin=True,
        reversible=action.reversible,
        can_interrupt_connectivity=action.can_interrupt_connectivity,
        apply=apply,
        verify=verify,
        before=lambda: f"{len(get_network_snapshot().active_adapters)} active adapters",
        preview_text="ipconfig /release followed by ipconfig /renew",
        warnings=["The current network session, including remote support, may be interrupted."],
        verification_method="Reads active adapters and IPv4 addresses after renewal",
    )


def build_reset_spec(kind: str) -> OperationSpec:
    if kind not in {"winsock", "tcpip"}:
        raise ValueError("Unsupported network reset type")
    if kind == "winsock":
        argv = ["netsh.exe", "winsock", "reset"]
        action = assess_action("network.reset_winsock")
        name = "Reset Winsock catalog"
        description = "Resets the Winsock catalog with netsh winsock reset."
    else:
        argv = ["netsh.exe", "int", "ip", "reset"]
        action = assess_action("network.reset_tcpip")
        name = "Reset TCP/IP stack"
        description = "Resets the TCP/IP stack with netsh int ip reset."

    def apply(progress, cancel) -> ApplyOutcome:
        runner = CommandRunner()
        progress(f"Running {argv[0]} {' '.join(argv[1:])}")
        result = runner.run(argv, timeout=120, cancel_event=cancel)
        success = result.launched and result.exit_code == 0
        return ApplyOutcome(
            success=success,
            previous_state="Existing network stack configuration",
            requested_state=name,
            actual_state="Reset command completed; restart required" if success else "Reset failed",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            restart_required=success,
            warnings=[
                "A restart is required before the change is fully effective.",
                "Static IP, DNS or proxy settings may need to be reconfigured.",
            ],
            error="" if success else result.tail(800),
        )

    return OperationSpec(
        operation_id=f"network.reset_{kind}",
        name=name,
        category="network",
        description=description,
        risk=action.level,
        requires_admin=True,
        reversible=action.reversible,
        restart_required=True,
        can_interrupt_connectivity=action.can_interrupt_connectivity,
        apply=apply,
        verify=None,
        before=lambda: "Current network stack",
        preview_text=" ".join(argv),
        warnings=[
            "This can disconnect the computer and interrupt remote support.",
            "Static network configuration may be lost.",
            "A restart is required.",
        ],
        verification_method="Exit status only; a restart is required before network state can be re-tested",
    )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""
