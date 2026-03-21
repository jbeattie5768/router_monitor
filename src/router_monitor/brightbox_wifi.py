import re
import subprocess

from rich.console import Console
from rich.table import Table

# Fields from "netsh wlan show interfaces"
# Selected fields to monitor Wi-Fi performance
SELECTED_FIELDS = {
    # "Name",
    # "Description",
    # "GUID",
    "Physical address",
    # "Interface type",
    # "State",
    "SSID",
    # "AP BSSID",
    "Band",
    "Channel",
    # "Connected Akm-cipher",
    # "Network type",
    "Radio type",
    # "Authentication",
    # "Cipher",
    # "Connection mode",
    "Receive rate (Mbps)",
    "Transmit rate (Mbps)",
    "Signal",
    "Rssi",
    # "Profile",
    # "QoS MSCS Configured",
    # "QoS Map Configured",
    # "QoS Map Allowed by Policy",
}


def get_wlan_info():
    # Run the PowerShell command
    result = subprocess.run(
        ["powershell", "-Command", "netsh wlan show interfaces"], capture_output=True, text=True, encoding="utf-8"
    )

    output = result.stdout.splitlines()
    info = {}

    # Regex to match "Key : Value" lines
    pattern = re.compile(r"^\s*([^:]+)\s*:\s*(.*)$")

    for line in output:
        match = pattern.match(line)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()

            if key in SELECTED_FIELDS:
                info[key] = value

    return info


def main():
    wlan_info = get_wlan_info()
    console = Console()
    table = Table(title="Wireless Interface", show_lines=True)

    table.add_column("Field", style="bold cyan")
    table.add_column("Value", style="bold yellow")

    for field in SELECTED_FIELDS:
        # Maintain predictable ordering for the selected netsh fields
        table.add_row(field, wlan_info.get(field, "N/A"))

    console.print(table)


if __name__ == "__main__":
    main()
