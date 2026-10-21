"""The provider names itself to the Evaluation API (#4153, contract in #4138).

Requests made through the provider carry
``featureflip-python/<v> featureflip-openfeature-python/<v>``, so OpenFeature
usage can be counted apart from direct SDK use. Asserted on the wire through a
real ``FeatureflipClient``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from typing import ClassVar

from featureflip import Config
from featureflip._core import _reset_for_testing
from featureflip._version import USER_AGENT
from openfeature.evaluation_context import EvaluationContext

from featureflip_openfeature.provider import WRAPPER_NAME, FeatureflipProvider

PROVIDER_TOKEN = f"featureflip-openfeature-python/{version('featureflip-openfeature-provider')}"


class _EvalApi(BaseHTTPRequestHandler):
    """Stands in for the Evaluation API: records each User-Agent, serves no flags."""

    agents: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        _EvalApi.agents.append(self.headers.get("User-Agent", ""))
        body = json.dumps({"environment": "e", "version": 1, "flags": [], "segments": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _user_agent_after_initialize(**config_fields: object) -> str:
    """Initializes a provider against a local server and returns the first User-Agent it sent."""
    _reset_for_testing()
    _EvalApi.agents = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EvalApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = Config(
            base_url=f"http://127.0.0.1:{server.server_port}", streaming=False, send_events=False, **config_fields
        )
        provider = FeatureflipProvider(sdk_key="identity-key", config=config)
        provider.initialize(EvaluationContext())
        provider.shutdown()
        _user_agent_after_initialize.last_config = config  # type: ignore[attr-defined]
        return _EvalApi.agents[0]
    finally:
        server.shutdown()
        server.server_close()
        _reset_for_testing()


def test_requests_through_the_provider_carry_its_token() -> None:
    assert _user_agent_after_initialize() == f"{USER_AGENT} {PROVIDER_TOKEN}"


def test_the_callers_config_is_left_untouched() -> None:
    # The wrapper goes on a copy; mutating a Config the caller may reuse elsewhere
    # would leak the provider's identity into their direct SDK clients.
    _user_agent_after_initialize()

    assert _user_agent_after_initialize.last_config.wrapper_name is None  # type: ignore[attr-defined]


def test_a_wrapper_the_caller_already_set_is_kept() -> None:
    # The Python SDK carries one wrapper token. A caller that already names its own
    # wrapper is more specific than this provider, so theirs wins.
    agent = _user_agent_after_initialize(wrapper_name="acme-platform", wrapper_version="3.1")

    assert agent == f"{USER_AGENT} acme-platform/3.1"


def test_wrapper_name_constant() -> None:
    assert WRAPPER_NAME == "featureflip-openfeature-python"
