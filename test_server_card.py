"""Static MCP server-card test (no network).

The card at GET /.well-known/mcp/server-card.json is the Smithery static
discovery document: it must be publicly reachable (a scanner has no
credentials), identify the server consistently with the Official MCP
Registry listing (server.json), point at the hosted MCP endpoint, declare
the API-key auth model truthfully, and list every tool mcp_server.py
exposes — letters and fax — in tools/list shape.
"""
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["PUBLIC_BASE_URL"] = "https://3lgrouptechnology.com"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init_db()
c = TestClient(api.app)

# 1. Publicly reachable — deliberately NO auth headers of any kind.
r = c.get("/.well-known/mcp/server-card.json")
assert r.status_code == 200, r.status_code
assert r.headers["content-type"].startswith("application/json")
card = r.json()

# 2. Identity matches the registry listing (server.json).
assert card["name"] == "3lt-letter-mail"
assert card["qualifiedName"] == "com.3lgrouptechnology/3lt-letter-mail"
assert card["title"] == "3LT Letter Mail"
assert card["displayName"] == "3lt Letter Mail"
assert card["version"] == "1.0.0"
assert card["status"] == "active"
assert card["repository"]["url"] == \
    "https://github.com/3LGroupTechnology/3lt-letter-mail"
assert "letter" in card["description"].lower()
assert "fax" in card["description"].lower()

# 3. Hosted endpoint, streamable HTTP, built from PUBLIC_BASE_URL.
assert card["remotes"] == [{
    "type": "streamable-http",
    "url": "https://3lgrouptechnology.com/mcp",
}]

# 4. Auth model: API key, truthfully declared (no OAuth claims).
auth = card["authentication"]
assert auth["type"] == "api_key"
assert auth["api_key_format"] == "lms_..."
assert auth["rest_api"] == "X-API-Key header"
assert "api_key argument" in auth["mcp"]
assert card["x-3lt-auth"]["scheme"] == "api_key"

# 5. Every MCP tool is listed, each with description + object inputSchema
#    whose required fields carry the api_key credential.
tools = {t["name"]: t for t in card["tools"]}
expected = {
    "draft_letter", "request_send", "send_status",
    "create_fax_draft", "approve_fax", "send_fax", "get_fax_status",
}
assert set(tools) == expected, set(tools) ^ expected
for name, tool in tools.items():
    assert tool["description"], name
    schema = tool["inputSchema"]
    assert schema["type"] == "object", name
    assert "api_key" in schema["required"], name
    assert schema["properties"]["api_key"]["type"] == "string", name

# 6. Tool-specific schema spot checks (mirror mcp_server.py signatures).
assert set(tools["draft_letter"]["inputSchema"]["required"]) == {
    "api_key", "to", "from_address"}
assert set(tools["create_fax_draft"]["inputSchema"]["required"]) == {
    "api_key", "to_number", "pdf_path"}
assert tools["request_send"]["inputSchema"]["properties"]["draft_id"]["type"] \
    == "integer"
assert tools["send_fax"]["inputSchema"]["properties"]["fax_id"]["type"] \
    == "integer"

os.remove(DB)
print("Server card test passed ✔ (public card, registry identity, "
      "endpoint, api-key auth, all 7 tools)")
