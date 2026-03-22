import os
import re
import subprocess
from typing import Final

from rich.console import Console
from rich.table import Table

# Fields from PowerShell "netsh wlan show interfaces" (Windows)
# Selected fields to monitor Wi-Fi performance
NT_SELECTED_FIELDS: Final[tuple[str, ...]] = (
    # "Name",  # interface name
    # "Description",
    # "GUID",
    # "Physical address",  # I think this is the ethernet MAC addr
    # "Interface type",
    # "State",
    "SSID",  # name of the access point
    "AP BSSID",  # MAC address of the access point
    "Band",  # wifi band, e.g. 2.4GHz, 5GHz
    "Channel",  # freq channel number, e.g. 1, 6, 11
    # "Connected Akm-cipher",
    # "Network type",
    # "Radio type",
    # "Authentication",
    # "Cipher",
    # "Connection mode",
    # "Receive rate (Mbps)",
    "Transmit rate (Mbps)",
    "Signal",  # calc of link/level/noise
    "Rssi",  # rx signal strength (dBm)
    # "Profile",
    # "QoS MSCS Configured",
    # "QoS Map Configured",
    # "QoS Map Allowed by Policy",
)

# Fields from "iwconfig wlan0" (Linux)
# Selected fields to monitor Wi-Fi performance
POSIX_NT_SELECTED_FIELDS: Final[tuple[str, ...]] = (
    "ESSID",  # name of the access point
    # "Mode",  # e.g. Master, Ad-Hoc, Managed
    "Frequency",  # channel frequency, e.g. 2437MHz for channel 6
    "Access Point",  # MAC address of the access point
    "Bit Rate",  # current transmit rate (Mbps)
    # "Tx-Power",  # transmit power level (dBm)
    # "Retry short limit",
    # "RTS thr",
    # "Fragment thr",
    # "Power Management",  # on/off
    "Link Quality",  # calc of link/level/noise
    "Signal level",  # rx signal strength (dBm)
    # "Rx invalid nwid",
    # "Rx invalid crypt",
    # "Rx invalid frag",
    # "Tx excessive retries",
    # "Invalid misc",
    # "Missed beacon",
)

# Fields from "iw dev wlan0 link" (Linux)
# Selected fields to monitor Wi-Fi performance
# POSIX_NT_SELECTED_FIELDS: Final[set[str]] = {
#     "Connected to",  # MAC address of the access point
#     "SSID",  # name of the access point
#     "freq",  # channel frequency, e.g. 2437MHz for channel 6
#     "RX",  # byte and packet count for received data
#     "TX",  # byte and packet count for transmitted data
#     "signal",  # rx signal strength (dBm)
#     "rx bitrate"  # current receive rate (Mbps)
#     "tx bitrate"  # current transmit rate (Mbps)
#     # "bss flags",  # e.g. short-preamble short-slot-time
#     # "dtim period",  # DTIM period in beacon intervals
#     # "beacon interval",  # beacon interval in ms
# }

if os.name == "nt":
    SELECTED_FIELDS: Final[tuple[str, ...]] = NT_SELECTED_FIELDS
else:
    SELECTED_FIELDS: Final[tuple[str, ...]] = POSIX_NT_SELECTED_FIELDS

# fmt: off
# Matching fields for 'nt' (Windows) and 'posix' (Linux) platforms
MAP_SELECTED_FIELDS: Final[dict[str, dict[str, str]]] = {
    "SSID":     {"nt": "SSID",                 "posix": "ESSID"},
    "MAC":      {"nt": "AP BSSID",             "posix": "Address"},
    "Band":     {"nt": "Band",                 "posix": "Frequency"},  # calculate band from freq in posix
    "Channel":  {"nt": "Channel",              "posix": "Frequency"},  # calculate chan from freq in posix
    "Bit Rate": {"nt": "Transmit rate (Mbps)", "posix": "Bit Rate"},
    "Quality":  {"nt": "Signal",               "posix": "Link Quality"},
    "RSSI":     {"nt": "Rssi",                 "posix": "Signal level"},
    "Rate":     {"nt": "Receive rate (Mbps)",  "posix": "Bit Rate"},
}
# fmt: on


def get_wlan_info_windows() -> dict[str, str]:
    # Run the PowerShell command
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["powershell", "-Command", "netsh wlan show interfaces"],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    output = result.stdout.splitlines()
    info: dict[str, str] = {}

    # Regex to match "Key : Value" lines
    pattern: re.Pattern[str] = re.compile(r"^\s*([^:]+)\s*:\s*(.*)$")

    for line in output:
        match = pattern.match(line)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()

            if key in SELECTED_FIELDS:
                info[key] = value

    return info


def get_wlan_info_posix() -> dict[str, str]:
    # Run the iwconfig command
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["iwconfig", "wlan0"],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    output = result.stdout.splitlines()
    info: dict[str, str] = {}

    # Regex to match "Key:Value" or "Key=Value" patterns
    # pattern: re.Pattern[str] = re.compile(r"(\b\w[\w\s]*\b)[=:](\S+)")
    pattern: re.Pattern[str] = re.compile(r'([A-Za-z][A-Za-z ]*[A-Za-z])\s*[:=]\s*("?[^"\s]+[^"]*"?)(?=\s|$)')

    for line in output:
        matches = pattern.findall(line)
        for key, value in matches:
            stripped_key = key.strip()
            stripped_value = value.strip().strip('"')  # Remove any surrounding quotes

            if stripped_key in SELECTED_FIELDS:
                info[stripped_key] = stripped_value

    return info


def get_wlan_info() -> dict[str, str]:
    if os.name == "nt":
        return get_wlan_info_windows()

    return get_wlan_info_posix()


def main() -> None:
    wlan_info: dict[str, str] = get_wlan_info()
    console: Console = Console()
    table: Table = Table(title="Wireless Interface", show_lines=True)

    table.add_column("Field", style="bold cyan")
    table.add_column("Value", style="bold yellow")

    for field in SELECTED_FIELDS:
        # Maintain predictable ordering for the selected netsh fields
        table.add_row(field, wlan_info.get(field, "N/A"))

    console.print(table)


if __name__ == "__main__":
    main()
