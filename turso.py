"""
Turso HTTP client — giao tiếp với Turso database qua HTTP Pipeline API.
Thay thế aiosqlite cho Vercel serverless deployment.
"""

import os
import httpx

TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")


def _api_url():
    url = TURSO_URL
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://")
    if not url.startswith("http"):
        url = f"https://{url}"
    return f"{url}/v2/pipeline"


def _convert_arg(val):
    if val is None:
        return {"type": "null"}
    if isinstance(val, bool):
        return {"type": "integer", "value": str(int(val))}
    if isinstance(val, int):
        return {"type": "integer", "value": str(val)}
    if isinstance(val, float):
        return {"type": "float", "value": val}
    return {"type": "text", "value": str(val)}


def _extract_value(cell):
    t = cell.get("type", "null")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    return cell.get("value", "")


def _parse_result(result):
    cols = [c["name"] for c in result.get("cols", [])]
    parsed_rows = []
    for row in result.get("rows", []):
        values = [_extract_value(cell) for cell in row]
        parsed_rows.append(dict(zip(cols, values)))
    raw_id = result.get("last_insert_rowid")
    last_id = int(raw_id) if raw_id is not None else None
    return {
        "cols": cols,
        "rows": parsed_rows,
        "affected_row_count": result.get("affected_row_count", 0),
        "last_insert_rowid": last_id,
    }


def _headers():
    return {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    }


async def execute(sql: str, args: list | None = None):
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [_convert_arg(a) for a in args]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _api_url(),
            headers=_headers(),
            json={"requests": [
                {"type": "execute", "stmt": stmt},
                {"type": "close"},
            ]},
        )
        data = resp.json()

    r = data["results"][0]
    if r["type"] == "error":
        raise Exception(r["error"].get("message", str(r["error"])))
    return _parse_result(r["response"]["result"])


async def execute_batch(statements: list[tuple[str, list | None]]):
    requests = []
    for sql, args in statements:
        stmt = {"sql": sql}
        if args:
            stmt["args"] = [_convert_arg(a) for a in args]
        requests.append({"type": "execute", "stmt": stmt})
    requests.append({"type": "close"})

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _api_url(),
            headers=_headers(),
            json={"requests": requests},
        )
        data = resp.json()

    results = []
    for r in data["results"]:
        if r["type"] == "error":
            raise Exception(r["error"].get("message", str(r["error"])))
        if r.get("response", {}).get("type") == "execute":
            results.append(_parse_result(r["response"]["result"]))
    return results
