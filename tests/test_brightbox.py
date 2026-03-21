import ast
import hashlib
from types import SimpleNamespace

import pytest
from rich.console import Console

from router_monitor import brightbox

SAMPLE_JS = """
var xdslLineStatus = [{state:'UP',mode:'ADSL2%2B',snr_margin_down:'71',snr_margin_up:'38',
latn_down:'615',latn_up:'450',output_power_down:'120',output_power_up:'204',
rate_down:'4333',rate_up:'322',attainable_rate_down:'5156000',attainable_rate_up:'664000'}];
"""


def test_js_literal_to_python_round_trip():
    js_literal = "{state:'UP',snr_margin_down:'71',flag:true,missing:null}"
    python_literal = brightbox.js_literal_to_python(js_literal)
    data = ast.literal_eval(python_literal)
    assert data["state"] == "UP"
    assert data["flag"] is True
    assert data["missing"] is None


def test_extract_js_variable_parses_array():
    js_blob = SAMPLE_JS + "var unrelated = [];;"
    parsed = brightbox.extract_js_variable(js_blob, "xdslLineStatus")
    assert parsed[0]["rate_down"] == "4333"


def test_extract_js_variable_missing_raises():
    with pytest.raises(ValueError):
        brightbox.extract_js_variable("var foo = [];", "missingVar")


def test_build_metrics_table_renders_metrics():
    line_status = {
        "rate_down": "4333",
        "rate_up": "322",
        "snr_margin_down": "71",
        "snr_margin_up": "38",
        "latn_down": "615",
        "latn_up": "450",
        "output_power_down": "120",
        "output_power_up": "204",
        "attainable_rate_down": "5156000",
        "attainable_rate_up": "664000",
    }
    table = brightbox.build_metrics_table(line_status)
    console = Console(record=True)
    console.print(table)
    rendered = console.export_text()
    assert "BrightBox DSL Metrics" in rendered
    assert "4333" in rendered
    assert "Attainable Rate" in rendered


def test_load_credentials_reads_env(monkeypatch):
    monkeypatch.setenv("USERLOGIN", "routerUser")
    monkeypatch.setenv("PASSWORD", "routerPass")
    monkeypatch.setattr(brightbox, "find_dotenv", lambda: "/tmp/.env")
    called = {}

    def fake_load(path: str) -> None:
        called["path"] = path

    monkeypatch.setattr(brightbox, "load_dotenv", fake_load)
    username, password = brightbox.load_credentials()
    assert (username, password) == ("routerUser", "routerPass")
    assert called["path"] == "/tmp/.env"


def test_load_credentials_missing_env_raises(monkeypatch):
    monkeypatch.delenv("USERLOGIN", raising=False)
    monkeypatch.delenv("PASSWORD", raising=False)
    monkeypatch.setattr(brightbox, "find_dotenv", lambda: "/tmp/.env")
    monkeypatch.setattr(brightbox, "load_dotenv", lambda path: None)
    with pytest.raises(RuntimeError):
        brightbox.load_credentials()


def test_create_authenticated_session_makes_requests(monkeypatch):
    class DummyResponse:
        def __init__(self, status_code: int = 200) -> None:
            self.status_code = status_code

    class DummySession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None]] = []

        def get(self, url: str, timeout: int | None = None) -> DummyResponse:
            self.calls.append(("get", url, {"timeout": timeout}))
            return DummyResponse()

        def post(
            self,
            url: str,
            data: dict[str, str] | None = None,
            timeout: int | None = None,
        ) -> DummyResponse:
            payload = dict(data or {})
            payload["timeout"] = timeout
            self.calls.append(("post", url, payload))
            return DummyResponse()

    monkeypatch.setattr(brightbox.requests, "Session", DummySession)
    session = brightbox.create_authenticated_session("user", "secret")
    assert session.calls[0] == ("get", brightbox.LOGIN_HTM, {"timeout": 5})
    verb, url, payload = session.calls[1]
    assert verb == "post"
    assert url == brightbox.LOGIN_CGI
    expected_hash = hashlib.md5(b"secret").hexdigest()
    assert payload["pws"] == expected_hash


def test_fetch_line_status_returns_first_entry():
    status_response = SimpleNamespace(status_code=200, text="var new_urn = 'abc123';")

    class DSLResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.text = SAMPLE_JS

        def raise_for_status(self) -> None:
            return None

    class StubSession:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict | None]] = []

        def get(
            self,
            url: str,
            timeout: int | None = None,
            headers: dict[str, str] | None = None,
        ) -> object:
            if url == brightbox.STATUS_HTM:
                self.requests.append((url, None))
                return status_response
            if url == brightbox.DSL_STATUS_JS:
                self.requests.append((url, headers))
                return DSLResponse()
            raise AssertionError("Unexpected URL")

    session = StubSession()
    result = brightbox.fetch_line_status(session)  # type: ignore[arg-type]
    assert result["rate_down"] == "4333"
    assert session.requests[1][1]["Cookie"].startswith("urn=abc123")


def test_configure_logging_respects_env(monkeypatch):
    captured = {}

    def fake_basic_config(**kwargs):
        captured.update(kwargs)

    def fake_set_level(level: int) -> None:
        captured["set_level"] = level

    monkeypatch.setenv("ROUTER_MONITOR_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(brightbox.logging, "basicConfig", fake_basic_config)
    monkeypatch.setattr(brightbox.logger, "setLevel", fake_set_level)
    brightbox.configure_logging(verbosity=0)
    assert captured["level"] == brightbox.logging.DEBUG
    assert captured["set_level"] == brightbox.logging.DEBUG
