"""HTTP wrapper around the nRev tables service.

Separate from `client.py` (workflow API) because:
- different base URL (`nrev-tables-service.public.prod.nurturev.com`)
- different surface (15 endpoints vs ~50)
- both share the same Supabase JWT — auth module reused

Verified live on prod 2026-05-25. See `docs/nrev_tables_api_investigation.md`
for the full API surface, known bugs, and design decisions.

Known platform behaviors codified here (all flagged with bug-id comments):
- BUG #6: `limit` param on GET /tables/{id}/rows accepts ONLY [100, 500,
  1000, 5000, 10000, 50000, 100000]. Anything else → HTTP 400. We clamp.
- BUG #1: `sortBy` on GET /tables is broken (no format accepted). We drop
  the param entirely from list_tables until platform ships a fix.
- BUG #7: `meta.total_entries: 0` when paging past end is misleading
  (should reflect total table size). Caller's problem; we surface meta as-is.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .auth import get_jwt


DEFAULT_HOST = os.environ.get(
    "NREV_TABLES_HOST",
    "https://nrev-tables-service.public.prod.nurturev.com",
)
TIMEOUT_SECONDS = float(os.environ.get("NREV_TABLES_TIMEOUT", "60"))

# Row-fetch limit must be one of these values. Validated live 2026-05-25.
_ALLOWED_LIMITS = (100, 500, 1000, 5000, 10000, 50000, 100000)


class TablesAPIError(RuntimeError):
    def __init__(self, status_code: int, body: str, url: str):
        super().__init__(f"HTTP {status_code} from {url}: {body[:500]}")
        self.status_code = status_code
        self.body = body
        self.url = url


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=DEFAULT_HOST,
        headers={
            "Authorization": f"Bearer {get_jwt()}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
    )


def request(
    method: str,
    path: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Any:
    with _client() as c:
        r = c.request(method, path, json=json_body, params=params)
        if r.status_code >= 400:
            raise TablesAPIError(r.status_code, r.text, str(r.request.url))
        if not r.content:
            return None
        try:
            return r.json()
        except Exception:
            return r.text


def _clamp_limit(limit: int) -> int:
    """Snap to the nearest allowed limit value (BUG #6 workaround).

    Round UP to the next allowed value so the caller gets AT LEAST `limit`
    rows. Maximum is 100,000 — bigger asks get clamped down.
    """
    li = int(limit)
    if li <= 0:
        return 100
    for allowed in _ALLOWED_LIMITS:
        if li <= allowed:
            return allowed
    return _ALLOWED_LIMITS[-1]


# ─── Tables ─────────────────────────────────────────────────────────────


def list_tables(name: Optional[str] = None,
                creators: Optional[list[str]] = None,
                skip: int = 0,
                limit: int = 100) -> dict:
    """GET /tables — paginated list of tables in this tenant."""
    params: dict = {"skip": max(0, int(skip)), "limit": int(limit)}
    if name:
        params["name"] = name
    if creators:
        # Repeating the param creates multiple values in the query string
        params["creators"] = creators
    return request("GET", "/tables", params=params)


def list_table_creators() -> list:
    """GET /tables/creators — distinct creators in this tenant.

    Returns: [{id, name, email}, ...]
    """
    return request("GET", "/tables/creators")


def get_table(table_id: str) -> dict:
    """GET /tables/{table_id} — full schema including columns."""
    return request("GET", f"/tables/{table_id}")


def create_table(name: str, columns: Optional[list[dict]] = None) -> dict:
    """POST /tables — create a new table with optional inline columns.

    `columns` items: {name: str, type: <enum>, position?: int}.
    Types: text | long_text | number | boolean | date | datetime | json.
    (row_id is system-reserved.)

    Every new table gets 3 auto-created system columns at positions 0/1/2:
    row_id, added_at (datetime), last_updated_at (datetime). User columns
    start at position 3.
    """
    body: dict = {}
    if name is not None:
        body["name"] = name
    if columns:
        body["columns"] = columns
    return request("POST", "/tables", json_body=body)


def rename_table(table_id: str, new_name: str) -> dict:
    """PATCH /tables/{table_id} — rename. Table id is stable; only the name
    changes. 5-64 char limit."""
    return request("PATCH", f"/tables/{table_id}", json_body={"name": new_name})


# ─── Columns ────────────────────────────────────────────────────────────


def add_column(table_id: str, name: str, col_type: str,
                position: Optional[int] = None) -> dict:
    """POST /tables/{table_id}/columns — append a new column."""
    body: dict = {"name": name, "type": col_type}
    if position is not None:
        body["position"] = int(position)
    return request("POST", f"/tables/{table_id}/columns", json_body=body)


def rename_column(table_id: str, column_id: str, new_name: str) -> dict:
    """PATCH /tables/{table_id}/columns/{column_id} — rename. Column id is
    stable; rename doesn't affect stored data or other columns referencing it
    (there are no FK-style refs in nrev tables)."""
    return request(
        "PATCH",
        f"/tables/{table_id}/columns/{column_id}",
        json_body={"name": new_name},
    )


def reorder_column(table_id: str, column_id: str, position: int) -> dict:
    """PUT /tables/{table_id}/columns/{column_id}/position — move a column
    to a new visual position. System columns (row_id, added_at,
    last_updated_at) can't be reordered — 400 on attempt."""
    return request(
        "PUT",
        f"/tables/{table_id}/columns/{column_id}/position",
        json_body={"position": int(position)},
    )


# ─── Rows ───────────────────────────────────────────────────────────────


def list_rows(table_id: str,
              skip: int = 0,
              limit: int = 100,
              sort_column_id: Optional[str] = None,
              sort_direction: str = "desc",
              search: Optional[str] = None,
              filter_column_id: Optional[str] = None,
              filter_operator: Optional[str] = None,
              filter_values: Optional[list] = None) -> dict:
    """GET /tables/{table_id}/rows — paginated row list with filter/sort/search.

    `limit` is auto-clamped to the nearest allowed value (BUG #6).
    `filter_values` is repeated as multiple `filter[value]` query params for
    multi-value operators like `in` and `not_in`.

    Filter operators: eq, neq, contains, gt, gte, lt, lte, is_empty,
    is_not_empty, in, not_in.

    Note: row values in the response are keyed by COLUMN UUID, not column
    name. Translate via the schema from get_table().
    """
    params: dict = {
        "skip": max(0, int(skip)),
        "limit": _clamp_limit(limit),
        "sort_direction": sort_direction,
    }
    if sort_column_id:
        params["sort_column_id"] = sort_column_id
    if search:
        params["search"] = search
    if filter_column_id and filter_operator:
        params["filter[column_id]"] = filter_column_id
        params["filter[operator]"] = filter_operator
        # is_empty / is_not_empty take no value
        if filter_values and filter_operator not in ("is_empty", "is_not_empty"):
            params["filter[value]"] = filter_values
    return request("GET", f"/tables/{table_id}/rows", params=params)


def add_row(table_id: str, values: dict) -> dict:
    """POST /tables/{table_id}/rows — insert one row.

    `values` MUST be keyed by COLUMN UUID, not column name. The MCP-side
    `add_row` tool translates names → UUIDs before calling this. Direct
    callers must resolve names themselves via get_table().
    """
    return request("POST", f"/tables/{table_id}/rows", json_body={"values": values})


def update_row(table_id: str, row_id: int, values: dict) -> dict:
    """PATCH /tables/{table_id}/rows/{row_id} — merge-update one row.

    Same UUID-keyed `values`. PATCH semantics: fields not in `values` keep
    their current values; passing `null` for a field DELETES that field.
    """
    return request(
        "PATCH",
        f"/tables/{table_id}/rows/{int(row_id)}",
        json_body={"values": values},
    )


# ─── DELETE (not yet shipped — will 405) ───────────────────────────────


def delete_table(table_id: str) -> dict:
    """DELETE /tables/{table_id} — NOT YET LIVE. Currently returns 405.
    Wrapper exists so the MCP tool surface is stable when platform ships
    DELETE endpoints (M1 milestone)."""
    return request("DELETE", f"/tables/{table_id}")


def delete_column(table_id: str, column_id: str) -> dict:
    """DELETE /tables/{table_id}/columns/{column_id} — NOT YET LIVE.
    Currently 405."""
    return request("DELETE", f"/tables/{table_id}/columns/{column_id}")


def delete_row(table_id: str, row_id: int) -> dict:
    """DELETE /tables/{table_id}/rows/{row_id} — hard-delete one row by ID.

    v0.2.32 — went live 2026-06-02. Returns HTTP 204 (empty body). The
    MCP wrapper uses the bulk-delete endpoint instead (functionally
    identical per the API docstring: "Delegates to bulk_delete_rows with a
    single-element list and discards the payload") so we get a structured
    response envelope back."""
    return request("DELETE", f"/tables/{table_id}/rows/{int(row_id)}")


# ═══════════════════════════════════════════════════════════════════════════
# M2 endpoints — went live 2026-06-02, verified by live probing
# ═══════════════════════════════════════════════════════════════════════════


def bulk_delete_rows(table_id: str, row_ids: list[int]) -> dict:
    """POST /tables/{table_id}/rows/bulk-delete — atomic delete of up to
    1000 rows. Missing ids inside row_ids are silently skipped.

    Returns: {deleted_row_ids: [...], table: {row_count, last_updated_at}}.
    """
    return request(
        "POST",
        f"/tables/{table_id}/rows/bulk-delete",
        json_body={"row_ids": [int(r) for r in row_ids]},
    )


def aggregate(
    table_id: str,
    measures: list[dict],
    group_by: Optional[list[dict]] = None,
    filter: Optional[list[dict]] = None,
    joins: Optional[list[dict]] = None,
    sort: Optional[list[dict]] = None,
    limit: Optional[int] = None,
    skip: Optional[int] = None,
) -> dict:
    """POST /tables/{table_id}/aggregate — server-side count / count_distinct
    / sum / avg / min / max with optional group_by and cross-table joins.

    Quirks confirmed live 2026-06-08:
      - measure dict: {"op": "sum|avg|...", "column_id": "...", "alias": "..."}
        (count without column_id is allowed)
      - group_by dict: {"column_id": "..."} OR {"table_id": "...", "column_id": "..."}
        for joined-table columns
      - filter clause: {"column_id": "...", "operator": "eq|...", "value": [...]}
        Note: "operator" (NOT "op"), and value MUST be a list even for scalar
        eq. Booleans must be lowercase strings ("true"/"false"). Different
        from list_rows filter shape.
      - joins[i]: {"type": "inner|left", "table_id": "...",
                    "on": {"base_column_id": "...", "joined_column_id": "..."}}
        Note: "on" is a single dict (single-column joins only).

    Returns: {groups: [{keys: {col_id: value}, measures: {alias: number}}],
              meta: {group_count, truncated}}.
    """
    body: dict = {"measures": measures}
    if group_by: body["group_by"] = group_by
    if filter: body["filter"] = filter
    if joins: body["joins"] = joins
    if sort: body["sort"] = sort
    if limit is not None: body["limit"] = int(limit)
    if skip is not None: body["skip"] = int(skip)
    return request("POST", f"/tables/{table_id}/aggregate", json_body=body)


def distinct_values(
    table_id: str,
    column_id: str,
    filter: Optional[list[dict]] = None,
    search: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """POST /tables/{table_id}/columns/{column_id}/distinct-values — unique
    values of a single column (optionally narrowed by filter / substring
    search). Drives the UI's filter-chip dropdowns.

    `filter` uses the aggregate-style clause shape (operator, value-as-list).
    `search` is case-insensitive substring on the value.
    """
    body: dict = {}
    if filter: body["filter"] = filter
    if search is not None: body["search"] = search
    if limit is not None: body["limit"] = int(limit)
    return request(
        "POST",
        f"/tables/{table_id}/columns/{column_id}/distinct-values",
        json_body=body,
    )


def join_tables(
    base_table_id: str,
    joins: list[dict],
    base_filter: Optional[list[dict]] = None,
    select: Optional[list[dict]] = None,
    sort: Optional[list[dict]] = None,
    limit: Optional[int] = None,
    skip: Optional[int] = None,
) -> dict:
    """POST /tables/{base_table_id}/join — multi-table inner/left join with
    optional projection.

    joins[i]: {"type": "inner|left", "table_id": "...",
               "on": {"base_column_id": "...", "joined_column_id": "..."}}
    select (optional): [{"table_id": "...", "column_id": "..."}, ...]
    base_filter: aggregate-style filter clauses (operator + list value).

    Rows come back PREFIX-KEYED: `base.<col_id>` for the driving table,
    `j0.<col_id>` / `j1.<col_id>` / etc. for joined tables in the order
    they appear in `joins`. The MCP tool layer name-resolves these for
    readability before returning.
    """
    body: dict = {"joins": joins}
    if base_filter: body["base_filter"] = base_filter
    if select: body["select"] = select
    if sort: body["sort"] = sort
    if limit is not None: body["limit"] = int(limit)
    if skip is not None: body["skip"] = int(skip)
    return request("POST", f"/tables/{base_table_id}/join", json_body=body)


# ─── batch-read — wrapper exists in case caller wants it, but no MCP tool ─

def batch_read(reads: list[dict]) -> dict:
    """POST /tables/batch-read — run 1-20 row reads in parallel. Per-entry
    partial success in results[i].ok. Not exposed as an MCP tool (no
    user-side need; the caller can already make 20 list_rows calls)."""
    return request("POST", "/tables/batch-read", json_body={"reads": reads})
