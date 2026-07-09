"""
Firebird MCP Server
Provides MCP tools for interacting with Firebird databases via FastMCP.
"""

import os
import json
import hashlib
import logging
import datetime
import decimal
from typing import Any, Optional

import fdb
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("firebird-mcp")

# ── Config from environment ────────────────────────────────────────────────────
FB_HOST = os.getenv("FB_HOST", "localhost")
FB_PORT = int(os.getenv("FB_PORT", "3050"))
FB_DATABASE = os.getenv("FB_DATABASE", "")
FB_USER = os.getenv("FB_USER", "SYSDBA")
FB_PASSWORD = os.getenv("FB_PASSWORD", "masterkey")
FB_CHARSET = os.getenv("FB_CHARSET", "UTF8")
FB_ROLE = os.getenv("FB_ROLE", None)
# DDL is destructive/irreversible; require explicit opt-in. Set FB_ALLOW_DDL=0 to disable.
FB_ALLOW_DDL = os.getenv("FB_ALLOW_DDL", "1").strip().lower() in ("1", "true", "yes", "on")

mcp = FastMCP(
    name="firebird-mcp",
    instructions=(
        "Provides tools to query and inspect a Firebird database. "
        "Use execute_query for SELECT statements, execute_dml/execute_many for "
        "INSERT/UPDATE/DELETE, and execute_ddl for schema changes (CREATE/ALTER/DROP)."
    ),
)


def get_connection() -> fdb.Connection:
    """Open a new Firebird connection using environment config."""
    kwargs: dict[str, Any] = dict(
        host=FB_HOST,
        port=FB_PORT,
        database=FB_DATABASE,
        user=FB_USER,
        password=FB_PASSWORD,
        charset=FB_CHARSET,
    )
    if FB_ROLE:
        kwargs["role"] = FB_ROLE
    return fdb.connect(**kwargs)


def serialize(val: Any) -> Any:
    """Make Firebird-specific types JSON-serialisable."""
    if isinstance(val, (datetime.date, datetime.datetime, datetime.time)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


def cursor_to_json(cur: fdb.Cursor, max_rows: int = 500) -> str:
    if cur.description is None:
        return json.dumps([])
    cols = [col[0] for col in cur.description]
    rows = []
    for row in cur.fetchmany(max_rows):
        rows.append({k: serialize(v) for k, v in zip(cols, row)})
    return json.dumps(rows, ensure_ascii=False, indent=2)


# Statements that must never run through the DML tools (they belong to execute_ddl).
_DDL_KEYWORDS = ("DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE",
                 "RECREATE", "COMMENT", "SET", "DECLARE")
# DML tools accept only these as the leading verb.
_DML_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "EXECUTE")


def _leading_verb(sql: str) -> str:
    """Return the uppercased leading SQL verb, ignoring leading comments/whitespace."""
    stmt = sql.strip()
    # Strip leading SQL comments (-- line and /* block */) before inspecting the verb.
    while True:
        if stmt.startswith("--"):
            nl = stmt.find("\n")
            stmt = "" if nl == -1 else stmt[nl + 1:].lstrip()
        elif stmt.startswith("/*"):
            end = stmt.find("*/")
            stmt = "" if end == -1 else stmt[end + 2:].lstrip()
        else:
            break
    upper = stmt.upper()
    return upper.split(None, 1)[0] if upper else ""


def _check_dml_allowed(sql: str) -> Optional[str]:
    """Return a JSON error string if `sql` is not an allowed DML statement, else None."""
    verb = _leading_verb(sql)
    if verb in _DDL_KEYWORDS:
        return json.dumps({"error": f"DDL/permission statement '{verb}' is blocked here. "
                                    "Use execute_ddl for schema changes."})
    if verb not in _DML_KEYWORDS:
        return json.dumps({"error": f"Only DML statements ({', '.join(_DML_KEYWORDS)}) "
                                    f"are allowed here; got '{verb or '(empty)'}'."})
    return None


def _check_ddl_allowed(sql: str) -> Optional[str]:
    """Return a JSON error string if `sql` is not an allowed/enabled DDL statement, else None."""
    if not FB_ALLOW_DDL:
        return json.dumps({"error": "DDL execution is disabled. Set FB_ALLOW_DDL=1 to enable."})
    verb = _leading_verb(sql)
    if verb not in _DDL_KEYWORDS:
        return json.dumps({"error": f"Only DDL statements ({', '.join(_DDL_KEYWORDS)}) "
                                    f"are allowed here; got '{verb or '(empty)'}'. "
                                    "Use execute_dml for INSERT/UPDATE/DELETE."})
    return None


@mcp.tool(
    description="Return general info about the connected Firebird database (version, table/view/procedure counts, charset)."
)
def get_db_info() -> str:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE")
        version = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG=0 AND RDB$VIEW_BLR IS NULL")
        tables = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG=0 AND RDB$VIEW_BLR IS NOT NULL")
        views = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM RDB$PROCEDURES WHERE RDB$SYSTEM_FLAG=0")
        procs = cur.fetchone()[0]
        cur.execute("SELECT RDB$CHARACTER_SET_NAME FROM RDB$DATABASE")
        charset = (cur.fetchone()[0] or "").strip()
        conn.close()
        return json.dumps({
            "host": FB_HOST, "port": FB_PORT, "database": FB_DATABASE,
            "user": FB_USER, "charset": charset, "fb_version": version,
            "tables": tables, "views": views, "procedures": procs,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("get_db_info: %s", exc)
        return json.dumps({"error": str(exc)})


# ── Inspection helpers (cursor-based, so the digest can reuse one connection) ────
def _list_tables(cur: fdb.Cursor) -> list[dict]:
    cur.execute("""
        SELECT RDB$RELATION_NAME, RDB$OWNER_NAME, RDB$DESCRIPTION
        FROM RDB$RELATIONS
        WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NULL
        ORDER BY RDB$RELATION_NAME
    """)
    return [{"table_name": (r[0] or "").strip(),
             "owner": (r[1] or "").strip(),
             "description": (r[2] or "").strip()} for r in cur.fetchall()]


@mcp.tool(description="List all user tables in the Firebird database with owner and description.")
def list_tables() -> str:
    try:
        conn = get_connection()
        rows = _list_tables(conn.cursor())
        conn.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("list_tables: %s", exc)
        return json.dumps({"error": str(exc)})


def _describe_table(cur: fdb.Cursor, tbl: str) -> dict:
    sql_cols = """
               SELECT RF.RDB$FIELD_NAME,
                      CASE F.RDB$FIELD_TYPE
                          WHEN 7 THEN 'SMALLINT'
                          WHEN 8 THEN 'INTEGER'
                          WHEN 10 THEN 'FLOAT'
                          WHEN 12 THEN 'DATE'
                          WHEN 13 THEN 'TIME'
                          WHEN 14 THEN 'CHAR'
                          WHEN 16 THEN 'BIGINT'
                          WHEN 27 THEN 'DOUBLE PRECISION'
                          WHEN 35 THEN 'TIMESTAMP'
                          WHEN 37 THEN 'VARCHAR'
                          WHEN 261 THEN 'BLOB'
                          ELSE 'TYPE_' || F.RDB$FIELD_TYPE
                          END,
                      F.RDB$FIELD_LENGTH,
                      F.RDB$FIELD_PRECISION,
                      F.RDB$FIELD_SCALE,
                      RF.RDB$NULL_FLAG,
                      RF.RDB$DEFAULT_SOURCE,
                      RF.RDB$DESCRIPTION,
                      RF.RDB$FIELD_POSITION
               FROM RDB$RELATION_FIELDS RF
                        JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME = RF.RDB$FIELD_SOURCE
               WHERE RF.RDB$RELATION_NAME = ?
               ORDER BY RF.RDB$FIELD_POSITION \
               """
    sql_idx = """
              SELECT I.RDB$INDEX_NAME,
                     IK.RDB$FIELD_NAME,
                     I.RDB$UNIQUE_FLAG,
                     I.RDB$INDEX_TYPE
              FROM RDB$INDICES I
                       JOIN RDB$INDEX_SEGMENTS IK ON IK.RDB$INDEX_NAME = I.RDB$INDEX_NAME
              WHERE I.RDB$RELATION_NAME = ?
              ORDER BY I.RDB$INDEX_NAME, IK.RDB$FIELD_POSITION \
              """
    sql_pk = """
             SELECT IS2.RDB$FIELD_NAME
             FROM RDB$RELATION_CONSTRAINTS RC
                      JOIN RDB$INDEX_SEGMENTS IS2 ON IS2.RDB$INDEX_NAME = RC.RDB$INDEX_NAME
             WHERE RC.RDB$RELATION_NAME = ?
               AND RC.RDB$CONSTRAINT_TYPE = 'PRIMARY KEY' \
             """
    cur.execute(sql_cols, [tbl])
    columns = [{"position": r[8], "name": (r[0] or "").strip(), "type": r[1],
                "length": r[2], "precision": r[3], "scale": abs(r[4]) if r[4] else 0,
                "not_null": bool(r[5]), "default": (r[6] or "").strip() or None,
                "description": (r[7] or "").strip() or None}
               for r in cur.fetchall()]
    cur.execute(sql_idx, [tbl])
    indexes: dict = {}
    for r in cur.fetchall():
        n = (r[0] or "").strip()
        if n not in indexes:
            indexes[n] = {"name": n, "unique": bool(r[2]),
                          "type": "DESC" if r[3] else "ASC", "columns": []}
        indexes[n]["columns"].append((r[1] or "").strip())
    cur.execute(sql_pk, [tbl])
    pk = [(r[0] or "").strip() for r in cur.fetchall()]
    return {"table": tbl, "columns": columns,
            "primary_key": pk, "indexes": list(indexes.values())}


@mcp.tool(description="Return column definitions, primary key, and indexes for a specific table.")
def describe_table(table_name: str) -> str:
    """Args: table_name — name of the table (case-insensitive)."""
    try:
        conn = get_connection()
        result = _describe_table(conn.cursor(), table_name.upper().strip())
        conn.close()
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("describe_table: %s", exc)
        return json.dumps({"error": str(exc)})


def _list_views(cur: fdb.Cursor) -> list[dict]:
    cur.execute("""
        SELECT RDB$RELATION_NAME, RDB$VIEW_SOURCE, RDB$DESCRIPTION
        FROM RDB$RELATIONS
        WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NOT NULL
        ORDER BY RDB$RELATION_NAME
    """)
    return [{"name": (r[0] or "").strip(),
             "source": (r[1] or "").strip(),
             "description": (r[2] or "").strip()} for r in cur.fetchall()]


@mcp.tool(description="List all views in the Firebird database with their SQL source.")
def list_views() -> str:
    try:
        conn = get_connection()
        rows = _list_views(conn.cursor())
        conn.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("list_views: %s", exc)
        return json.dumps({"error": str(exc)})


def _list_procedures(cur: fdb.Cursor) -> list[dict]:
    cur.execute("""
        SELECT RDB$PROCEDURE_NAME, RDB$PROCEDURE_INPUTS,
               RDB$PROCEDURE_OUTPUTS, RDB$DESCRIPTION
        FROM RDB$PROCEDURES
        WHERE RDB$SYSTEM_FLAG = 0
        ORDER BY RDB$PROCEDURE_NAME
    """)
    return [{"name": (r[0] or "").strip(), "input_params": r[1] or 0,
             "output_params": r[2] or 0, "description": (r[3] or "").strip()}
            for r in cur.fetchall()]


@mcp.tool(description="List all stored procedures in the Firebird database.")
def list_procedures() -> str:
    try:
        conn = get_connection()
        rows = _list_procedures(conn.cursor())
        conn.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("list_procedures: %s", exc)
        return json.dumps({"error": str(exc)})


def _get_procedure_source(cur: fdb.Cursor, name: str) -> dict:
    sql_src = "SELECT RDB$PROCEDURE_SOURCE FROM RDB$PROCEDURES WHERE RDB$PROCEDURE_NAME=?"
    sql_params = """
                 SELECT PP.RDB$PARAMETER_NAME,
                        PP.RDB$PARAMETER_TYPE,
                        CASE F.RDB$FIELD_TYPE
                            WHEN 7 THEN 'SMALLINT'
                            WHEN 8 THEN 'INTEGER'
                            WHEN 14 THEN 'CHAR'
                            WHEN 16 THEN 'BIGINT'
                            WHEN 27 THEN 'DOUBLE PRECISION'
                            WHEN 35 THEN 'TIMESTAMP'
                            WHEN 37 THEN 'VARCHAR'
                            WHEN 261 THEN 'BLOB'
                            ELSE 'TYPE_' || F.RDB$FIELD_TYPE
                            END,
                        F.RDB$FIELD_LENGTH,
                        PP.RDB$PARAMETER_NUMBER
                 FROM RDB$PROCEDURE_PARAMETERS PP
                          JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME = PP.RDB$FIELD_SOURCE
                 WHERE PP.RDB$PROCEDURE_NAME = ?
                 ORDER BY PP.RDB$PARAMETER_TYPE, PP.RDB$PARAMETER_NUMBER \
                 """
    cur.execute(sql_src, [name])
    row = cur.fetchone()
    source = (row[0] or "").strip() if row else ""
    cur.execute(sql_params, [name])
    inputs, outputs = [], []
    for r in cur.fetchall():
        p = {"name": (r[0] or "").strip(), "type": r[2], "length": r[3]}
        (inputs if r[1] == 0 else outputs).append(p)
    return {"procedure": name, "source": source, "inputs": inputs, "outputs": outputs}


@mcp.tool(description="Return the source code and parameter definitions of a stored procedure.")
def get_procedure_source(procedure_name: str) -> str:
    """Args: procedure_name — name of the procedure (case-insensitive)."""
    try:
        conn = get_connection()
        result = _get_procedure_source(conn.cursor(), procedure_name.upper().strip())
        conn.close()
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("get_procedure_source: %s", exc)
        return json.dumps({"error": str(exc)})


def _schema_fingerprint(cur: fdb.Cursor) -> str:
    """Short hash of schema metadata. Computed in Python so it works on FB 2.5–5.0
    (no dependency on LIST()/HASH() built-ins). RDB$FORMAT bumps on every ALTER TABLE,
    so combined with object names it changes whenever the schema does."""
    cur.execute("""
        SELECT TRIM(RDB$RELATION_NAME), COALESCE(RDB$FORMAT, 0)
        FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG = 0
        ORDER BY RDB$RELATION_NAME
    """)
    rels = "|".join(f"{r[0]}:{r[1]}" for r in cur.fetchall())
    cur.execute("""
        SELECT TRIM(RDB$PROCEDURE_NAME)
        FROM RDB$PROCEDURES WHERE RDB$SYSTEM_FLAG = 0
        ORDER BY RDB$PROCEDURE_NAME
    """)
    procs = "|".join((r[0] or "") for r in cur.fetchall())
    return hashlib.sha256(f"{rels}#{procs}".encode("utf-8")).hexdigest()[:16]


@mcp.tool(description=(
    "Return a short fingerprint (hash) of the database schema in one cheap query. "
    "Cache it, then compare on the next session: if it is unchanged, a previously "
    "saved get_schema_digest output is still valid and the DB need not be re-read."
))
def get_schema_fingerprint() -> str:
    try:
        conn = get_connection()
        fp = _schema_fingerprint(conn.cursor())
        conn.close()
        return json.dumps({"fingerprint": fp})
    except Exception as exc:
        logger.error("get_schema_fingerprint: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.tool(description=(
    "Return the full database schema in a single call: every table with columns, "
    "primary key and indexes, all views, and procedure signatures. Includes a "
    "'fingerprint' field. Intended to be fetched once, saved to the project "
    "(e.g. a committed db_schema.json / CLAUDE.md), and reused across sessions; "
    "re-fetch only when get_schema_fingerprint changes. Set include_procedure_source "
    "to also embed full stored-procedure bodies (larger output)."
))
def get_schema_digest(include_procedure_source: bool = False) -> str:
    """Args: include_procedure_source — embed full procedure bodies (default False)."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        tables = _list_tables(cur)
        procedures = _list_procedures(cur)
        schema: dict[str, Any] = {
            "tables": [_describe_table(cur, t["table_name"]) for t in tables],
            "views": _list_views(cur),
            "procedures": procedures,
        }
        if include_procedure_source:
            schema["procedure_source"] = [
                _get_procedure_source(cur, p["name"]) for p in procedures
            ]
        schema["fingerprint"] = _schema_fingerprint(cur)
        conn.close()
        return json.dumps(schema, ensure_ascii=False, indent=2)
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        logger.error("get_schema_digest: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.tool(description=(
    "Execute a SELECT (or any read) SQL query. "
    "Use ? as positional placeholders. Returns up to max_rows rows as JSON."
))
def execute_query(sql: str, params: Optional[list] = None, max_rows: int = 500) -> str:
    """Args: sql — SELECT statement; params — list of values for ? placeholders; max_rows — row limit."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or [])
        result = cursor_to_json(cur, max_rows)
        conn.close()
        return result
    except Exception as exc:
        logger.error("execute_query: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.tool(description=(
    "Execute a single INSERT, UPDATE, or DELETE statement in its own transaction "
    "(COMMIT on success, ROLLBACK on error). Use ? as positional placeholders. "
    "If the statement has a RETURNING clause, the returned row(s) are included in the result. "
    "DDL and permission statements (DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE) are blocked."
))
def execute_dml(sql: str, params: Optional[list] = None) -> str:
    """Args: sql — DML statement; params — list of values for ? placeholders."""
    blocked = _check_dml_allowed(sql)
    if blocked:
        return blocked
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or [])
        affected = cur.rowcount
        # INSERT/UPDATE/DELETE ... RETURNING produces a result set.
        returning = None
        if cur.description is not None:
            cols = [c[0] for c in cur.description]
            returning = [{k: serialize(v) for k, v in zip(cols, r)}
                         for r in cur.fetchall()]
        conn.commit()
        result: dict[str, Any] = {"status": "ok", "rows_affected": affected}
        if returning is not None:
            result["returning"] = returning
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("execute_dml: %s", exc)
        return json.dumps({"error": str(exc)})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@mcp.tool(description=(
    "Execute the same INSERT/UPDATE/DELETE statement once for each set of parameters "
    "in a single transaction (all-or-nothing: COMMIT on success, ROLLBACK if any row fails). "
    "Use ? as positional placeholders. `params_seq` is a list of parameter lists, "
    "e.g. [[1, 'a'], [2, 'b']]. DDL and permission statements are blocked."
))
def execute_many(sql: str, params_seq: list[list]) -> str:
    """Args: sql — DML statement; params_seq — list of parameter lists, one per row."""
    blocked = _check_dml_allowed(sql)
    if blocked:
        return blocked
    if not isinstance(params_seq, list) or not params_seq:
        return json.dumps({"error": "params_seq must be a non-empty list of parameter lists."})
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.executemany(sql, params_seq)
        affected = cur.rowcount
        conn.commit()
        return json.dumps({"status": "ok", "batches": len(params_seq),
                           "rows_affected": affected}, ensure_ascii=False, indent=2)
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("execute_many: %s", exc)
        return json.dumps({"error": str(exc)})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@mcp.tool(description=(
    "Execute a single DDL / schema statement (CREATE, ALTER, DROP, RECREATE, "
    "CREATE OR ALTER, COMMENT, GRANT, REVOKE, SET GENERATOR, DECLARE) in its own "
    "transaction (COMMIT on success, ROLLBACK on error). WARNING: schema changes "
    "such as DROP are irreversible. Requires FB_ALLOW_DDL to be enabled."
))
def execute_ddl(sql: str) -> str:
    """Args: sql — a single DDL statement (no positional parameters)."""
    blocked = _check_ddl_allowed(sql)
    if blocked:
        return blocked
    verb = _leading_verb(sql)
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        logger.info("execute_ddl: %s ... committed", verb)
        return json.dumps({"status": "ok", "statement": verb}, ensure_ascii=False)
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("execute_ddl: %s", exc)
        return json.dumps({"error": str(exc)})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting Firebird MCP → %s:%s/%s", FB_HOST, FB_PORT, FB_DATABASE)
    mcp.run(transport="stdio")
