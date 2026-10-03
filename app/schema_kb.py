from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import domain
from .config import settings

log = logging.getLogger(__name__)

_STOP = set(
    "a an the of in on at to for from by with and or is are was were be been do does did me my i we you your "
    "show list give get tell find what which who whom how many much all any some there this that these those "
    "please can could would should has have had it its as into about between than then also only "
    "kya hai hain ka ki ke ko se me mein par aur ya kaun kitne kitni kitna dikhao batao bataiye de do".split()
)


def tokenize(text: str) -> list[str]:
    text = domain.apply_glossary(text.lower())
    return [t for t in re.findall(r"[a-z0-9_]+", text) if t not in _STOP and len(t) > 1]


@dataclass
class Column:
    name: str
    dtype: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    is_pk: bool = False
    from_excel: bool = True


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)  # lower -> Column
    pk: list[str] = field(default_factory=list)  # lower names


@dataclass(frozen=True)
class Edge:
    parent: str
    parent_col: str
    child: str
    child_col: str
    rel: str
    desc: str


def _clean(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else re.sub(r"\s+", " ", str(v)).strip()


def _table_name(v) -> str:
    # Excel has dirty cells such as "MENU_DETAIL1 MENU_DETAIL1 " -> keep first token
    s = _clean(v)
    return s.split(" ")[0] if s else ""


class SchemaKB:
    def __init__(self) -> None:
        self.tables: dict[str, Table] = {}
        self.edges: list[Edge] = []
        self.value_profile: dict[str, list[str]] = {}
        self._adj: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, catalog: dict[str, dict[str, str]] | None = None, profile: bool = True) -> "SchemaKB":
        kb = cls()
        d = settings.data_dir
        kb._load_columns(d / "table_schema.xlsx")
        kb._load_pks(d / "Primary_keys.xlsx")
        kb._load_edges(d / "Relationship.xlsx")
        kb._add_views()
        if catalog:
            kb._merge_catalog(catalog)
        kb._apply_overrides()
        kb._build_graph()
        if profile:
            kb._load_profile(from_db=bool(catalog))
        log.info("SchemaKB ready: %d tables, %d edges", len(kb.tables), len(kb.edges))
        return kb

    def _load_columns(self, path: Path) -> None:
        df = pd.read_excel(path, dtype=str).fillna("")
        for _, r in df.iterrows():
            t, c = _table_name(r.iloc[0]), _clean(r.iloc[1])
            if not t or not c:
                continue
            tbl = self.tables.setdefault(t.lower(), Table(name=t))
            syn = [s.strip().lower() for s in re.split(r"[,;]", _clean(r.iloc[4])) if s.strip()]
            col = Column(name=c, description=_clean(r.iloc[3]), synonyms=syn)
            # 'Type' column of the sheet says PRIMARY/FIELD/Foreign – not trusted for PK (phone1 is wrongly PRIMARY)
            tbl.columns[c.lower()] = col

    def _add_views(self) -> None:
        for view, cols in domain.VIEWS.items():
            tbl = self.tables.setdefault(view.lower(), Table(name=view))
            for c, desc in cols.items():
                tbl.columns[c.lower()] = Column(name=c, description=desc)

    def _load_pks(self, path: Path) -> None:
        df = pd.read_excel(path, dtype=str).fillna("")
        for _, r in df.iterrows():
            t = _table_name(r.iloc[0])
            tbl = self.tables.get(t.lower())
            if not tbl:
                continue
            tbl.pk = [p.strip().lower() for p in _clean(r.iloc[1]).split(",") if p.strip()]
            for p in tbl.pk:
                if p not in tbl.columns:  # PK column missing in the description sheet (e.g. InstId)
                    tbl.columns[p] = Column(name=p if p != "instid" else "InstId", from_excel=False)
                tbl.columns[p].is_pk = True

    def _load_edges(self, path: Path) -> None:
        df = pd.read_excel(path, dtype=str).fillna("")
        seen = set()
        for _, r in df.iterrows():
            p, pc, c, cc = _table_name(r.iloc[0]), _clean(r.iloc[1]), _table_name(r.iloc[2]), _clean(r.iloc[3])
            key = (p.lower(), pc.lower(), c.lower(), cc.lower())
            if key in seen or not (p and c):
                continue
            seen.add(key)
            if p.lower() not in self.tables or c.lower() not in self.tables:
                log.debug("edge dropped (table not in schema sheet): %s -> %s", p, c)
                continue
            self.edges.append(Edge(p, pc, c, cc, _clean(r.iloc[4]), _clean(r.iloc[5])))

    def _merge_catalog(self, catalog: dict[str, dict[str, str]]) -> None:
        for tl, tbl in list(self.tables.items()):
            live = catalog.get(tl)
            if live is None:
                log.warning("table %s is in Excel but NOT in the database – removed", tbl.name)
                del self.tables[tl]
                continue
            for cl in list(tbl.columns):
                if cl not in live:
                    log.warning("column %s.%s in Excel but not in DB – removed", tbl.name, tbl.columns[cl].name)
                    del tbl.columns[cl]
                else:
                    tbl.columns[cl].dtype = live[cl]
            for cl, dtype in live.items():  # DB columns missing from Excel
                if cl not in tbl.columns:
                    tbl.columns[cl] = Column(name=cl, dtype=dtype, from_excel=False, is_pk=cl in tbl.pk)
        self.edges = [
            e for e in self.edges
            if e.parent.lower() in self.tables and e.child.lower() in self.tables
            and e.parent_col.lower() in self.tables[e.parent.lower()].columns
            and e.child_col.lower() in self.tables[e.child.lower()].columns
        ]

    def _apply_overrides(self) -> None:
        for (t, c), desc in domain.COLUMN_OVERRIDES.items():
            tbl = self.tables.get(t.lower())
            if tbl and c.lower() in tbl.columns:
                tbl.columns[c.lower()].description = desc

    def _build_graph(self) -> None:
        self._adj = {t: set() for t in self.tables}
        for e in self.edges:
            self._adj[e.parent.lower()].add(e.child.lower())
            self._adj[e.child.lower()].add(e.parent.lower())

    def _load_profile(self, from_db: bool) -> None:
        cache = settings.data_dir / "value_profile.json"
        if from_db:
            from .db import fetch_distinct
            for t, c in domain.PROFILE_TARGETS:
                tbl = self.tables.get(t.lower())
                if not tbl or c.lower() not in tbl.columns:
                    continue
                try:
                    vals = fetch_distinct(tbl.name, tbl.columns[c.lower()].name)
                    if vals:
                        self.value_profile[f"{t.lower()}.{c.lower()}"] = vals
                except Exception as e:  # noqa: BLE001
                    log.warning("profile %s.%s failed: %s", t, c, e)
            if self.value_profile:
                cache.write_text(json.dumps(self.value_profile, ensure_ascii=False, indent=1), encoding="utf-8")
        elif cache.exists():
            self.value_profile = json.loads(cache.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- lookups
    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables

    def columns_of(self, table: str) -> set[str]:
        t = self.tables.get(table.lower())
        return set(t.columns) if t else set()

    def is_blocked(self, table: str, col: str) -> bool:
        return col.lower() in domain.BLOCKED_COLUMNS

    # ------------------------------------------------------------ retrieval
    def score_tables(self, question: str, extra_tables: list[str] | None = None) -> list[tuple[str, float]]:
        toks = set(tokenize(question))
        scores: dict[str, float] = {}
        for tl, tbl in self.tables.items():
            s = 0.0
            name_toks = set(re.findall(r"[a-z]+", tl))
            s += 3.0 * len(toks & name_toks)
            for cl, col in tbl.columns.items():
                if cl in domain.BLOCKED_COLUMNS:
                    continue
                ctoks = set(re.findall(r"[a-z]+", cl))
                syn_toks = {w for sy in col.synonyms for w in re.findall(r"[a-z]+", sy)}
                desc_toks = set(re.findall(r"[a-z]+", col.description.lower()))
                s += 1.5 * len(toks & ctoks) + 1.0 * len(toks & syn_toks) + 0.25 * len(toks & desc_toks)
            scores[tl] = s
        for tl, boost in domain.entity_table_boosts(question).items():
            if tl in scores:
                scores[tl] += boost
        for t in extra_tables or []:
            if t.lower() in scores:
                scores[t.lower()] += 3.0
        return sorted(scores.items(), key=lambda kv: -kv[1])

    def select_tables(self, question: str, extra_tables: list[str] | None = None, k: int = 4, cap: int = 7) -> list[str]:
        ranked = [(t, s) for t, s in self.score_tables(question, extra_tables) if s > 0][:k]
        chosen = [t for t, _ in ranked]
        # connect chosen tables through bridge tables (shortest path <= 3 hops)
        for i in range(len(chosen)):
            for j in range(i + 1, len(chosen)):
                for hop in self._path(chosen[i], chosen[j]):
                    if hop not in chosen and len(chosen) < cap:
                        chosen.append(hop)
        return chosen[:cap]

    def _path(self, a: str, b: str, max_len: int = 3) -> list[str]:
        if a == b:
            return []
        q, prev = deque([a]), {a: None}
        while q:
            cur = q.popleft()
            if cur == b:
                break
            if self._depth(prev, cur) >= max_len:
                continue
            for n in self._adj.get(cur, ()):
                if n not in prev:
                    prev[n] = cur
                    q.append(n)
        if b not in prev:
            return []
        path, cur = [], prev[b]
        while cur and cur != a:
            path.append(cur)
            cur = prev[cur]
        return path[::-1]

    @staticmethod
    def _depth(prev: dict, node: str) -> int:
        d = 0
        while prev[node] is not None:
            node = prev[node]
            d += 1
        return d

    # --------------------------------------------------------------- render
    def join_hints(self, tables: list[str]) -> list[str]:
        sel = set(tables)
        out = []
        for e in self.edges:
            p, c = e.parent.lower(), e.child.lower()
            if p in sel and c in sel:
                cond = f"{e.parent}.{e.parent_col} = {e.child}.{e.child_col}"
                pt, ct = self.tables[p], self.tables[c]
                if "instid" in pt.columns and "instid" in ct.columns and ("instid" in pt.pk or "instid" in ct.pk):
                    cond += f" AND {e.parent}.InstId = {e.child}.InstId"
                out.append(f"{cond}   -- {e.rel}: {e.desc}")
        return out

    def render_schema(self, question: str, tables: list[str], max_cols_big: int = 26) -> str:
        toks = set(tokenize(question))
        lines: list[str] = []
        for tl in tables:
            tbl = self.tables[tl]
            note = domain.TABLE_NOTES.get(tl, "")
            pk = ", ".join(tbl.pk) if tbl.pk else "-"
            lines.append(f"TABLE {tbl.name}  (PK: {pk})" + (f"  -- {note}" if note else ""))
            cols = [(cl, c) for cl, c in tbl.columns.items() if cl not in domain.BLOCKED_COLUMNS]
            keep = cols
            if len(cols) > 20:
                must = domain.IMPORTANT_COLUMNS.get(tl, set())
                fk_cols = {e.child_col.lower() for e in self.edges if e.child.lower() == tl} | {
                    e.parent_col.lower() for e in self.edges if e.parent.lower() == tl}
                def rel(item):
                    cl, c = item
                    words = set(re.findall(r"[a-z]+", cl)) | {w for s in c.synonyms for w in re.findall(r"[a-z]+", s)}
                    return (cl in must) * 3 + (cl in fk_cols or c.is_pk) * 2 + len(toks & words) * 2 + c.from_excel
                keep = sorted(cols, key=rel, reverse=True)[:max_cols_big]
            for cl, c in keep:
                desc = c.description[:110]
                vals = self.value_profile.get(f"{tl}.{cl}")
                extra = f" | values: {', '.join(repr(v) for v in vals)}" if vals else ""
                lines.append(f"  - {c.name} {c.dtype}{(' -- ' + desc) if desc else ''}{extra}")
        return "\n".join(lines)
