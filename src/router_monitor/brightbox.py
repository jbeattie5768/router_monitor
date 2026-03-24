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
from typing import TYPE_CHECKING, Final, cast

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

# Find the key name *before* a colon (:) and match the name and colon, e.g. state:
KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<=\{|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")

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
    logger.debug("Resolved .env path: %s", dotenv_path)
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
    """Log in to the router and return an authenticated session."""
    session = requests.Session()
    logger.info("Attempting initial GET to %s", LOGIN_HTM)

    # requests.adapter has a retry mechanism for certain errors, but we'll implement our own for ConnectTimeoutError
    # Use an exponential backoff <https://en.wikipedia.org/wiki/Exponential_backoff>
    backoff_multiplier = 0.5  # seconds, can be adjusted based on expected router response times
    max_retries = 6  # e.g. 1 + 2 + 4 + 8 + 16 + 32 = 63sec total wait time if all retries needed

    for this_attempt in range(1, max_retries + 1):
        try:
            session_resp = session.get(LOGIN_HTM, timeout=5)
            logger.info("GET status: %s", session_resp.status_code)
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


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_cli_args(argv)
    configure_logging(args.verbose)

    userlogin, password = load_credentials()
    session = create_authenticated_session(userlogin, password)
    line_status = fetch_line_status(session)

    console.print(build_metrics_table(line_status))


if __name__ == "__main__":
    main()
