"""Guard tests (need sqlglot installed)."""
import pytest

pytest.importorskip("sqlglot")

from app import sql_guard
from app.schema_kb import SchemaKB

KB = SchemaKB.load(catalog=None, profile=False)


def ok(sql, **kw):
    return sql_guard.validate(sql, KB, **kw)


def test_accepts_valid_and_adds_top():
    r = ok("SELECT bc.title FROM bookcatalog bc WHERE bc.title LIKE N'%python%'")
    assert r.ok and "TOP 200" in r.sql.upper()


def test_caps_large_top():
    r = ok("SELECT TOP 5000 title FROM bookcatalog")
    assert r.ok and "5000" not in r.sql


@pytest.mark.parametrize("bad", [
    "DROP TABLE bookcatalog", "SELECT 1; DELETE FROM bookcatalog", "UPDATE bookcatalog SET title='x'",
    "SELECT title FROM bookcatalog -- x", "EXEC xp_cmdshell 'dir'", "SELECT * INTO #t FROM bookcatalog",
])
def test_rejects_dangerous(bad):
    assert not ok(bad).ok


def test_rejects_unknown_table_and_column():
    assert "Unknown table" in ok("SELECT a FROM nosuchtable").error
    assert "does not exist" in ok("SELECT bc.nonsense FROM bookcatalog bc").error


def test_rejects_star_and_pii():
    assert not ok("SELECT * FROM bookcatalog").ok
    assert not ok("SELECT cu.AadharNo FROM CircUserManagement cu").ok
    assert ok("SELECT COUNT(*) AS n FROM bookcatalog").ok


def test_member_rls():
    q = "SELECT it.issuedate FROM CircIssueTransaction it WHERE it.status = 'Issued'"
    assert not ok(q, role="member", user_id="42").ok
    assert ok(q + " AND it.userid = '42'", role="member", user_id="42").ok
    assert not ok(q + " AND it.userid = '99'", role="member", user_id="42").ok
    assert not ok("SELECT it.issuedate FROM CircIssueTransaction it WHERE it.userid = '42' OR 1=1",
                  role="member", user_id="42").ok
    assert not ok("SELECT vendorname FROM vendormaster", role="member", user_id="42").ok


def test_member_vmember_scope():
    # member may read only their own vMember row (by usercode or userid), N'' literals accepted
    assert ok("SELECT name, finebal FROM vMember WHERE usercode = 'M1'", role="member", user_id="M1", member_ids=["M1", "u1"]).ok
    assert ok("SELECT name FROM vMember WHERE userid = N'u1'", role="member", user_id="M1", member_ids=["M1", "u1"]).ok
    assert not ok("SELECT name FROM vMember WHERE usercode = 'M2'", role="member", user_id="M1", member_ids=["M1"]).ok
    assert not ok("SELECT name, finebal FROM vMember", role="member", user_id="M1", member_ids=["M1"]).ok
    assert not ok("SELECT name FROM vMember WHERE usercode = 'M1' OR 1=1", role="member", user_id="M1", member_ids=["M1"]).ok
    assert not ok("SELECT AadharNo FROM vMember WHERE usercode = 'M1'", role="member", user_id="M1", member_ids=["M1"]).ok
    assert not ok("SELECT memberPic FROM vMember WHERE usercode = 'M1'", role="member", user_id="M1", member_ids=["M1"]).ok
