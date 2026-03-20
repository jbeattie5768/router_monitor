# uv run --with requests --with python-dotenv .\scrape_wifi2.py

import ast
import hashlib
import os
import re

import dotenv
import requests
from dotenv import find_dotenv, load_dotenv

# Constants
# ###################################
ROUTER_IP = "http://192.168.1.1"
LOGIN_HTM = f"{ROUTER_IP}/login.htm"
LOGIN_CGI = f"{ROUTER_IP}/login.cgi"
# STATUS_XML = f"{ROUTER_IP}/status_conn.xml"
STATUS_HTM = f"{ROUTER_IP}/status.htm"
DSL_STATUS_JS = f"{ROUTER_IP}/cgi/cgi_dsl_status.js"

# USER and Password in .env file
# ###################################
dotenv_path = find_dotenv()
print(dotenv_path)
load_dotenv(dotenv_path)

_userlogin = os.getenv("USERLOGIN")  # using USERLOGIN to avoid confusion with OS USERNAME
_password = os.getenv("PASSWORD")

if _userlogin is None or _password is None:
    msg = "USERLOGIN and PASSWORD must be set in .env"
    raise RuntimeError(msg)

# Login Process
# ###################################
session = requests.Session()
# sess_resp = session.get(ROUTER_IP, allow_redirects=False)
# print(sess_resp.status_code)
sess_resp = session.get(LOGIN_HTM, timeout=5)
print(sess_resp.status_code)

payload: dict[str, str] = {
    "usr": _userlogin,
    # MD5 is a weak hash function, but this is what the router expects
    "pws": hashlib.md5(_password.encode("utf-8")).hexdigest(),  # noqa: S324
    "GO": "status.htm",
}

cgi_resp = session.post(LOGIN_CGI, data=payload, timeout=5)
print(cgi_resp.status_code)


# Wait for the router to process the login and set cookies
# ###################################
# time.sleep(20)  # Wait for the router to process the login


# xml_headers = {
#     "Connection": "keep-alive",
#     "cookie": "menu_sel=0; menu_adv=0; defpg=status%2Ehtm; urn=46bfab72d1318bb2",
# }

# xml_resp = session.get(STATUS_XML, headers=xml_headers, timeout=5)
# print(xml_resp.status_code)
# print(xml_resp.text)
# xml_resp.raise_for_status()
# root = ET.fromstring(xml_resp.text)

# Get Router Generated Cookie
# ###################################
status_resp = session.get(STATUS_HTM)  # status.htm to get the JS set URN cookie
print(status_resp.status_code)
# print(status_resp.text)

# status_resp = session.get(STATUS_HTM)
# print(status_resp.status_code)
# print(status_resp.text)

# Parse the HTML to get the cookie value for URN
match = re.search(r"var\s+new_urn\s*=\s*'([^']+)'", status_resp.text)
if match is None:
    print(status_resp.text)
    msg = "Could not extract new_urn in status.htm response"
    raise RuntimeError(msg)
urn_value = match.group(1)
# urn_cookie = f"urn={urn_value}; menu_sel=2; defpg=system%5Fadv%2Ehtm;"
print("Extracted URN Cookie:", urn_value)
# Add the cookie to the headers for the CGI request
cgi_headers = {
    "Connection": "keep-alive",
    "Cookie": f"urn={urn_value}; menu_sel=2; defpg=system%5Fadv%2Ehtm;",
}
# simple_cookie = http.cookies.SimpleCookie(urn_cookie)
# cookie_jar = requests.cookies.RequestsCookieJar()
# cookie_jar.update(simple_cookie)

# Get the JS DSL Data
dsl_resp = session.get(DSL_STATUS_JS, headers=cgi_headers, timeout=5)
dsl_resp.raise_for_status()
print(dsl_resp.status_code)
# print(dsl_resp.text)

# Extract JS variables
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


data = {name: parse_literal(value) for name, value in var_pattern.findall(dsl_resp.text)}
data["addCfg"] = [
    {"name": name, "id": int(cfg_id), "value": flag} for name, cfg_id, flag in cfg_pattern.findall(dsl_resp.text)
]

# print(json.dumps(data, indent=2))

print(f"Downstream Rate: {data['xdslLineStatus'][0]['rate_down']} kbps")
print(f"Upstream Rate: {data['xdslLineStatus'][0]['rate_up']} kbps")
print(f"DownStream Noise Margin: {int(data['xdslLineStatus'][0]['snr_margin_down']) / 10} dB")
print(f"UpStream Noise Margin: {int(data['xdslLineStatus'][0]['snr_margin_up']) / 10} dB")
print(f"DownStream Line Attenuation: {int(data['xdslLineStatus'][0]['latn_down']) / 10} dB")
print(f"UpStream Line Attenuation: {int(data['xdslLineStatus'][0]['latn_up']) / 10} dB")
print(f"DownStream Power: {int(data['xdslLineStatus'][0]['output_power_down']) / 10} dBm")
print(f"UpStream Power: {int(data['xdslLineStatus'][0]['output_power_up']) / 10} dBm")
print(f"Downstream Attainable Rate: {int(data['xdslLineStatus'][0]['attainable_rate_down']) / 1000} kbps")
print(f"Upstream Attainable Rate: {int(data['xdslLineStatus'][0]['attainable_rate_up']) / 1000} kbps")
