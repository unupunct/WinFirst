"""Network inventory, diagnostics and confirmed repairs."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from winfirst.services import network_service
from winfirst.ui.base_page import BasePage
from winfirst.ui.dialogs import run_operations
from winfirst.ui.widgets import Card, DataTable, Badge, hint, make_button


class NetworkPage(BasePage):
    page_key = "network"
    page_title = "Network Tools"
    page_subtitle = "Adapters, addresses, proxy state, DNS cache and explicit connectivity tests."

    def __init__(self, ctx: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent=parent)
        self.snapshot = None
        self._built = False

    def load(self) -> None:
        if not self._built:
            self._build()
            self._built = True
        self.refresh()

    def _build(self) -> None:
        self.add_action_button("Refresh", slot=self.refresh, variant="ghost")

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.adapter_badge = Badge("Adapters: —", "muted")
        self.gateway_badge = Badge("Gateway: —", "muted")
        self.proxy_badge = Badge("Proxy: —", "muted")
        for badge in (self.adapter_badge, self.gateway_badge, self.proxy_badge):
            stats.addWidget(badge)
        stats.addStretch(1)
        self.add_layout(stats)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.add_widget(self.tabs)

        adapters = QWidget()
        adapters_layout = QVBoxLayout(adapters)
        adapters_layout.setContentsMargins(0, 12, 0, 0)
        adapters_layout.setSpacing(9)
        self.adapters_table = DataTable(
            ["Adapter", "Kind", "Status", "IPv4", "Gateway", "DNS", "DHCP", "Speed"],
            height=250,
        )
        self.adapters_table.resize_columns([220, 110, 150, 170, 120, 200, 80, 100])
        self.routes_table = DataTable(["Destination", "Gateway", "Interface", "Metric"], height=170)
        self.proxy_label = QLabel("")
        self.proxy_label.setWordWrap(True)
        self.proxy_label.setObjectName("SmallMuted")
        adapters_layout.addWidget(self.adapters_table)
        adapters_layout.addWidget(self.proxy_label)
        adapters_layout.addWidget(self.routes_table)
        self.tabs.addTab(adapters, "Adapters & routes")

        dns = QWidget()
        dns_layout = QVBoxLayout(dns)
        dns_layout.setContentsMargins(0, 12, 0, 0)
        dns_layout.setSpacing(9)
        dns_toolbar = QHBoxLayout()
        dns_toolbar.addWidget(
            make_button("Load DNS cache", variant="ghost", slot=self.load_dns_cache)
        )
        dns_toolbar.addStretch(1)
        self.dns_note = QLabel("")
        self.dns_note.setObjectName("SmallMuted")
        dns_toolbar.addWidget(self.dns_note)
        dns_layout.addLayout(dns_toolbar)
        self.dns_table = DataTable(["Entry", "Type", "Data"], height=400)
        self.dns_table.resize_columns([300, 90, 420])
        dns_layout.addWidget(self.dns_table)
        self.tabs.addTab(dns, "DNS cache")

        diagnostics = QWidget()
        diag_layout = QVBoxLayout(diagnostics)
        diag_layout.setContentsMargins(0, 12, 0, 0)
        diag_layout.setSpacing(9)
        form = QHBoxLayout()
        form.setSpacing(8)
        self.host_input = QLineEdit("1.1.1.1")
        self.host_input.setPlaceholderText("Host name or IP address")
        form.addWidget(self.host_input, 2)
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(443)
        form.addWidget(QLabel("TCP port"))
        form.addWidget(self.port_input)
        for label, slot in (
            ("Ping", self.ping),
            ("Resolve DNS", self.resolve),
            ("Test TCP", self.tcp),
            ("Traceroute", self.traceroute),
            ("Full diagnostics", self.full_diagnostics),
        ):
            form.addWidget(make_button(label, variant="ghost", slot=slot))
        diag_layout.addLayout(form)
        self.diag_output = QPlainTextEdit()
        self.diag_output.setObjectName("LogView")
        self.diag_output.setReadOnly(True)
        self.diag_output.setPlaceholderText("Diagnostic output appears here.")
        diag_layout.addWidget(self.diag_output, 1)
        self.tabs.addTab(diagnostics, "Diagnostics")

        repair = QWidget()
        repair_layout = QVBoxLayout(repair)
        repair_layout.setContentsMargins(0, 12, 0, 0)
        repair_layout.setSpacing(9)
        repair_layout.addWidget(
            hint(
                "These repairs change Windows networking. Each is previewed and verified where Windows "
                "provides a reliable read-back. Winsock and TCP/IP resets require a restart."
            )
        )
        repair_row = QHBoxLayout()
        repair_row.setSpacing(8)
        repair_row.addWidget(make_button("Flush DNS…", variant="ghost", slot=self.flush_dns))
        repair_row.addWidget(make_button("Renew DHCP…", variant="ghost", slot=self.renew_dhcp))
        repair_row.addWidget(make_button("Reset Winsock…", variant="danger", slot=lambda: self.reset("winsock")))
        repair_row.addWidget(make_button("Reset TCP/IP…", variant="danger", slot=lambda: self.reset("tcpip")))
        repair_row.addStretch(1)
        repair_layout.addLayout(repair_row)
        repair_layout.addWidget(
            hint(
                "ICMP ping can be blocked by the target or by policy. A failed ping does not prove the "
                "network is down; use the DNS and TCP checks together."
            )
        )
        repair_layout.addStretch(1)
        self.tabs.addTab(repair, "Repair")

    # --------------------------------------------------------------------- data
    def refresh(self) -> None:
        self.clear_error()

        def work():
            return network_service.get_network_snapshot(self.ctx.runner)

        self.run_async(work, self._show_snapshot, busy="Reading network configuration…")

    def _show_snapshot(self, snapshot) -> None:
        self.snapshot = snapshot
        self.adapter_badge.setText(f"Adapters: {len(snapshot.adapters)}")
        gateways = sorted({gw for adapter in snapshot.adapters for gw in adapter.gateways if gw})
        self.gateway_badge.setText(f"Gateway: {gateways[0] if gateways else 'none detected'}")
        self.gateway_badge.set_status("ok" if gateways else "warning")
        proxy = snapshot.proxy
        if proxy.enabled:
            self.proxy_badge.setText("Proxy: enabled")
            self.proxy_badge.set_status("info")
        else:
            self.proxy_badge.setText("Proxy: not enabled")
            self.proxy_badge.set_status("muted")
        self.adapters_table.set_rows(
            [
                [
                    adapter.name,
                    adapter.kind,
                    adapter.status,
                    ", ".join(adapter.ipv4) or "—",
                    ", ".join(adapter.gateways) or "—",
                    ", ".join(adapter.dns) or "—",
                    adapter.dhcp or "—",
                    adapter.link_speed or "—",
                ]
                for adapter in snapshot.adapters
            ]
        )
        self.routes_table.set_rows(
            [
                [
                    str(route.get("destination") or route.get("Destination") or ""),
                    str(route.get("gateway") or route.get("NextHop") or ""),
                    str(route.get("interface") or route.get("InterfaceAlias") or ""),
                    str(route.get("metric") or route.get("RouteMetric") or ""),
                ]
                for route in snapshot.routes[:60]
            ]
        )
        proxy_bits = [
            f"WinHTTP: {proxy.winhttp or 'not configured'}",
            f"Auto-config: {proxy.auto_config_url or 'none'}",
            f"Override: {proxy.override or 'none'}",
        ]
        if proxy.server:
            proxy_bits.insert(0, f"Server: {proxy.server}")
        self.proxy_label.setText("Proxy settings · " + " · ".join(proxy_bits))
        if snapshot.errors:
            self.show_error("Some network details could not be read: " + "; ".join(snapshot.errors[:3]))
        self._gateway = gateways[0] if gateways else ""

    def load_dns_cache(self) -> None:
        self.dns_note.setText("Reading DNS cache…")
        self.run_async(
            lambda: network_service.get_dns_cache_entries(self.ctx.runner, 300),
            self._show_dns,
            busy="Reading DNS cache…",
        )

    def _show_dns(self, data) -> None:
        rows, error = data
        self.dns_note.setText(f"{len(rows)} cached entries" + (f" · {error}" if error else ""))
        self.dns_table.set_rows(
            [[row.get("Entry", ""), row.get("Type", ""), row.get("Data", "")] for row in rows]
        )

    # --------------------------------------------------------------- diagnostics
    def _append(self, text: str) -> None:
        self.diag_output.appendPlainText(text)

    def _run_check(self, title: str, fn) -> None:
        self._append(f"--- {title} ---")
        self.run_async(fn, self._show_check, busy=title + "…")

    def _show_check(self, check) -> None:
        if isinstance(check, tuple):
            check, extra = check
            if extra:
                self._append(f"Addresses: {', '.join(extra)}")
        self._append(
            f"{check.name}: {check.status} ({check.duration_ms} ms)\n"
            f"Target: {check.target}\n{check.detail}"
        )

    def ping(self) -> None:
        host = self.host_input.text().strip()
        if not host:
            return
        self._run_check(f"Ping {host}", lambda: network_service.ping_host(host))

    def resolve(self) -> None:
        name = self.host_input.text().strip()
        if not name:
            return
        self._run_check(f"Resolve {name}", lambda: network_service.resolve_dns(name))

    def tcp(self) -> None:
        host = self.host_input.text().strip()
        if not host:
            return
        port = self.port_input.value()
        self._run_check(f"TCP {host}:{port}", lambda: network_service.tcp_test(host, port))

    def traceroute(self) -> None:
        host = self.host_input.text().strip()
        if not host:
            return
        self._run_check(f"Traceroute {host}", lambda: network_service.traceroute(host))

    def full_diagnostics(self) -> None:
        self.diag_output.clear()
        host = self.host_input.text().strip() or "1.1.1.1"
        port = self.port_input.value()
        gateway = getattr(self, "_gateway", "")

        def work():
            return network_service.run_diagnostics(
                gateway=gateway,
                dns_name=host,
                tcp_host=host,
                tcp_port=port,
                include_ping=True,
                include_traceroute=False,
                runner=self.ctx.runner,
            )

        def show(checks) -> None:
            for check in checks:
                self._append(
                    f"{check.name}: {check.status} ({check.duration_ms} ms)\n"
                    f"Target: {check.target}\n{check.detail}\n"
                )

        self.run_async(work, show, busy="Running network diagnostics…")

    # -------------------------------------------------------------------- repair
    def flush_dns(self) -> None:
        self._repair(network_service.build_flush_dns_spec(), "Flush DNS cache")

    def renew_dhcp(self) -> None:
        self._repair(network_service.build_renew_dhcp_spec(), "Renew DHCP lease")

    def reset(self, kind: str) -> None:
        try:
            spec = network_service.build_reset_spec(kind)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self._repair(spec, spec.name)

    def _repair(self, spec, title: str) -> None:
        results = run_operations(self.ctx, self, [spec], title=title)
        if results:
            self.refresh()
