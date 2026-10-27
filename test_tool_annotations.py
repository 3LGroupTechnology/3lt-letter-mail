"""MCP tool annotation tests (metadata only, no network, no tool calls).

Every tool must declare an explicit title and the complete annotation set
with values derived from actual behavior in core.py — the map below IS the
spec; assert the whole table, not samples:

  draft_letter      creates a draft only; verifies the address via PostGrid
                    (external) -> openWorld. New draft_id every call.
  request_send      can mail a letter + capture a Stripe charge, and may
                    auto-send on on_command keys -> destructive. Every call
                    inserts a NEW send request -> not idempotent.
  send_status       a read in spirit, but while a request is PENDING it
                    mints a fresh single-use approval token
                    (core.ensure_approval_link) — a real write, so
                    readOnlyHint stays False deliberately.
  create_fax_draft  stores a draft; normalization, page count, screening
                    and pricing are all local -> openWorld False.
  approve_fax       records the approval gate, transmits nothing; a repeat
                    call returns 409 and changes nothing -> idempotent.
  send_fax          transmits via Phaxio + captures the charge ->
                    destructive/openWorld. Idempotent is code-backed: the
                    lifecycle sends exactly once, a retry returns 409, and
                    provider/billing calls carry key 3lt-fax-{fax_id}.
  get_fax_status    the only pure read -> the only readOnlyHint=True; for
                    SENT faxes it fetches live Phaxio status -> openWorld.

send_fax exactly-once behavior itself is covered by test_fax.py check #4
(double send -> 409, exactly one transmission; see also check #3, double
approve -> 409) — cited here per spec, not duplicated.
"""
import asyncio
import os
import sys
import tempfile

DB = tempfile.mktemp(suffix=".db")
os.environ["LMS_DB_PATH"] = DB
os.environ["LMS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["LMS_UPLOADS_DIR"] = tempfile.mkdtemp(prefix="tlt_anno_uploads_")
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
os.environ["APPROVAL_TOKEN_SECRET"] = "test-approval-secret"
os.environ["PHAXIO_API_KEY"] = "test_phaxio_key"
os.environ["PHAXIO_API_SECRET"] = "test_phaxio_secret"
os.environ.pop("ALLOW_LIVE_FAX", None)

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(os.path.dirname(SERVICE_DIR), "3lt-shared")
sys.path.insert(0, SERVICE_DIR)
sys.path.insert(0, SHARED_DIR)

import mcp_server  # noqa: E402
from fastmcp import Client  # noqa: E402

# name -> (title, readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
EXPECTED = {
    "draft_letter": ("Draft a Letter", False, False, False, True),
    "request_send": ("Request Letter Send", False, True, False, True),
    "send_status": ("Check Letter Send Status", False, False, False, False),
    "create_fax_draft": ("Draft a Fax", False, False, False, False),
    "approve_fax": ("Approve a Fax", False, False, True, False),
    "send_fax": ("Send a Fax", False, True, True, True),
    "get_fax_status": ("Check Fax Status", True, False, True, True),
}

HINT_FIELDS = ("readOnlyHint", "destructiveHint", "idempotentHint",
               "openWorldHint")


async def fetch_tools():
    async with Client(mcp_server.mcp) as client:
        return await client.list_tools()


def main():
    tools = asyncio.run(fetch_tools())
    by_name = {t.name: t for t in tools}

    # 1. Exactly the 7 expected tools, no more, no fewer.
    assert set(by_name) == set(EXPECTED), set(by_name) ^ set(EXPECTED)
    print("1. tools/list returns exactly 7 tools ✔")

    # 2. Every tool carries a non-null title and a complete annotations
    #    object — no annotation field is null on any tool.
    for name, tool in by_name.items():
        assert tool.title, f"{name}: title is null/empty"
        assert tool.annotations is not None, f"{name}: annotations missing"
        dumped = tool.annotations.model_dump(mode="json", by_alias=True)
        for field in HINT_FIELDS:
            assert dumped.get(field) is not None, \
                f"{name}: annotation field {field} is null"
    print("2. every tool: non-null title + complete annotations, no nulls ✔")

    # 3. Whole-map equality: each tool's title + four hints equal the
    #    behavior-derived table exactly.
    for name, (title, ro, d, i, ow) in EXPECTED.items():
        tool = by_name[name]
        assert tool.title == title, f"{name}: title {tool.title!r} != {title!r}"
        ann = tool.annotations
        actual = (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint,
                  ann.open_world_hint)
        assert actual == (ro, d, i, ow), \
            f"{name}: hints {actual} != {(ro, d, i, ow)}"
    print("3. whole annotation map matches the spec table exactly ✔")

    # 4. The pair directory review flagged: send_fax and request_send must
    #    declare destructiveHint=True and openWorldHint=True.
    for name in ("send_fax", "request_send"):
        ann = by_name[name].annotations
        assert ann.destructive_hint is True, f"{name}: destructiveHint"
        assert ann.open_world_hint is True, f"{name}: openWorldHint"
    print("4. send_fax + request_send: destructive + openWorld declared ✔")

    # 5. get_fax_status is the ONLY tool with readOnlyHint=True.
    readonly = [n for n, t in by_name.items()
                if t.annotations.read_only_hint is True]
    assert readonly == ["get_fax_status"], readonly
    print("5. get_fax_status is the only readOnlyHint=True tool ✔")

    # 6. send_fax exactly-once semantics are behavior, not metadata — they
    #    are covered by test_fax.py checks #3/#4 (double approve -> 409;
    #    double send -> 409 with exactly one transmission). This suite
    #    asserts the metadata that describes that behavior:
    assert by_name["send_fax"].annotations.idempotent_hint is True
    assert by_name["approve_fax"].annotations.idempotent_hint is True
    print("6. send_fax/approve_fax idempotent hints match the 409-on-retry "
          "behavior proven in test_fax.py #3/#4 ✔")

    print("Tool annotation tests passed ✔ (7 tools, titles + full hint map)")


main()
