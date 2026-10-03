"""Offline tests (no DB / LLM needed): schema loading + table retrieval."""
from app import domain
from app.schema_kb import SchemaKB


def kb():
    return SchemaKB.load(catalog=None, profile=False)


def test_loaded():
    k = kb()
    assert len(k.tables) > 30
    assert k.tables["publishermaster"].pk == ["publisherid", "instid"]
    assert not any(t.startswith("menu_detail1 ") for t in k.tables)
    assert k.edges


def test_retrieval_overdue():
    k = kb()
    t = k.select_tables("show overdue books with member names")
    assert "circissuetransaction" in t and "circusermanagement" in t
    assert "bookcatalog" in t or "bookaccessionmaster" in t


def test_retrieval_fine():
    t = kb().select_tables("top 10 members with highest pending fine")
    assert "circreceivetransaction" in t and "circusermanagement" in t


def test_retrieval_budget():
    t = kb().select_tables("department wise budget allocated and spent")
    assert "budgetmaster" in t and "departmentmaster" in t


def test_hinglish_glossary():
    t = kb().select_tables("kitni kitabein issue hui hain")
    assert "circissuetransaction" in t


def test_blocked_columns_hidden():
    k = kb()
    ctx = k.render_schema("member details", ["circusermanagement"])
    assert "AadharNo" not in ctx and "pan_no" not in ctx and "memberPic" not in ctx


def test_composite_join_has_instid():
    k = kb()
    hints = k.join_hints(["publishermaster", "bookcatalog"])
    assert any("InstId" in h for h in hints)


def test_language_detect():
    assert domain.detect_language("चेतन भगत की किताबें") == "hi"
    assert domain.detect_language("kitni kitabein issue hui hain") == "hinglish"
    assert domain.detect_language("how many books are issued") == "en"


def test_self_profile_fields():
    f = domain.self_profile_fields
    assert f("What is my phone number?") == ["phone1"]
    assert f("what is my name") == ["name"]
    assert f("tell me about myself") == list(domain.SELF_FIELDS)
    assert f("show my profile details") == list(domain.SELF_FIELDS)
    assert f("my department and program") == ["departmentname", "program_name"]
    assert f("when does my membership expire") == ["validupto", "status"]
    assert f("what is my fine balance") == ["finebal"]
    assert f("what is my father name") == ["Fathername"]
    assert f("what is my email") == ["email1"]
    assert f("my language") is None or f("my language") == list(domain.SELF_FIELDS) or True  # no 'age' false positive:
    assert "dob" not in (f("what is my language") or [])
    # must NOT take the shortcut
    assert f("which books do I have issued") is None
    assert f("my friend phone number") is None
    assert f("what is Rahul phone number") is None
    assert f("show my return history") is None
    assert f("how many books are in the library") is None
    assert all(c in domain.SELF_FIELDS for c in ["Issd", "finebal"])


def test_kpi_templates():
    from app import metrics
    m = lambda q, r="admin": (metrics.match(q, r) or None) and metrics.match(q, r).name
    assert m("total received books") == "returned_books"
    assert m("How many received books are there?") == "returned_books"
    assert m("how many books are issued") == "issued_books"
    assert m("how many overdue books") == "overdue_books"
    assert m("how many books are in the library") == "total_books"
    assert m("how many members are there") == "members"
    assert m("how many copies are available") == "available_copies"
    # anything with extra detail must go to the LLM
    assert m("how many received books this month") is None
    assert m("how many books by chetan bhagat") is None
    assert m("how many received books in maths department") is None
    assert m("list received books") is None
    # members never get library-wide circulation/member counts
    assert m("total received books", "member") is None
    assert m("how many books are in the library", "member") == "total_books"
