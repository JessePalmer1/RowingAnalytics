"""The MCP endpoint, exercised over HTTP the way Claude calls it."""

import json

import httpx
import respx
from conftest import result_payload
from fastapi.testclient import TestClient

from erg import api
from erg.mcp_server import athlete_for_token, issue_token
from erg.models import Athlete
from test_pieces_api import load
from test_session import sign_in

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(http, method, params=None, token=None, via_query=False):
    url = "/mcp/"
    headers = dict(HEADERS)
    if token and via_query:
        url += f"?key={token}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return http.post(url, headers=headers, json=body)


def call_tool(http, token, name, **arguments):
    resp = rpc(http, "tools/call", {"name": name, "arguments": arguments}, token)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError"), result
    # Typed list results come back as structured output wrapped in {"result": ...}; plain
    # dict results only as JSON text.
    structured = result.get("structuredContent")
    if structured is not None:
        return structured["result"] if set(structured) == {"result"} else structured
    return json.loads(result["content"][0]["text"])


def test_tokens_resolve_to_one_athlete_and_rotate(db, settings):
    db.add(Athlete(id=42, raw={"id": 42}))
    db.commit()
    first = issue_token(42)
    assert athlete_for_token(first) == 42
    second = issue_token(42)
    assert athlete_for_token(second) == 42
    assert athlete_for_token(first) is None  # minting a new token revokes the old one
    assert athlete_for_token("erg_nonsense") is None


def test_endpoint_rejects_missing_or_bad_tokens(db, settings):
    with TestClient(api.app) as http:
        assert rpc(http, "tools/list").status_code == 401
        assert rpc(http, "tools/list", token="erg_wrong").status_code == 401


@respx.mock
def test_tools_answer_for_the_tokens_athlete(db, settings):
    load(db, settings)
    token = issue_token(42)
    with TestClient(api.app) as http:
        names = {t["name"] for t in rpc(http, "tools/list", token=token).json()["result"]["tools"]}
        assert names == {
            "find_pieces", "get_piece", "compare_pieces", "get_trend",
            "weekly_summary", "training_load", "sync_from_concept2",
        }

        tests = call_tool(http, token, "find_pieces", workout_class="test_2k")
        assert [p["id"] for p in tests] == [21] and tests[0]["description"] == "2k"

        piece = call_tool(http, token, "get_piece", workout_id=21)
        assert piece["classification"]["class"] == "test_2k"

        # Phone connectors can't send headers, so the token also works as ?key=.
        resp = rpc(http, "tools/list", token=token, via_query=True)
        assert resp.status_code == 200


@respx.mock
def test_tools_cannot_reach_another_athletes_data(db, settings):
    load(db, settings)
    db.add(Athlete(id=99, raw={"id": 99}))
    db.commit()
    other = issue_token(99)
    with TestClient(api.app) as http:
        assert call_tool(http, other, "find_pieces") == []
        resp = rpc(http, "tools/call", {"name": "get_piece", "arguments": {"workout_id": 21}}, other)
        assert resp.json()["result"]["isError"] is True


def test_token_endpoint_needs_a_session_and_returns_connection_details(db, settings):
    db.add(Athlete(id=42, raw={"id": 42}))
    db.commit()
    with TestClient(api.app) as http:
        assert http.post("/athletes/me/mcp-token").status_code == 401
        sign_in(http, 42)
        body = http.post("/athletes/me/mcp-token").json()
    assert body["token"].startswith("erg_")
    assert body["connector_url"].endswith(f"/mcp/?key={body['token']}")
    assert body["claude_code_command"].startswith("claude mcp add --transport http erg ")
    assert athlete_for_token(body["token"]) == 42
