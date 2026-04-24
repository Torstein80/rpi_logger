from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config import (
    APP_PORT,
    NETWORK_AP_CONNECTION_NAME,
    NETWORK_AP_INTERFACE,
    NETWORK_MANAGER_ENABLED,
    NETWORK_WIRED_CONNECTION_NAME,
    NETWORK_WIRED_INTERFACE,
)

DBUS_SYSTEM_BUS_SOCKET = Path("/run/dbus/system_bus_socket")


class NetworkConfigError(RuntimeError):
    pass


class NetworkUnavailableError(RuntimeError):
    pass


def _run_nmcli(args: list[str], check: bool = True) -> str:
    if not NETWORK_MANAGER_ENABLED:
        raise NetworkUnavailableError("NetworkManager integration is disabled in the container environment.")
    if shutil.which("nmcli") is None:
        raise NetworkUnavailableError("nmcli is not installed in the container image.")
    if not DBUS_SYSTEM_BUS_SOCKET.exists():
        raise NetworkUnavailableError(
            "The host D-Bus socket is not mounted into the container. Mount /run/dbus/system_bus_socket and redeploy."
        )

    env = os.environ.copy()
    env.setdefault("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={DBUS_SYSTEM_BUS_SOCKET}")
    process = subprocess.run(
        ["nmcli", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout or "nmcli command failed").strip()
        raise NetworkConfigError(detail)
    return process.stdout.strip()


def _parse_kv_lines(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for raw_line in (text or "").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key = re.sub(r"\[\d+\]$", "", key.strip())
        value = value.strip()
        existing = data.get(key)
        if existing is None:
            data[key] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            data[key] = [existing, value]
    return data


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    if value == "":
        return []
    return [value]


def _connection_exists(name: str) -> bool:
    names = _run_nmcli(["-t", "-f", "NAME", "connection", "show"], check=False)
    return any(line.strip() == name for line in names.splitlines())


def _device_show(device: str) -> dict[str, Any]:
    output = _run_nmcli(
        ["-t", "-f", "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", device],
        check=False,
    )
    data = _parse_kv_lines(output)
    addresses = _as_list(data.get("IP4.ADDRESS"))
    dns_servers = _as_list(data.get("IP4.DNS"))
    return {
        "device": data.get("GENERAL.DEVICE") or device,
        "type": data.get("GENERAL.TYPE"),
        "state": data.get("GENERAL.STATE"),
        "connection": data.get("GENERAL.CONNECTION"),
        "addresses": addresses,
        "gateway": data.get("IP4.GATEWAY") or None,
        "dns": dns_servers,
    }


def _connection_show(name: str) -> dict[str, Any]:
    output = _run_nmcli(
        [
            "-t",
            "-f",
            "connection.id,connection.interface-name,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,802-11-wireless.ssid",
            "connection",
            "show",
            name,
        ],
        check=False,
    )
    data = _parse_kv_lines(output)
    return {
        "name": data.get("connection.id") or name,
        "interface": data.get("connection.interface-name"),
        "mode": data.get("ipv4.method") or None,
        "addresses": _as_list(data.get("ipv4.addresses")),
        "gateway": data.get("ipv4.gateway") or None,
        "dns": _as_list(data.get("ipv4.dns")),
        "ssid": data.get("802-11-wireless.ssid") or None,
    }


def _preferred_connection_for_device(device: str, preferred_name: str) -> str:
    if _connection_exists(preferred_name):
        return preferred_name
    lines = _run_nmcli(["-t", "-f", "NAME,DEVICE", "connection", "show"], check=False)
    for line in lines.splitlines():
        if not line:
            continue
        name, _, current_device = line.partition(":")
        if current_device.strip() == device:
            return name.strip()
    return preferred_name


def _first_url_from_addresses(addresses: list[str], scheme: str = "http") -> str | None:
    if not addresses:
        return None
    ip = addresses[0].split("/", 1)[0]
    return f"{scheme}://{ip}:{APP_PORT}"


def network_manager_status() -> dict[str, Any]:
    hostname = socket.gethostname()
    hostname_url = f"http://{hostname}.local:{APP_PORT}"
    if not NETWORK_MANAGER_ENABLED:
        return {
            "available": False,
            "reason": "Network settings are disabled in the container configuration.",
            "hostname": hostname,
            "hostname_url": hostname_url,
        }
    if shutil.which("nmcli") is None:
        return {
            "available": False,
            "reason": "nmcli is not installed in the container image.",
            "hostname": hostname,
            "hostname_url": hostname_url,
        }
    if not DBUS_SYSTEM_BUS_SOCKET.exists():
        return {
            "available": False,
            "reason": "The host D-Bus socket is not mounted into the container. Add /run/dbus/system_bus_socket and redeploy.",
            "hostname": hostname,
            "hostname_url": hostname_url,
        }

    try:
        ap_connection_name = _preferred_connection_for_device(NETWORK_AP_INTERFACE, NETWORK_AP_CONNECTION_NAME)
        wired_connection_name = _preferred_connection_for_device(NETWORK_WIRED_INTERFACE, NETWORK_WIRED_CONNECTION_NAME)
        ap_device = _device_show(NETWORK_AP_INTERFACE)
        wired_device = _device_show(NETWORK_WIRED_INTERFACE)
        ap_profile = _connection_show(ap_connection_name) if _connection_exists(ap_connection_name) else {
            "name": ap_connection_name,
            "interface": NETWORK_AP_INTERFACE,
            "mode": None,
            "addresses": [],
            "gateway": None,
            "dns": [],
            "ssid": None,
        }
        wired_profile = _connection_show(wired_connection_name) if _connection_exists(wired_connection_name) else {
            "name": wired_connection_name,
            "interface": NETWORK_WIRED_INTERFACE,
            "mode": None,
            "addresses": [],
            "gateway": None,
            "dns": [],
            "ssid": None,
        }
    except (NetworkConfigError, NetworkUnavailableError) as exc:
        return {
            "available": False,
            "reason": str(exc),
            "hostname": hostname,
            "hostname_url": hostname_url,
        }

    ap_addresses = ap_device["addresses"] or ap_profile["addresses"]
    wired_addresses = wired_device["addresses"] or wired_profile["addresses"]
    urls = [hostname_url]
    for address in [*ap_addresses, *wired_addresses]:
        ip_only = address.split("/", 1)[0]
        candidate = f"http://{ip_only}:{APP_PORT}"
        if candidate not in urls:
            urls.append(candidate)

    return {
        "available": True,
        "reason": None,
        "hostname": hostname,
        "hostname_url": hostname_url,
        "urls": urls,
        "ap": {
            "interface": NETWORK_AP_INTERFACE,
            "connection_name": ap_profile["name"],
            "ssid": ap_profile["ssid"],
            "state": ap_device["state"],
            "active_connection": ap_device["connection"],
            "addresses": ap_addresses,
            "gateway": ap_device["gateway"],
            "dns": ap_device["dns"],
            "url": _first_url_from_addresses(ap_addresses),
        },
        "wired": {
            "interface": NETWORK_WIRED_INTERFACE,
            "connection_name": wired_profile["name"],
            "state": wired_device["state"],
            "active_connection": wired_device["connection"],
            "mode": wired_profile["mode"],
            "configured_addresses": wired_profile["addresses"],
            "active_addresses": wired_addresses,
            "gateway": wired_profile["gateway"] or wired_device["gateway"],
            "dns": wired_profile["dns"] or wired_device["dns"],
            "url": _first_url_from_addresses(wired_addresses),
        },
    }


def _validate_ip_cidr(value: str) -> str:
    try:
        interface = ipaddress.ip_interface(value)
    except ValueError as exc:
        raise NetworkConfigError("Enter the wired IPv4 address in CIDR format, for example 10.87.0.5/24.") from exc
    if interface.version != 4:
        raise NetworkConfigError("Only IPv4 addresses are supported for the wired profile in this UI.")
    return str(interface)


def _validate_optional_ip(value: str | None, label: str) -> str | None:
    clean = (value or "").strip()
    if not clean:
        return None
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError as exc:
        raise NetworkConfigError(f"{label} must be a valid IPv4 address.") from exc
    if ip.version != 4:
        raise NetworkConfigError(f"{label} must be a valid IPv4 address.")
    return str(ip)


def _validate_dns(value: str | None) -> list[str]:
    raw = (value or "").replace(",", " ").split()
    dns_servers: list[str] = []
    for item in raw:
        try:
            ip = ipaddress.ip_address(item)
        except ValueError as exc:
            raise NetworkConfigError("DNS entries must be valid IPv4 addresses separated by spaces or commas.") from exc
        if ip.version != 4:
            raise NetworkConfigError("DNS entries must be valid IPv4 addresses.")
        dns_servers.append(str(ip))
    return dns_servers


def apply_wired_network(mode: str, ip_cidr: str | None = None, gateway: str | None = None, dns: str | None = None) -> dict[str, Any]:
    if mode not in {"auto", "manual"}:
        raise NetworkConfigError("mode must be either 'auto' or 'manual'.")
    if not NETWORK_MANAGER_ENABLED:
        raise NetworkUnavailableError("Network settings are disabled in the container configuration.")

    connection_name = NETWORK_WIRED_CONNECTION_NAME
    interface = NETWORK_WIRED_INTERFACE
    if not _connection_exists(connection_name):
        _run_nmcli(["connection", "add", "type", "ethernet", "ifname", interface, "con-name", connection_name, "autoconnect", "yes"])

    if mode == "manual":
        if not ip_cidr:
            raise NetworkConfigError("A fixed IPv4 address is required when wired mode is set to Fixed IP.")
        validated_ip = _validate_ip_cidr(ip_cidr)
        validated_gateway = _validate_optional_ip(gateway, "Gateway")
        validated_dns = _validate_dns(dns)
        _run_nmcli(["connection", "modify", connection_name, "ipv4.method", "manual", "ipv4.addresses", validated_ip])
        if validated_gateway:
            _run_nmcli(["connection", "modify", connection_name, "ipv4.gateway", validated_gateway])
        else:
            _run_nmcli(["connection", "modify", connection_name, "-ipv4.gateway"], check=False)
        if validated_dns:
            _run_nmcli(["connection", "modify", connection_name, "ipv4.dns", " ".join(validated_dns)])
        else:
            _run_nmcli(["connection", "modify", connection_name, "-ipv4.dns"], check=False)
    else:
        _run_nmcli(["connection", "modify", connection_name, "ipv4.method", "auto"])
        _run_nmcli(["connection", "modify", connection_name, "-ipv4.addresses"], check=False)
        _run_nmcli(["connection", "modify", connection_name, "-ipv4.gateway"], check=False)
        _run_nmcli(["connection", "modify", connection_name, "-ipv4.dns"], check=False)

    _run_nmcli(
        [
            "connection",
            "modify",
            connection_name,
            "ipv6.method",
            "ignore",
            "connection.autoconnect",
            "yes",
            "connection.autoconnect-priority",
            "100",
        ]
    )
    _run_nmcli(["connection", "up", connection_name, "ifname", interface], check=False)
    return network_manager_status()
