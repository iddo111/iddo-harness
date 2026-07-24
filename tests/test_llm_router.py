"""
Tests for agent/llm_router.py — LlmRouter primary/secondary failover.

Uses `respx` to mock httpx traffic to the (fake) Eranim vLLM endpoints so
no real network calls happen. Covers:

  - parse_llm_backends() shape parsing from a policy.yaml-shaped dict
  - route() returns the primary when healthy
  - route() falls back to the secondary when the primary's /v1/models
    health check fails
  - route() raises NoHealthyBackendError when neither is healthy
  - route() raises KeyError for an unconfigured role
  - status() reports per-backend health rows
"""
import respx
import httpx
import pytest

from llm_router import (
    LlmRouter,
    NoHealthyBackendError,
    parse_llm_backends,
)

CODER_PRIMARY = "http://eran1.local:8000/v1"
CODER_SECONDARY = "http://eran3.local:8000/v1"
REASONER_PRIMARY = "http://eran3.local:8000/v1"

LLM_BACKENDS_CFG = {
    "coder": {
        "primary": {"base_url": CODER_PRIMARY, "model": "glm-4.6"},
        "secondary": {"base_url": CODER_SECONDARY, "model": "glm-4.6"},
        "health_check_interval": 60,
    },
    "reasoner": {
        "primary": {"base_url": REASONER_PRIMARY, "model": "deepseek-v3.1"},
        "health_check_interval": 60,
    },
}


# ---------------------------------------------------------------------------
# parse_llm_backends
# ---------------------------------------------------------------------------

def test_parse_llm_backends_shape():
    roles = parse_llm_backends(LLM_BACKENDS_CFG)
    assert set(roles) == {"coder", "reasoner"}

    coder = roles["coder"]
    assert coder.primary.base_url == CODER_PRIMARY
    assert coder.primary.model == "glm-4.6"
    assert coder.secondary is not None
    assert coder.secondary.base_url == CODER_SECONDARY
    assert coder.health_check_interval == 60

    reasoner = roles["reasoner"]
    assert reasoner.secondary is None
    assert reasoner.primary.model == "deepseek-v3.1"


def test_parse_llm_backends_missing_primary_raises():
    with pytest.raises(ValueError, match="missing a 'primary'"):
        parse_llm_backends({"coder": {"secondary": {"base_url": "x", "model": "y"}}})


# ---------------------------------------------------------------------------
# route() — healthy primary
# ---------------------------------------------------------------------------

@respx.mock
def test_route_returns_primary_when_healthy():
    respx.get(f"{CODER_PRIMARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{CODER_SECONDARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))

    router = LlmRouter(LLM_BACKENDS_CFG)
    router.check_all_once()

    client = router.route("coder")
    assert client.base_url == CODER_PRIMARY
    assert client.model == "glm-4.6"


# ---------------------------------------------------------------------------
# route() — primary down, falls back to secondary
# ---------------------------------------------------------------------------

@respx.mock
def test_route_falls_back_to_secondary_when_primary_unhealthy():
    respx.get(f"{CODER_PRIMARY}/models").mock(return_value=httpx.Response(500))
    respx.get(f"{CODER_SECONDARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))

    router = LlmRouter(LLM_BACKENDS_CFG)
    router.check_all_once()

    client = router.route("coder")
    assert client.base_url == CODER_SECONDARY


@respx.mock
def test_route_falls_back_when_primary_connection_error():
    respx.get(f"{CODER_PRIMARY}/models").mock(side_effect=httpx.ConnectError("connection refused"))
    respx.get(f"{CODER_SECONDARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))

    router = LlmRouter(LLM_BACKENDS_CFG)
    router.check_all_once()

    client = router.route("coder")
    assert client.base_url == CODER_SECONDARY


# ---------------------------------------------------------------------------
# route() — nothing healthy
# ---------------------------------------------------------------------------

@respx.mock
def test_route_raises_when_no_healthy_backend():
    respx.get(f"{CODER_PRIMARY}/models").mock(return_value=httpx.Response(500))
    respx.get(f"{CODER_SECONDARY}/models").mock(return_value=httpx.Response(500))

    router = LlmRouter(LLM_BACKENDS_CFG)
    router.check_all_once()

    with pytest.raises(NoHealthyBackendError):
        router.route("coder")


@respx.mock
def test_route_no_secondary_configured_raises_when_primary_down():
    respx.get(f"{REASONER_PRIMARY}/models").mock(return_value=httpx.Response(500))

    router = LlmRouter(LLM_BACKENDS_CFG)
    router.check_all_once()

    with pytest.raises(NoHealthyBackendError):
        router.route("reasoner")


# ---------------------------------------------------------------------------
# route() — unknown role
# ---------------------------------------------------------------------------

def test_route_unknown_role_raises_keyerror():
    router = LlmRouter(LLM_BACKENDS_CFG)
    with pytest.raises(KeyError):
        router.route("planner")


# ---------------------------------------------------------------------------
# route() — before any health check, optimistic default is "healthy"
# ---------------------------------------------------------------------------

def test_route_before_health_check_defaults_to_primary():
    router = LlmRouter(LLM_BACKENDS_CFG)
    # No check_all_once() called yet — optimistic default lets route()
    # still hand back the primary rather than blocking startup on the
    # first health-check pass.
    client = router.route("coder")
    assert client.base_url == CODER_PRIMARY


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------

@respx.mock
def test_status_reports_all_backend_rows():
    # Use a distinct reasoner-only cfg so its base_url doesn't collide with
    # coder's secondary (both otherwise point at eran3.local:8000 in the
    # shared module-level fixture, which would make respx's route matching
    # for that URL ambiguous between the two different desired responses).
    cfg = {
        "coder": {
            "primary": {"base_url": CODER_PRIMARY, "model": "glm-4.6"},
            "secondary": {"base_url": CODER_SECONDARY, "model": "glm-4.6"},
        },
        "reasoner": {
            "primary": {"base_url": "http://eran4.local:8000/v1", "model": "deepseek-v3.1"},
        },
    }
    respx.get(f"{CODER_PRIMARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{CODER_SECONDARY}/models").mock(return_value=httpx.Response(500))
    respx.get("http://eran4.local:8000/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))

    router = LlmRouter(cfg)
    router.check_all_once()
    rows = router.status()

    by_key = {(r["role"], r["slot"]): r for r in rows}
    assert by_key[("coder", "primary")]["healthy"] is True
    assert by_key[("coder", "secondary")]["healthy"] is False
    assert by_key[("reasoner", "primary")]["healthy"] is True
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# background health-check thread start/stop lifecycle (smoke test)
# ---------------------------------------------------------------------------

@respx.mock
def test_start_stop_health_thread_smoke():
    respx.get(f"{CODER_PRIMARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{CODER_SECONDARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{REASONER_PRIMARY}/models").mock(return_value=httpx.Response(200, json={"data": []}))

    router = LlmRouter(LLM_BACKENDS_CFG)
    router.start()
    assert router._thread is not None and router._thread.is_alive()
    router.stop()
    assert not router._thread.is_alive()
