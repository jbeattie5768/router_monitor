# uv run --with requests --with python-dotenv .\scrape_wifi2.py
# uv run ./src/router_monitor/brightbox.py

from __future__ import annotations

import argparse
import ast
import hashlib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from ast import literal_eval
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import unquote

import requests
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from collections.abc import Sequence


console = Console()

# Constants
# ###################################
ROUTER_IP: Final[str] = "http://192.168.1.1"

LOGIN_HTM: Final[str] = f"{ROUTER_IP}/login.htm"
LOGIN_CGI: Final[str] = f"{ROUTER_IP}/login.cgi"
STATUS_HTM: Final[str] = f"{ROUTER_IP}/status.htm"
DSL_STATUS_JS: Final[str] = f"{ROUTER_IP}/cgi/cgi_dsl_status.js"
STATUS_CONN_XML: Final[str] = f"{ROUTER_IP}/status_conn.xml"

# Find the key name *before* a colon (:) and match the name and colon, e.g. state:
KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<=\{|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")

# Values taken from cgi/cgi_status.js in the router's web interface
# Optionally, we could read these direct from the JS file
WAN_TYPE: dict[str, int] = {
    # "Ethernet": 1,
    "ADSL": 2,  # only interested in the DSL status for now
}

# JS var entries in dsl_status.js
EXTRACT_DATA: Final[list[str]] = [
    # "WAN_TYPE",
    # "ADSL_OP_MODE_LIST",
    "xdslLineStatus",  # contains the key metrics we want to extract
    # "xdslLinePerf",
    # "xdslAtmStat",
    # "xdslChannPerf",
    # "xdslStatistics",
]


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor BrightBox DSL statistics")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )
    return parser.parse_args(argv)


def configure_logging(verbosity: int) -> None:
    env_override = os.getenv("ROUTER_MONITOR_LOG_LEVEL")
    if env_override:
        normalized = env_override.upper()
        log_level = getattr(logging, normalized, None)
        if not isinstance(log_level, int):
            msg = f"Invalid log level: {env_override}"
            raise ValueError(msg)
    elif verbosity >= 2:  # noqa: PLR2004
        log_level = logging.DEBUG
    elif verbosity == 1:
        log_level = logging.INFO
    else:
        log_level = logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.setLevel(log_level)


def js_literal_to_python(js_literal: str) -> str:
    """Convert a JS-ish object literal into something ast.literal_eval understands."""

    def wrap_key(match: re.Match[str]) -> str:
        """Enclose matched keys in single quotes, e.g. state: -> 'state':"""
        return f"'{match.group(1)}':"

    # Replace JS literals with Python equivalents
    var_text = (
        js_literal.replace("\\'", "'")
        .replace("%2B", "+")  # could use urllib.parse.unquote("%2B")
        .replace("null", "None")
        .replace("true", "True")
        .replace("false", "False")
    )
    var_text = KEY_PATTERN.sub(wrap_key, var_text)

    return var_text


def build_metrics_table(line_status: dict[str, str]) -> Table:
    table = Table(title="BrightBox DSL Metrics", header_style="bold cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Downstream", justify="right")
    table.add_column("Upstream", justify="right")

    table.add_row(
        "Rate (kbps)",
        f"{int(line_status['rate_down']):.0f}",
        f"{int(line_status['rate_up']):.0f}",
    )
    table.add_row(
        "Noise Margin (dB)",
        f"{int(line_status['snr_margin_down']) / 10:.1f}",
        f"{int(line_status['snr_margin_up']) / 10:.1f}",
    )
    table.add_row(
        "Line Attenuation (dB)",
        f"{int(line_status['latn_down']) / 10:.1f}",
        f"{int(line_status['latn_up']) / 10:.1f}",
    )
    table.add_row(
        "Power (dBm)",
        f"{int(line_status['output_power_down']) / 10:.1f}",
        f"{int(line_status['output_power_up']) / 10:.1f}",
    )
    table.add_row(
        "Attainable Rate (kbps)",
        f"{int(line_status['attainable_rate_down']) / 1000:.0f}",
        f"{int(line_status['attainable_rate_up']) / 1000:.0f}",
    )

    return table


def load_credentials() -> tuple[str, str]:
    """Load USERLOGIN/PASSWORD from the .env file."""
    dotenv_path = find_dotenv()
    load_dotenv(dotenv_path)
    logger.debug("Loaded .env file from path: %s", dotenv_path)

    userlogin = os.getenv("USERLOGIN")
    password = os.getenv("PASSWORD")
    if userlogin is None or password is None:
        msg = "USERLOGIN and PASSWORD must be set in .env"
        logger.error(msg)
        raise RuntimeError(msg)

    return userlogin, password


def create_authenticated_session(userlogin: str, password: str) -> requests.Session:
    """Login to the router and return an authenticated session."""
    session = requests.Session()

    # requests.adapter has a retry mechanism for certain errors, but we'll implement our own for ConnectTimeoutError
    # Use an exponential backoff <https://en.wikipedia.org/wiki/Exponential_backoff>
    backoff_multiplier = 0.5  # seconds, can be adjusted based on expected router response times
    max_retries = 6  # e.g. 1 + 2 + 4 + 8 + 16 + 32 = 63sec total wait time if all retries needed

    logger.debug("Checking %s is available", ROUTER_IP)
    for this_attempt in range(1, max_retries + 1):
        req_resp = requests.get(ROUTER_IP, timeout=5)
        logger.debug("GET status: %s", req_resp.status_code)
        if req_resp.status_code == requests.codes.ok:
            break

        if this_attempt <= max_retries:
            connect_delay = backoff_multiplier * (2**this_attempt)
            logger.warning(
                "(Attempt %d/%d) Router not responding at %s, retrying in %d seconds",
                this_attempt,
                max_retries,
                ROUTER_IP,
                connect_delay,
            )
            time.sleep(connect_delay)

    # Now login
    logger.debug("Logging into router: %s", LOGIN_HTM)
    for this_attempt in range(1, max_retries + 1):
        try:
            session_resp = session.get(LOGIN_HTM, timeout=5)
            logger.debug("GET status: %s", session_resp.status_code)
            break
        except (ConnectionError, requests.Timeout) as e:
            if this_attempt <= max_retries:
                connect_delay = backoff_multiplier * (2**this_attempt)
                logger.warning(
                    "(Attempt %d/%d) Connection %s failed, retrying in %d seconds",
                    this_attempt,
                    max_retries,
                    LOGIN_HTM,
                    connect_delay,
                )
                logger.info("Connection failure error: %s", e)
                time.sleep(connect_delay)
            else:
                raise  # last exception will propagate

    payload = {
        "usr": userlogin,
        "pws": hashlib.md5(password.encode("utf-8")).hexdigest(),  # noqa: S324
        "GO": "status.htm",
    }
    logger.info("Submitting login form to %s", LOGIN_CGI)
    cgi_resp = session.post(LOGIN_CGI, data=payload, timeout=5)
    logger.info("POST status: %s", cgi_resp.status_code)
    return session


def extract_js_variable(js_blob: str, variable: str) -> list[dict[str, str] | None]:
    """Extract and parse a JS variable assignment into Python objects."""
    pattern = r"var\s+" + re.escape(variable) + r"\s*=\s*([\[{][\s\S]*?);"
    match = re.search(pattern, js_blob, re.DOTALL)
    if not match:
        msg = f"Variable {variable} not found in JS blob"
        logger.error(msg)
        raise ValueError(msg)
    python_literal = js_literal_to_python(match.group(1))
    return cast("list[dict[str, str] | None]", ast.literal_eval(python_literal))


def fetch_line_status(session: requests.Session) -> dict[str, str]:
    """Fetch DSL status JS and return the first xdslLineStatus entry."""
    urn_value = None  # assign to prevent `reportPossiblyUnboundVariable`
    logger.info("Attempting initial GET to %s", STATUS_HTM)

    # STATUS_HTML has been seen to not be populated with URN Cookie
    # If that happens, retry a few times
    backoff_multiplier = 0.5  # seconds, can be adjusted based on expected router response times
    max_retries = 6  # e.g. 1 + 2 + 4 + 8 + 16 + 32 = 63sec total wait time if all retries needed

    for this_attempt in range(1, max_retries + 1):
        status_resp = session.get(STATUS_HTM)
        logger.info("GET status: %s", status_resp.status_code)

        match = re.search(r"var\s+new_urn\s*=\s*'([^']+)'", status_resp.text)
        if match is None:
            if this_attempt <= max_retries:
                connect_delay = backoff_multiplier * (2**this_attempt)
                logger.warning(
                    "(Attempt %d/%d) failed to parse new_urn, retrying in %d seconds",
                    this_attempt,
                    max_retries,
                    connect_delay,
                )
                time.sleep(connect_delay)
            else:
                logger.debug("Response content for debugging:\n%s", status_resp.text)
                msg = f"Could not extract new_urn from {STATUS_HTM} after {max_retries} attempts"
                logger.error(msg)
                raise RuntimeError(msg)
        else:
            urn_value = match.group(1)
            logger.info("Extracted URN cookie: %s", urn_value)
            break

    headers = {
        "Connection": "keep-alive",
        "Cookie": f"urn={urn_value}; menu_sel=2; defpg=system%5Fadv%2Ehtm;",
    }
    logger.info("Requesting DSL status JS from %s", DSL_STATUS_JS)
    dsl_resp = session.get(DSL_STATUS_JS, headers=headers, timeout=5)
    dsl_resp.raise_for_status()
    logger.info("DSL status response: %s", dsl_resp.status_code)

    parsed = extract_js_variable(dsl_resp.text, "xdslLineStatus")
    line_status = parsed[0]
    if line_status is None:
        msg = "xdslLineStatus returned no data"
        logger.error(msg)
        raise RuntimeError(msg)
    return line_status


@dataclass(slots=True)
class InternetState:
    """High-level summary of one WAN interface."""

    wan_type: str
    slot: int
    raw_state: str
    is_enabled: bool
    is_connected: bool
    sys_up_seconds: str | None
    uptime_seconds: int | None
    uptime_hms: str | None


def _parse_router_array(root: ET.Element, tag: str) -> list[Any]:
    node = root.find(tag)
    if node is None:
        msg = f"{tag} missing from status file"
        raise KeyError(msg)

    raw_value = unquote(node.attrib["value"])
    sanitized = raw_value.replace("null", "None")
    return literal_eval(sanitized)


def _parse_state(entry: list[str] | None) -> tuple[str, int | None]:
    if not entry:
        msg = "Empty WAN status entry"
        raise ValueError(msg)

    parts = entry[0].split(";")
    state = parts[0]
    uptime = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return state, uptime


def _format_hms(seconds: int | None) -> str | None:
    """Return an HH:MM:SS string for the supplied duration."""

    if seconds is None:
        return None

    total_seconds = int(timedelta(seconds=seconds).total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def internet_state(xml_data: str, wan_type: str) -> InternetState:
    """Return the connection status for the requested WAN type."""
    try:
        slot = WAN_TYPE[wan_type]
    except KeyError as exc:
        msg = f"WAN type '{wan_type}' is not recognized. Valid types: {list(WAN_TYPE.keys())}"
        raise ValueError(msg) from exc

    # XML parsers are vulnerable to XML attacks, but we trust the router's output
    # Use <https://pypi.org/project/defusedxml/.ElementTree> if concerned
    root = ET.fromstring(xml_data)
    wan_enable = _parse_router_array(root, "wan_enable")
    wan_conn_status = _parse_router_array(root, "wanConnStatus")
    sys_up = root.find("sys_up_time")
    sys_up_seconds = int(sys_up.attrib["value"]) if sys_up is not None else None

    is_enabled = bool(wan_enable[slot - 1] and wan_enable[slot - 1][0] == "1")
    raw_state, since_boot = _parse_state(wan_conn_status[slot - 1])
    is_connected = is_enabled and raw_state == "connected"
    uptime_seconds = None
    if since_boot and since_boot != 0 and sys_up_seconds is not None and sys_up_seconds >= since_boot:
        uptime_seconds = sys_up_seconds - since_boot
    sys_up_seconds = _format_hms(sys_up_seconds) if sys_up_seconds is not None else None
    uptime_hms = _format_hms(uptime_seconds)

    return InternetState(
        wan_type=wan_type,
        slot=slot,
        raw_state=raw_state,
        is_enabled=is_enabled,
        is_connected=is_connected,
        sys_up_seconds=sys_up_seconds,
        uptime_seconds=uptime_seconds,
        uptime_hms=uptime_hms,
    )


def fetch_connection_status(session: requests.Session) -> None | InternetState:
    """Fetch the connection status from the router."""
    state = None
    status_resp = session.get(STATUS_CONN_XML)
    logger.info("GET status: %s", status_resp.status_code)
    for name in WAN_TYPE:
        state = internet_state(status_resp.text, name)
        logger.debug(
            "%s, %s, %s, %s, %s, %s, %s, %s)",
            state.wan_type,
            state.slot,
            state.raw_state,
            state.is_enabled,
            state.is_connected,
            state.sys_up_seconds,
            state.uptime_seconds,
            state.uptime_hms,
        )

    return state


def _print_connection_status(state: InternetState) -> None:
    enabled = "enabled" if state.is_enabled else "disabled"
    online = "online" if state.is_connected else "offline"
    print(f"System uptime: {state.sys_up_seconds or 'N/A'}")
    print(
        f"{state.wan_type} (slot {state.slot}) is {enabled} and {online} (state={state.raw_state}, uptime={state.uptime_hms or 'N/A'})"
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli_args(argv)
    configure_logging(args.verbose)

    userlogin, password = load_credentials()
    session = create_authenticated_session(userlogin, password)
    line_status = fetch_line_status(session)
    console.print(build_metrics_table(line_status))

    connection_status = fetch_connection_status(session)
    if connection_status:
        _print_connection_status(connection_status)


if __name__ == "__main__":
    main()
