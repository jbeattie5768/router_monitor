# uv run --with requests --with python-dotenv .\scrape_wifi2.py

import argparse
import ast
import hashlib
import logging
import os
import re

import requests
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()

# Constants
# ###################################
ROUTER_IP = "http://192.168.1.1"
LOGIN_HTM = f"{ROUTER_IP}/login.htm"
LOGIN_CGI = f"{ROUTER_IP}/login.cgi"
# STATUS_XML = f"{ROUTER_IP}/status_conn.xml"
STATUS_HTM = f"{ROUTER_IP}/status.htm"
DSL_STATUS_JS = f"{ROUTER_IP}/cgi/cgi_dsl_status.js"


def parse_cli_args(argv=None):
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
    elif verbosity >= 2:
        log_level = logging.DEBUG
    elif verbosity == 1:
        log_level = logging.INFO
    else:
        log_level = logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.setLevel(log_level)


var_pattern = re.compile(r"var\s+([A-Za-z0-9_]+)\s*=\s*(.*?);", re.DOTALL)
cfg_pattern = re.compile(r'addCfg\("([^"]+)",\s*([0-9]+),\s*\'([01])\'\);')


# Converts a JS literal (objects/arrays) into a Python literal string
def js_literal_to_python(js_literal: str) -> str:
    def wrap_key(match: re.Match[str]) -> str:
        return f"'{match.group(1)}':"

    text = js_literal
    text = text.replace("\\'", "'")  # undo \' escapes from the router output
    text = text.rstrip()
    if text.endswith(";"):
        text = text.removesuffix(";")
    text = re.sub(r"(?<=\{|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", wrap_key, text)
    text = re.sub(r"\bnull\b", "None", text)
    text = re.sub(r"\btrue\b", "True", text)
    text = re.sub(r"\bfalse\b", "False", text)

    return text


def parse_literal(js_literal: str):
    return ast.literal_eval(js_literal_to_python(js_literal))


def format_tenths(value: str, unit: str) -> str:
    return f"{int(value) / 10:.1f} {unit}"


def format_rate(value: str, divisor: int = 1) -> str:
    rate = int(value) / divisor
    return f"{rate:.0f} kbps"


def build_metrics_table(line_status: dict[str, str]) -> Table:
    table = Table(title="BrightBox DSL Metrics", header_style="bold cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Downstream", justify="right")
    table.add_column("Upstream", justify="right")

    table.add_row("Rate", format_rate(line_status["rate_down"]), format_rate(line_status["rate_up"]))
    table.add_row(
        "Noise Margin",
        format_tenths(line_status["snr_margin_down"], "dB"),
        format_tenths(line_status["snr_margin_up"], "dB"),
    )
    table.add_row(
        "Line Attenuation",
        format_tenths(line_status["latn_down"], "dB"),
        format_tenths(line_status["latn_up"], "dB"),
    )
    table.add_row(
        "Power",
        format_tenths(line_status["output_power_down"], "dBm"),
        format_tenths(line_status["output_power_up"], "dBm"),
    )
    table.add_row(
        "Attainable Rate",
        format_rate(line_status["attainable_rate_down"], 1000),
        format_rate(line_status["attainable_rate_up"], 1000),
    )

    return table


def main(argv=None):
    args = parse_cli_args(argv)
    configure_logging(args.verbose)

    # USER and Password in .env file
    # ###################################
    dotenv_path = find_dotenv()
    logger.debug("Resolved .env path: %s", dotenv_path)
    load_dotenv(dotenv_path)

    _userlogin = os.getenv("USERLOGIN")  # using USERLOGIN to avoid confusion with OS USERNAME
    _password = os.getenv("PASSWORD")

    if _userlogin is None or _password is None:
        msg = "USERLOGIN and PASSWORD must be set in .env"
        logger.error(msg)
        raise RuntimeError(msg)

    # Login Process
    # ###################################
    session = requests.Session()
    logger.info("Attempting initial GET to %s", LOGIN_HTM)
    sess_resp = session.get(LOGIN_HTM, timeout=5)
    logger.info("Initial GET status: %s", sess_resp.status_code)

    payload: dict[str, str] = {
        "usr": _userlogin,
        # MD5 is a weak hash function, but this is what the router expects
        "pws": hashlib.md5(_password.encode("utf-8")).hexdigest(),  # noqa: S324
        "GO": "status.htm",
    }

    logger.info("Submitting login form to %s", LOGIN_CGI)
    cgi_resp = session.post(LOGIN_CGI, data=payload, timeout=5)
    logger.info("Login POST status: %s", cgi_resp.status_code)

    # Get Router Generated Cookie
    # ###################################
    logger.info("Requesting status page %s for URN", STATUS_HTM)
    status_resp = session.get(STATUS_HTM)
    logger.info("Status page response: %s", status_resp.status_code)

    # Parse the HTML to get the cookie value for URN
    match = re.search(r"var\s+new_urn\s*=\s*'([^']+)'", status_resp.text)
    if match is None:
        logger.error("Could not extract new_urn in status.htm response: %s", status_resp.text)
        msg = "Could not extract new_urn in status.htm response"
        raise RuntimeError(msg)
    urn_value = match.group(1)
    logger.info("Extracted URN cookie: %s", urn_value)

    # Add the cookie to the headers for the CGI request
    cgi_headers = {
        "Connection": "keep-alive",
        "Cookie": f"urn={urn_value}; menu_sel=2; defpg=system%5Fadv%2Ehtm;",
    }

    # Get the JS DSL Data
    logger.info("Requesting DSL status JS from %s", DSL_STATUS_JS)
    dsl_resp = session.get(DSL_STATUS_JS, headers=cgi_headers, timeout=5)
    dsl_resp.raise_for_status()
    logger.info("DSL status response: %s", dsl_resp.status_code)

    data = {name: parse_literal(value) for name, value in var_pattern.findall(dsl_resp.text)}
    data["addCfg"] = [
        {"name": name, "id": int(cfg_id), "value": flag} for name, cfg_id, flag in cfg_pattern.findall(dsl_resp.text)
    ]

    # Use a table for easy scanning of results.
    # Alternative would be structured log using key/value pairs:
    # e.g. logger.info("dsl_metrics", extra={"rate_down": data["xdslLineStatus"][0]["rate_down"]...})
    table = build_metrics_table(data["xdslLineStatus"][0])
    logger.info("Rendered DSL metrics table")
    console.print(table)


if __name__ == "__main__":
    main()
