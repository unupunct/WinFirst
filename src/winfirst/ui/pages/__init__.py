"""Page registry used by the main window for lazy construction."""
from __future__ import annotations

import importlib
from typing import Any

# key -> (module within winfirst.ui.pages, class name)
PAGE_CLASSES: dict[str, tuple[str, str]] = {
    "dashboard": ("dashboard", "DashboardPage"),
    "quick_setup": ("quick_setup", "QuickSetupPage"),
    "windows_settings": ("windows_settings", "WindowsSettingsPage"),
    "power": ("power", "PowerPage"),
    "cleanup": ("cleanup", "CleanupPage"),
    "software": ("software", "SoftwarePage"),
    "health": ("system_health", "SystemHealthPage"),
    "drivers": ("drivers", "DriverPage"),
    "network": ("network", "NetworkPage"),
    "repair": ("repair", "RepairPage"),
    "privacy": ("privacy", "PrivacyPage"),
    "explorer": ("explorer", "ExplorerPage"),
    "startup": ("startup", "StartupPage"),
    "services": ("services", "ServicesPage"),
    "storage": ("storage", "StoragePage"),
    "printers": ("printers", "PrinterPage"),
    "users": ("users", "UsersPage"),
    "profiles": ("profiles", "ProfilesPage"),
    "drift": ("drift", "DriftPage"),
    "deployment": ("deployment", "DeploymentPage"),
    "portable": ("portable", "PortablePage"),
    "history": ("history", "HistoryPage"),
    "reports": ("reports", "ReportsPage"),
    "app_settings": ("app_settings", "AppSettingsPage"),
}

PAGE_TITLES: dict[str, str] = {
    "dashboard": "Dashboard",
    "quick_setup": "Quick Setup",
    "windows_settings": "Windows Settings",
    "power": "Power Management",
    "cleanup": "Applications & Bloatware",
    "software": "Software Installation",
    "health": "System Health",
    "drivers": "Driver Management",
    "network": "Network Tools",
    "repair": "Repair Tools",
    "privacy": "Privacy & Security",
    "explorer": "Explorer & Desktop",
    "startup": "Startup Manager",
    "services": "Services & Tasks",
    "storage": "Storage Analysis",
    "printers": "Printer Management",
    "users": "Users, Environment & Features",
    "profiles": "Profiles",
    "drift": "Configuration Drift",
    "deployment": "Deployment Checklist",
    "portable": "Portable Toolkit",
    "history": "History & Rollback",
    "reports": "Reports",
    "app_settings": "Application Settings",
}

PAGE_GROUPS: list[tuple[str, list[str]]] = [
    ("Overview", ["dashboard", "quick_setup"]),
    (
        "Personalise Windows",
        ["windows_settings", "power", "explorer", "privacy"],
    ),
    (
        "Maintain",
        ["cleanup", "software", "health", "drivers", "network", "repair"],
    ),
    (
        "Manage",
        ["startup", "services", "storage", "printers", "users"],
    ),
    (
        "Configuration",
        ["profiles", "drift", "deployment"],
    ),
    (
        "Records & Tools",
        ["portable", "history", "reports", "app_settings"],
    ),
]


def create_page(key: str, ctx: Any) -> Any:
    try:
        module_name, class_name = PAGE_CLASSES[key]
    except KeyError as exc:
        raise KeyError(f"Unknown page: {key}") from exc
    module = importlib.import_module(f"winfirst.ui.pages.{module_name}")
    page_class = getattr(module, class_name)
    return page_class(ctx)
