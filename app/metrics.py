from __future__ import annotations

import re
from dataclasses import dataclass

from . import domain


@dataclass(frozen=True)
class Metric:
    name: str
    trigger: frozenset[str]      # at least one must be present
    extra: frozenset[str]        # additional words allowed for this metric
    sql: str
    tables: tuple[str, ...]
    admin_only: bool = False


_F = frozenset
METRICS: list[Metric] = [
    Metric("returned_books", _F({"received", "returned"}), _F({"back"}),
           "SELECT COUNT(*) AS returned_books FROM CircIssueTransaction WHERE status = 'Recieved'",
           ("circissuetransaction",), True),
    Metric("overdue_books", _F({"overdue"}), _F({"currently", "now"}),
           "SELECT COUNT(*) AS overdue_books FROM CircIssueTransaction WHERE status = 'Issued' AND duedate < CAST(GETDATE() AS date)",
           ("circissuetransaction",), True),
    Metric("issued_books", _F({"issued", "borrowed", "loaned"}), _F({"currently", "now", "right", "abhi"}),
           "SELECT COUNT(*) AS currently_issued_books FROM CircIssueTransaction WHERE status = 'Issued'",
           ("circissuetransaction",), True),
    Metric("available_copies", _F({"available"}), _F({"currently", "now"}),
           "SELECT COUNT(*) AS available_copies FROM bookaccessionmaster WHERE CheckStatus = 'A'",
           ("bookaccessionmaster",)),
    Metric("members", _F({"members", "member", "students", "student"}), _F({"registered"}),
           "SELECT COUNT(*) AS total_members, SUM(CASE WHEN status = 'Active' THEN 1 ELSE 0 END) AS active_members FROM CircUserManagement",
           ("circusermanagement",), True),
    Metric("total_books", _F({"books", "book", "titles", "title", "copies"}), _F(),
           "SELECT (SELECT COUNT(*) FROM bookcatalog) AS total_titles, (SELECT COUNT(*) FROM bookaccessionmaster) AS total_copies",
           ("bookcatalog", "bookaccessionmaster")),
]
_COUNTQ = re.compile(r"\b(how many|total|count|number of|no of|kitni|kitne)\b")
_FILLER = set("how many total count number of no the are is there in a an do we have has library all books book copies titles "
              "title kitni kitne hai hain what s".split())


def match(question: str, role: str = "admin") -> Metric | None:
    q = domain.apply_glossary(question.lower())
    if not _COUNTQ.search(q):
        return None
    words = set(re.findall(r"[a-z]+", q))
    for m in METRICS:
        if m.admin_only and role != "admin":
            continue
        if not (words & m.trigger):
            continue
        allowed = _FILLER | m.extra | m.trigger
        if not (words - allowed):
            return m
    return None
