from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from . import domain
from .schema_kb import SchemaKB

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|exec|execute|merge|grant|revoke|openrowset|"
    r"opendatasource|openquery|waitfor|shutdown|backup|restore|bulk|xp_\w+|sp_\w+)\b", re.I)
_DATEPARTS = set("year yy yyyy quarter qq q month mm m dayofyear dy y day dd d week wk ww weekday dw hour hh "
                 "minute mi n second ss s millisecond ms".split())
_BAD_NODES = tuple(
    getattr(exp, n) for n in ("Insert", "Update", "Delete", "Create", "Drop", "Alter", "AlterTable", "Command",
                              "Merge", "Into", "TruncateTable", "Execute") if hasattr(exp, n))


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    error: str = ""
    tables: list[str] = field(default_factory=list)


def _fail(msg: str) -> GuardResult:
    return GuardResult(False, error=msg)


def _strip_literals(sql: str) -> str:
    return re.sub(r"N?'(?:[^']|'')*'", "''", sql, flags=re.I)


def _conjuncts(node):
    if node is None:
        return
    if isinstance(node, exp.And):
        yield from _conjuncts(node.left)
        yield from _conjuncts(node.right)
    elif isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    else:
        yield node


def _scope_has_user_filter(select: exp.Select, alias: str, cols: set[str], ids: set[str], single_table: bool) -> bool:
    where = select.args.get("where")
    if not where:
        return False
    for c in _conjuncts(where.this):
        if not isinstance(c, exp.EQ):
            continue
        for a, b in ((c.left, c.right), (c.right, c.left)):
            if isinstance(a, exp.Column) and a.name.lower() in cols and isinstance(b, (exp.Literal, exp.National)):
                qual = a.table.lower()
                if (qual == alias or (not qual and single_table)) and str(b.this) in ids:
                    return True
    return False


def validate(sql: str, kb: SchemaKB, *, role: str = "admin", user_id: str = "", max_rows: int = 200,
             member_ids: list[str] | None = None) -> GuardResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return _fail("Empty SQL.")
    bare = _strip_literals(sql)
    if ";" in bare or "--" in bare or "/*" in bare:
        return _fail("Only ONE statement without comments is allowed (remove ';', '--', '/* */').")
    if _FORBIDDEN.search(bare):
        return _fail("Only read-only SELECT queries are allowed.")

    try:
        parsed = [p for p in sqlglot.parse(sql, read="tsql") if p is not None]
    except sqlglot.errors.ParseError as e:
        return _fail(f"SQL syntax error (T-SQL): {str(e)[:300]}")
    if len(parsed) != 1:
        return _fail("Exactly one SELECT statement is required.")
    tree = parsed[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        return _fail("Query must be a SELECT.")
    if any(isinstance(n, _BAD_NODES) for n in tree.walk()):
        return _fail("Only read-only SELECT queries are allowed (no INTO / DML / DDL).")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    derived = {s.alias.lower() for s in tree.find_all(exp.Subquery) if s.alias}
    has_derived = bool(cte_names or derived)

    alias_map: dict[str, str] = {}
    used_tables: list[str] = []
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in cte_names:
            continue
        if t.catalog or (t.db and t.db.lower() != "dbo"):
            return _fail(f"Cross-database/schema access is not allowed: {t.sql()}")
        if not kb.has_table(name):
            close = ", ".join(sorted(kb.tables)[:60])
            return _fail(f"Unknown table '{t.name}'. Allowed tables: {close}")
        alias_map[(t.alias or t.name).lower()] = name
        if name not in used_tables:
            used_tables.append(name)
    if not used_tables:
        return _fail("Query must read from at least one table.")

    # ---------------- column checks
    proj_aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    tables_cols = {t: kb.columns_of(t) for t in used_tables}
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            return _fail("Do not use SELECT *; list the needed columns explicitly.")
    for col in tree.find_all(exp.Column):
        name, qual = col.name.lower(), col.table.lower()
        if name in domain.BLOCKED_COLUMNS:
            return _fail(f"Column '{col.name}' is restricted and cannot be queried.")
        if qual:
            base = alias_map.get(qual)
            if base is None:
                if qual in cte_names or qual in derived:
                    continue
                return _fail(f"Unknown table alias '{col.table}'.")
            if name not in tables_cols[base]:
                sample = ", ".join(sorted(tables_cols[base])[:45])
                return _fail(f"Column '{col.name}' does not exist in table {base}. Columns of {base}: {sample}")
        else:
            if name in proj_aliases or name in _DATEPARTS or has_derived:
                continue
            owners = [t for t, cols in tables_cols.items() if name in cols]
            if not owners:
                return _fail(f"Column '{col.name}' does not exist in any table used in the query ({', '.join(used_tables)}).")
            if len(owners) > 1:
                return _fail(f"Column '{col.name}' is ambiguous (exists in {', '.join(owners)}); qualify it with a table alias.")

    # ---------------- row-level security
    if role == "member":
        ids = {str(x) for x in (member_ids or []) if x} | ({str(user_id)} if user_id else set())
        if not ids:
            return _fail("Member queries require a user id.")
        for t in used_tables:
            if t not in domain.MEMBER_ALLOWED_TABLES:
                return _fail(f"Table '{t}' is not accessible for member role.")
        for t in tree.find_all(exp.Table):
            base = t.name.lower()
            cols = domain.USER_SCOPED_TABLES.get(base)
            if not cols:
                continue
            scope = t.find_ancestor(exp.Select)
            alias = (t.alias or t.name).lower()
            n_tables = len([x for x in scope.find_all(exp.Table)]) if scope else 1
            if scope is None or not _scope_has_user_filter(scope, alias, cols, ids, n_tables == 1):
                col = sorted(cols)[0]
                return _fail(
                    f"Row-level security: the WHERE clause (AND-ed at top level) must contain {t.alias or t.name}.{col} = '{sorted(ids)[0]}'.")

    # ---------------- row cap
    final = sql
    if isinstance(tree, exp.Select):
        lim = tree.args.get("limit")
        cap_needed = lim is None
        if lim is not None:
            try:
                cap_needed = int(lim.expression.name) > max_rows
            except Exception:  # noqa: BLE001
                cap_needed = True
        if cap_needed:
            tree.set("limit", None)
            final = tree.limit(max_rows).sql(dialect="tsql")
    else:
        final = exp.select("*").from_(tree.subquery("q")).limit(max_rows).sql(dialect="tsql")

    return GuardResult(True, sql=final, tables=used_tables)
