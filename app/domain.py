from __future__ import annotations

import re
from dataclasses import dataclass

# ----------------------------------------------------------------------------- language helpers
GLOSSARY = {
    "kitab": "book", "kitabein": "books", "kitabe": "books", "kitaben": "books", "pustak": "book",
    "pustake": "books", "lekhak": "author", "writer": "author", "prakashak": "publisher",
    "sadasya": "member", "chhatra": "student", "vidyarthi": "student", "jurmana": "fine", "dand": "fine",
    "wapas": "return", "vapas": "return", "lautayi": "returned", "uplabdh": "available", "vibhag": "department",
    "sabhi": "all", "kul": "total", "naya": "new", "nayi": "new", "naye": "new", "kitabon": "books",
    "jari": "issue", "vitarit": "issued", "bakaya": "pending", "baki": "pending", "lambit": "pending",
    "samay": "time", "tarikh": "date", "shulk": "fee", "bajet": "budget", "vikreta": "vendor",
}
HINGLISH_MARKERS = {
    "kitni", "kitne", "kitna", "kaun", "kya", "hai", "hain", "mujhe", "batao", "bataiye", "dikhao", "dikhaiye",
    "wali", "wale", "mera", "meri", "mere", "kab", "kahan", "nahi", "aur", "sabhi", "pehle", "uska", "uski",
    "isme", "kitab", "kitabein", "hui", "hue", "gayi", "gaya", "wapas", "vapas", "abhi", "aaj", "kal", "liye",
    "kisne", "kiski", "kiska", "kisko", "sab", "ka", "ki", "ko", "se", "mein", "me", "par",
}
_STRONG_HINGLISH = HINGLISH_MARKERS - {"ka", "ki", "ko", "se", "me", "mein", "par", "aur", "sab", "hai"}


def apply_glossary(text: str) -> str:
    return re.sub(r"[a-z]+", lambda m: GLOSSARY.get(m.group(0), m.group(0)), text)


def detect_language(text: str) -> str:
    """'hi' (Devanagari) | 'hinglish' (Hindi in Latin script) | 'en'."""
    dev = len(re.findall(r"[\u0900-\u097F]", text))
    letters = len(re.findall(r"[A-Za-z\u0900-\u097F]", text)) or 1
    if dev / letters > 0.3:
        return "hi"
    words = set(re.findall(r"[a-z]+", text.lower()))
    return "hinglish" if len(words & _STRONG_HINGLISH) >= 1 or len(words & HINGLISH_MARKERS) >= 3 else "en"


# ----------------------------------------------------------------------------- security
# Never exposed to the LLM, never allowed in generated SQL (PII / binary).
BLOCKED_COLUMNS = {"aadharno", "pan_no", "memberpic"}

# Row-level security for role == "member": table -> column that must equal the caller's user id.
USER_SCOPED_TABLES = {
    "circissuetransaction": {"userid"},
    "circreceivetransaction": {"userid"},
    "circusermanagement": {"userid"},
    "fine_calculation": {"userid"},
    "circpartpayment": {"user_id"},
    "vmember": {"usercode", "userid"},   # member profile view (1 row per member)
}
MEMBER_ALLOWED_TABLES = {
    "bookcatalog", "bookauthor", "bookaccessionmaster", "item_type", "itemstatusmaster", "publishermaster",
    "locationstorage", "digitalcontent", "digitalcontentfiles", "typeofcontent", "program_master",
    "departmentmaster", "language_master", *USER_SCOPED_TABLES,
}

# ----------------------------------------------------------------------------- schema semantics
TABLE_NOTES = {
    "bookcatalog": "Bibliographic record = one TITLE (many copies). Publisher name is in column firstname.",
    "bookaccessionmaster": "Physical COPIES of titles. One row per accession number. CheckStatus A=available, I=issued.",
    "circusermanagement": "Library MEMBERS (students/faculty/staff). Full name in MembName.",
    "circissuetransaction": "Every ISSUE (loan). status='Issued' => currently on loan.",
    "circreceivetransaction": "Every RETURN incl. fine info. One row per returned issue (IssueId).",
    "fine_calculation": "Current outstanding fine balance per member (userid, fine).",
    "circpartpayment": "Part/full payments of fines (Pay_amt, pay_date).",
    "bookarrival": "NEW ACQUISITIONS only (MRN, order number, gift flag, copies). NOT for books received back/returned by members. No direct join to copies.",
    "ordermaster": "Purchase orders. cancelorder=1 means cancelled.",
    "budgetmaster": "Department budget per session (composite PK departmentcode+Curr_Session+InstId).",
    "publishermaster": "Publishers (composite PK PublisherId+InstId).",
    "vendormaster": "Vendors/suppliers (composite PK vendorid+InstId).",
    "locationstorage": "Physical shelf locations; details JSON in JsonStorage, joined via bookaccessionmaster.Loc_id = StoreId.",
    "itemstatusmaster": "Item states such as Lost/Damaged/Withdrawn. bookaccessionmaster.Status -> ItemStatusID.",
    "digitalcontent": "E-resources (e-books, videos, PDFs...).",
    "bookauthor": "Authors of a title (several rows per ctrl_no). Full name in Author.",
    "vmember": "VIEW: member profile + current numbers (Issd = books currently issued, finebal = fine balance). One row per member. Best source for my profile / my fine / my validity and for member lists with fine.",
}
IMPORTANT_COLUMNS = {
    "bookcatalog": {"ctrl_no", "title", "isbn", "issn", "edition", "publisherno", "firstname", "subject1", "subject2",
                    "dept", "language_name", "pubdate", "bookcost", "pages", "volume", "booktype", "itemcategory",
                    "catalogdate1", "instid"},
    "circusermanagement": {"userid", "usercode", "membname", "firstname", "lastname", "departmentcode", "validupto",
                           "status", "email1", "phone1", "program_id", "membergroupid", "classname", "gender", "doj",
                           "yearsem", "section", "session", "issuedbookstatus", "instid"},
    "bookaccessionmaster": {"accessionnumber", "booktitle", "checkstatus", "status", "ctrl_no", "loc_id",
                            "accessioneddate", "bookprice", "vendorid", "item_type", "copynumber", "itemcategory",
                            "booknumber", "pubyear", "instid"},
    "vmember": {"usercode", "userid", "name", "classname", "departmentname", "program_name", "session", "validupto", "status", "issd", "finebal", "email1", "phone1", "doj", "dob", "fathername", "peraddress", "percity", "cat_name", "instid"},
}
COLUMN_OVERRIDES = {
    ("budgetmaster", "expendedamount"): "Amount already SPENT (expended) by the department in that session",
    ("circusermanagement", "phone1"): "Member primary phone/mobile number (NOT a primary key)",
    ("circusermanagement", "userid"): "Member identifier - the join key used by all circulation tables",
    ("circusermanagement", "usercode"): "Member code / library card / roll number shown to the user",
    ("circusermanagement", "status"): "Member status (e.g. Active / Sleep) - see values",
    ("bookaccessionmaster", "checkstatus"): "A = Available on shelf, I = currently Issued",
    ("bookaccessionmaster", "status"): "Item status id -> ItemStatusMaster.ItemStatusID (Lost, Damaged...). NOT the loan status",
    ("circissuetransaction", "status"): "'Issued' = currently on loan; already-returned rows use the spelling in values (e.g. 'Recieved')",
    ("bookcatalog", "firstname"): "PUBLISHER name (denormalised) - not a person's name",
    ("bookcatalog", "departmentname"): "Department name linked to the title",
    ("circreceivetransaction", "ispaid"): "Fine payment flag - see values (0/N = unpaid, 1/Y = paid)",
}
# Views that are not in table_schema.xlsx. Columns come from the live DB; descriptions from here.
VIEWS = {
    "vMember": {
        "usercode": "Member code / library card / roll number (unique per member)",
        "userid": "Login/user id (can be empty for some members)",
        "name": "Full name of the member",
        "classname": "Member group (STUDENT / FACULTY / STAFF ...)",
        "departmentname": "Department name of the member",
        "departmentcode": "Department code",
        "program_name": "Academic program / course name",
        "program_id": "Program id",
        "session": "Academic session of the member (e.g. 2024-25)",
        "validupto": "Membership expiry date",
        "status": "Member status: Active / Sleep",
        "Issd": "Number of books CURRENTLY issued to the member",
        "finebal": "Current outstanding FINE balance of the member (amount)",
        "email1": "Email address",
        "phone1": "Mobile / phone number",
        "peraddress": "Permanent address",
        "percity": "City",
        "Fathername": "Father's name",
        "doj": "Date of joining",
        "dob": "Date of birth",
        "joinyear": "Year of joining",
        "cat_name": "Member category name (e.g. Gen)",
        "cat_id": "Category id",
        "policystatus": "Policy status flag",
        "InstId": "Institute id",
    },
}

# enum-like columns whose real values are read from the DB at startup and shown to the LLM
PROFILE_TARGETS = [
    ("circissuetransaction", "status"), ("bookaccessionmaster", "checkstatus"), ("circusermanagement", "status"),
    ("circusermanagement", "gender"), ("circreceivetransaction", "ispaid"), ("circreceivetransaction", "finecause"),
    ("itemstatusmaster", "itemstatus"), ("item_type", "item_type"), ("membergroup", "groupname"),
    ("ordermaster", "orderstatus"), ("bookarrival", "isgift"), ("bookarrival", "itemstatus"),
    ("typeofcontent", "contenttype"), ("media_type", "media_name"), ("bookaccessionmaster", "item_type"),
    ("librarysetupinformation", "currentacademicsession"), ("bookcatalog", "booktype"),
    ("vmember", "status"), ("vmember", "classname"), ("vmember", "cat_name"),
]

# ----------------------------------------------------------------------------- entities
@dataclass(frozen=True)
class Entity:
    keywords: tuple[str, ...]
    primary: str
    related: tuple[str, ...]
    default_fields: tuple[str, ...]
    default_order: str
    description: str


ENTITIES: dict[str, Entity] = {
    "BOOK": Entity(("book", "books", "title", "titles", "catalog", "bibliographic", "isbn", "issn", "edition", "subject"),
                   "bookcatalog", ("bookauthor", "bookaccessionmaster"), ("title", "isbn", "edition", "firstname"),
                   "title", "Titles (bibliographic records)."),
    "BOOK_COPY": Entity(("copy", "copies", "accession", "accession number", "barcode", "holding", "physical copy"),
                        "bookaccessionmaster", ("bookcatalog", "locationstorage", "itemstatusmaster"),
                        ("accessionnumber", "booktitle", "CheckStatus", "Loc_id"), "accessionnumber",
                        "Physical copies."),
    "MEMBER": Entity(("member", "members", "student", "students", "faculty", "staff", "reader", "borrower", "patron", "membership"),
                     "circusermanagement", ("circissuetransaction", "circreceivetransaction", "membergroup", "program_master"),
                     ("MembName", "userid", "usercode", "departmentcode", "validupto"), "MembName",
                     "Library members."),
    "ISSUE": Entity(("issue", "issued", "borrow", "borrowed", "loan", "loaned", "circulation", "checkout", "lent"),
                    "circissuetransaction", ("bookaccessionmaster", "circusermanagement", "bookcatalog"),
                    ("issuedate", "duedate", "status", "accno"), "issuedate DESC", "Loans."),
    "RETURN": Entity(("return", "returned", "received", "submitted", "deposited"),
                     "circreceivetransaction", ("circissuetransaction", "circusermanagement"),
                     ("receivingdate", "fineamount", "isPaid", "accno"), "receivingdate DESC", "Returns incl. fines."),
    "FINE": Entity(("fine", "fines", "penalty", "overdue charge", "fine amount", "pending fine", "paid fine", "fine balance"),
                   "circreceivetransaction", ("fine_calculation", "circpartpayment", "circusermanagement"),
                   ("fineamount", "fineCause", "paidOn", "waivedAmt"), "receivingdate DESC", "Fines and payments."),
    "ARRIVAL": Entity(("arrival", "arrivals", "arrived", "mrn", "gift", "donation", "receipt"),
                      "bookarrival", ("ordermaster",), ("ArriveDate", "ArrMrnNo", "OrderNumber", "NoOfCopies", "IsGift"),
                      "ArriveDate DESC", "Book receipt records."),
    "DIGITAL_CONTENT": Entity(("digital", "e-book", "ebook", "pdf", "video", "audio", "electronic", "multimedia", "e-resource"),
                              "digitalcontent", ("digitalcontentfiles", "typeofcontent"),
                              ("Title", "Descr", "DateSaved", "ValidTill"), "DateSaved DESC", "Digital resources."),
    "PUBLISHER": Entity(("publisher", "publishers", "publishing", "publication"),
                        "bookcatalog", ("publishermaster",), ("firstname", "title", "isbn"), "firstname",
                        "Publishers (name lives in bookcatalog.firstname)."),
    "VENDOR": Entity(("vendor", "vendors", "supplier", "bookseller", "distributor"),
                     "vendormaster", ("bookaccessionmaster", "ordermaster"), ("vendorname", "vendorcode", "phone1", "emailID"),
                     "vendorname", "Suppliers."),
    "LOCATION": Entity(("location", "rack", "shelf", "almirah", "hall", "storage", "kept", "located", "where is", "placed"),
                       "locationstorage", ("bookaccessionmaster",), ("Scheme", "JsonStorage"), "StoreId", "Shelf locations."),
    "PROGRAM": Entity(("program", "programme", "course", "degree", "branch", "curriculum"),
                      "program_master", ("programsubject", "circusermanagement"), ("program_name", "short_name"),
                      "program_name", "Academic programs."),
    "DEPARTMENT": Entity(("department", "departments", "dept"),
                         "departmentmaster", ("bookcatalog", "circusermanagement", "budgetmaster"),
                         ("departmentname", "shortname", "departmentcode"), "departmentname", "Departments."),
    "LANGUAGE": Entity(("language", "languages", "hindi", "english", "translation"),
                       "bookcatalog", (), ("language_name", "title"), "language_name", "Language of a title (bookcatalog.language_name)."),
    "AUTHOR": Entity(("author", "authors", "writer", "authored"),
                     "bookauthor", ("bookcatalog",), ("Author", "firstname1", "lastname1"), "Author", "Authors."),
    "BUDGET": Entity(("budget", "budgets", "allocation", "allocated", "expenditure", "expended", "spent", "fund"),
                     "budgetmaster", ("departmentmaster",), ("allocatedamount", "expendedamount", "Curr_Session"),
                     "allocatedamount DESC", "Budgets."),
    "ORDER": Entity(("order", "orders", "purchase order", "procurement", "acquisition"),
                    "ordermaster", ("vendormaster", "bookarrival"), ("ordernumber", "orderdate", "OrderStatus", "cancelorder"),
                    "orderdate DESC", "Purchase orders."),
    "MY_PROFILE": Entity(("my profile", "my details", "my information", "my info", "about me", "about myself", "my name", "my email", "my phone", "my mobile", "my address", "my department", "my program", "my course", "my membership", "my validity", "my fine", "my fine balance", "my session", "my category", "my member code", "my usercode", "my status", "my card", "my father"),
                         "vmember", (), ("name", "usercode", "classname", "departmentname", "program_name", "validupto", "status", "Issd", "finebal"),
                         "name", "The caller's own profile (member role)."),
    "ITEM_STATUS": Entity(("lost", "damaged", "missing", "withdrawn", "item status"),
                          "itemstatusmaster", ("bookaccessionmaster",), ("ItemStatus", "isIsued"), "ItemStatus", "Item states."),
}


def entity_table_boosts(question: str) -> dict[str, float]:
    q = " " + apply_glossary(question.lower()) + " "
    boosts: dict[str, float] = {}
    for ent in ENTITIES.values():
        hits = sum(1 for k in ent.keywords if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", q))
        if not hits:
            continue
        boosts[ent.primary] = boosts.get(ent.primary, 0) + min(4.0 * hits, 8.0)
        for r in ent.related:
            boosts[r] = boosts.get(r, 0) + 1.0
    for name in RULE_TABLES:
        if _rule_hit(name, q):
            for t in RULE_TABLES[name]:
                boosts[t] = boosts.get(t, 0) + 4.0
    for t in TABLE_NOTES:  # small prior for core tables (keeps peripheral master tables out)
        boosts[t] = boosts.get(t, 0) + 1.5
    return boosts


# ----------------------------------------------------------------------------- business rules
UNPAID = "CAST(ISNULL({a}.isPaid, 0) AS varchar(5)) IN ('0','N','No','False')"
PAID = "CAST(ISNULL({a}.isPaid, 0) AS varchar(5)) IN ('1','Y','Yes','True')"
TODAY = "CAST(GETDATE() AS date)"


@dataclass(frozen=True)
class Rule:
    keywords: tuple[str, ...]
    text: str


BUSINESS_RULES: dict[str, Rule] = {
    "AVAILABLE": Rule(("available", "in library", "not issued", "on shelf", "free copies"),
                      "Available copies: bookaccessionmaster.CheckStatus = 'A' (join bookcatalog ON ctrl_no). "
                      "Count copies with COUNT(*), count distinct titles with COUNT(DISTINCT ctrl_no)."),
    "ISSUED": Rule(("issued", "borrowed", "on loan", "checked out", "currently issued", "lent"),
                   "Currently issued: CircIssueTransaction.status = 'Issued' "
                   "(join bookaccessionmaster ON it.accno = am.accessionnumber, CircUserManagement ON it.userid = cu.userid)."),
    "RETURNED": Rule(("returned", "received", "receive", "received back", "submitted", "deposited"),
                     "IMPORTANT: in this library 'received books' / 'books received' means books RECEIVED BACK from members (= returned). "
                     "It is NOT BookArrival (new acquisitions) unless the user says arrival / new / purchase / gift / MRN. "
                     "Returned loans: CircIssueTransaction.status = the 'returned' value from the values list "
                     "(spelled 'Recieved' in this database). Return date/fine: CircReceiveTransaction (join ON IssueId)."),
    "OVERDUE": Rule(("overdue", "late", "past due", "delayed", "not returned in time"),
                    f"Overdue: CircIssueTransaction.status = 'Issued' AND duedate < {TODAY}. "
                    f"days_overdue = DATEDIFF(day, duedate, {TODAY})."),
    "DUE_SOON": Rule(("due soon", "due date", "due in", "deadline", "return by", "due this week", "due tomorrow"),
                     f"Due soon: status = 'Issued' AND duedate BETWEEN {TODAY} AND DATEADD(day, N, {TODAY})."),
    "FINE_PENDING": Rule(("pending fine", "unpaid fine", "fine due", "outstanding fine", "fine balance", "fine pending"),
                         "Unpaid fines: CircReceiveTransaction.fineamount > 0 AND " + UNPAID.format(a="rt") +
                         ". Current balance per member is also in Fine_calculation(userid, fine)."),
    "FINE_PAID": Rule(("paid fine", "cleared fine", "fine settled", "fine collected", "fine paid"),
                      "Paid fines: CircReceiveTransaction.fineamount > 0 AND " + PAID.format(a="rt") +
                      "; payments in Circpartpayment(Pay_amt, pay_date)."),
    "NEW_ARRIVAL": Rule(("new arrival", "latest arrival", "recent arrival", "new books", "latest books", "recently added",
                         "just arrived", "newly added"),
                        "Newly added books: bookaccessionmaster.accessioneddate (ORDER BY DESC / >= DATEADD(day,-N,GETDATE())) "
                        "grouped by bookcatalog.title. BookArrival has NO join to copies - use it only for MRN/order/gift stats."),
    "LOST": Rule(("lost", "missing", "damaged", "withdrawn"),
                 "Lost/damaged copies: JOIN ItemStatusMaster s ON bookaccessionmaster.Status = s.ItemStatusID "
                 "WHERE s.ItemStatus LIKE N'%lost%' (use the exact name from the values list)."),
    "MEMBERSHIP_EXPIRED": Rule(("expired", "expiry", "validity", "valid upto", "membership ended"),
                               f"Expired membership: CircUserManagement.validupto < {TODAY}."),
    "ACTIVE_MEMBER": Rule(("active member", "active members", "inactive", "sleep"),
                          "Member state: CircUserManagement.status (see values list, e.g. 'Active' / 'Sleep')."),
    "GIFT": Rule(("gift", "donation", "donated"),
                 "BookArrival.IsGift: 1 = gift/donation, 0 = purchased."),
    "OPEN_ORDER": Rule(("open order", "active order", "pending order", "cancelled order"),
                       "ordermaster.cancelorder: 1 = cancelled, 0 = active."),
    "DIGITAL_VALID": Rule(("valid digital", "active digital", "not expired digital"),
                          f"Digital content still valid: DigitalContent.ValidTill >= {TODAY}."),
    "SELF": Rule(("my profile", "my details", "my information", "my info", "about me", "about myself", "my name", "my email", "my phone", "my mobile", "my address", "my department", "my program", "my course", "my membership", "my validity", "my fine", "my fine balance", "my session", "my category", "my member code", "my usercode", "my status", "my card", "my father"),
                 "Caller's own profile/fine/validity: SELECT from vMember WHERE usercode = <caller code> (one row). "
                 "Columns: name, usercode, classname, departmentname, program_name, session, validupto, status, "
                 "Issd (books currently issued), finebal (fine balance), email1, phone1. Never SELECT *."),
}


RULE_TABLES = {
    "AVAILABLE": ("bookaccessionmaster", "bookcatalog"),
    "ISSUED": ("circissuetransaction", "circusermanagement", "bookaccessionmaster", "bookcatalog"),
    "RETURNED": ("circissuetransaction", "circreceivetransaction"),
    "OVERDUE": ("circissuetransaction", "circusermanagement", "bookaccessionmaster", "bookcatalog"),
    "DUE_SOON": ("circissuetransaction", "circusermanagement", "bookaccessionmaster", "bookcatalog"),
    "FINE_PENDING": ("circreceivetransaction", "circusermanagement", "fine_calculation"),
    "FINE_PAID": ("circreceivetransaction", "circpartpayment", "circusermanagement"),
    "NEW_ARRIVAL": ("bookaccessionmaster", "bookcatalog"),
    "LOST": ("bookaccessionmaster", "itemstatusmaster", "bookcatalog"),
    "MEMBERSHIP_EXPIRED": ("circusermanagement",),
    "ACTIVE_MEMBER": ("circusermanagement",),
    "GIFT": ("bookarrival",),
    "OPEN_ORDER": ("ordermaster",),
    "DIGITAL_VALID": ("digitalcontent",),
    "SELF": ("vmember",),
}


def _rule_hit(name: str, q: str) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", q) for k in BUSINESS_RULES[name].keywords)


def match_rules(question: str) -> list[str]:
    q = " " + question.lower() + " "
    out = []
    for name, rule in BUSINESS_RULES.items():
        if any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", q) for k in rule.keywords):
            out.append(f"[{name}] {rule.text}")
    if any(r.startswith("[SELF]") for r in out):   # own fine/profile -> vMember, not the fine transaction rules
        out = [r for r in out if not r.startswith(("[FINE_PENDING]", "[FINE_PAID]"))]
    return out


GLOBAL_SQL_RULES = """\
T-SQL (Microsoft SQL Server) rules:
1. Output exactly ONE read-only SELECT (CTEs allowed). No INSERT/UPDATE/DELETE/DDL/EXEC, no temp tables, no comments.
2. Use TOP n (never LIMIT). For non-aggregate list queries always add TOP (default TOP 50). Sort meaningfully.
3. Use ONLY tables/columns from the SCHEMA block, spelled exactly. Never invent columns.
4. Text search: col LIKE N'%value%' (N prefix for Hindi/Unicode). Names/titles: search with LIKE, never '='.
   Multi-word names: put each word in its own LIKE joined by AND (e.g. MembName LIKE N'%rahul%' AND MembName LIKE N'%kumar%').
5. Dates: today = CAST(GETDATE() AS date); month start = DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1);
   compare dates with >= start AND < next-day/next-month, use DATEADD/DATEDIFF. Avoid FORMAT except for month labels.
6. "book" = ask what is meant: TITLE (bookcatalog) vs COPY (bookaccessionmaster). Counting "books in library" -> return both.
7. Joins: use the JOIN HINTS. Tables with composite PK (publishermaster, vendormaster, budgetmaster) must also join on InstId.
8. Status/flag filters must use the exact literals in the 'values:' lists of the schema.
9. Give every output column a readable alias (member_name, title, issue_date ...). Never SELECT * . Never select Aadhaar/PAN/photo.
10. Aggregates: GROUP BY every non-aggregated column; add ORDER BY the metric DESC for rankings.
"""

# ----------------------------------------------------------------------------- few-shot examples
EXAMPLES: list[tuple[str, str]] = [
    ("how many books are in the library",
     "SELECT (SELECT COUNT(*) FROM bookcatalog) AS total_titles, (SELECT COUNT(*) FROM bookaccessionmaster) AS total_copies"),
    ("is the book python available? how many copies",
     "SELECT bc.title, COUNT(*) AS available_copies FROM bookaccessionmaster am JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE bc.title LIKE N'%python%' AND am.CheckStatus = 'A' GROUP BY bc.title ORDER BY bc.title"),
    ("list all currently issued books with member names",
     "SELECT TOP 50 cu.MembName AS member_name, bc.title, it.accno AS accession_no, it.issuedate AS issue_date, it.duedate AS due_date "
     "FROM CircIssueTransaction it JOIN CircUserManagement cu ON it.userid = cu.userid "
     "JOIN bookaccessionmaster am ON it.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE it.status = 'Issued' ORDER BY it.issuedate DESC"),
    ("show overdue books and how many days late",
     "SELECT TOP 50 cu.MembName AS member_name, bc.title, it.duedate AS due_date, DATEDIFF(day, it.duedate, CAST(GETDATE() AS date)) AS days_overdue "
     "FROM CircIssueTransaction it JOIN CircUserManagement cu ON it.userid = cu.userid "
     "JOIN bookaccessionmaster am ON it.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE it.status = 'Issued' AND it.duedate < CAST(GETDATE() AS date) ORDER BY days_overdue DESC"),
    ("how many books were issued this month",
     "SELECT COUNT(*) AS issued_this_month FROM CircIssueTransaction "
     "WHERE issuedate >= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1) AND issuedate < DATEADD(month, 1, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"),
    ("top 10 most issued books",
     "SELECT TOP 10 bc.title, COUNT(*) AS times_issued FROM CircIssueTransaction it "
     "JOIN bookaccessionmaster am ON it.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "GROUP BY bc.title ORDER BY times_issued DESC"),
    ("which books has rahul kumar currently issued",
     "SELECT cu.MembName AS member_name, bc.title, it.issuedate AS issue_date, it.duedate AS due_date "
     "FROM CircIssueTransaction it JOIN CircUserManagement cu ON it.userid = cu.userid "
     "JOIN bookaccessionmaster am ON it.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE cu.MembName LIKE N'%rahul%' AND cu.MembName LIKE N'%kumar%' AND it.status = 'Issued' ORDER BY it.duedate"),
    ("top 10 members with highest pending fine",
     "SELECT TOP 10 cu.MembName AS member_name, SUM(rt.fineamount) AS pending_fine FROM CircReceiveTransaction rt "
     "JOIN CircUserManagement cu ON rt.userid = cu.userid WHERE rt.fineamount > 0 AND " + UNPAID.format(a="rt") +
     " GROUP BY cu.MembName ORDER BY pending_fine DESC"),
    ("books written by chetan bhagat",
     "SELECT bc.title, ba.Author AS author, bc.edition, bc.pubDate AS publication_date FROM bookcatalog bc "
     "JOIN BookAuthor ba ON bc.ctrl_no = ba.ctrl_no WHERE ba.Author LIKE N'%chetan%' AND ba.Author LIKE N'%bhagat%' ORDER BY bc.title"),
    ("books published by oxford",
     "SELECT TOP 50 bc.title, bc.firstname AS publisher, bc.edition, bc.isbn FROM bookcatalog bc WHERE bc.firstname LIKE N'%oxford%' ORDER BY bc.title"),
    ("where is the book data structures kept",
     "SELECT TOP 20 bc.title, am.accessionnumber AS accession_no, ls.JsonStorage AS location FROM bookaccessionmaster am "
     "JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no LEFT JOIN LocationStorage ls ON am.Loc_id = ls.StoreId "
     "WHERE bc.title LIKE N'%data structures%' ORDER BY am.accessionnumber"),
    ("new books added in last 30 days",
     "SELECT TOP 50 bc.title, MAX(am.accessioneddate) AS added_on, COUNT(*) AS copies FROM bookaccessionmaster am "
     "JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no WHERE am.accessioneddate >= DATEADD(day, -30, CAST(GETDATE() AS date)) "
     "GROUP BY bc.title ORDER BY added_on DESC"),
    ("how many members have expired membership",
     "SELECT COUNT(*) AS expired_members FROM CircUserManagement WHERE validupto < CAST(GETDATE() AS date)"),
    ("department wise budget allocated and spent",
     "SELECT dm.departmentname AS department, bm.Curr_Session AS session, bm.allocatedamount AS allocated, bm.expendedamount AS spent, "
     "(bm.allocatedamount - bm.expendedamount) AS balance FROM budgetmaster bm JOIN departmentmaster dm ON bm.departmentcode = dm.departmentcode "
     "ORDER BY bm.Curr_Session DESC, bm.allocatedamount DESC"),
    ("digital content count by type",
     "SELECT tc.ContentType AS content_type, COUNT(*) AS total FROM DigitalContent dc JOIN TypeOfContent tc ON dc.ContentTypeId = tc.Id "
     "GROUP BY tc.ContentType ORDER BY total DESC"),
    ("month wise books issued in last 12 months",
     "SELECT FORMAT(issuedate, 'yyyy-MM') AS issue_month, COUNT(*) AS books_issued FROM CircIssueTransaction "
     "WHERE issuedate >= DATEADD(month, -12, CAST(GETDATE() AS date)) GROUP BY FORMAT(issuedate, 'yyyy-MM') ORDER BY issue_month"),
    ("how many received books are there",
     "SELECT COUNT(*) AS returned_books FROM CircIssueTransaction WHERE status = 'Recieved'"),
    ("how many books were returned this month",
     "SELECT COUNT(*) AS returned_this_month FROM CircReceiveTransaction WHERE receivingdate >= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1) "
     "AND receivingdate < DATEADD(month, 1, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"),
    ("how many copies are lost",
     "SELECT COUNT(*) AS lost_copies FROM bookaccessionmaster am JOIN ItemStatusMaster s ON am.Status = s.ItemStatusID WHERE s.ItemStatus LIKE N'%lost%'"),
    ("member wise count of books issued by department",
     "SELECT cu.departmentcode AS department_code, COUNT(*) AS books_issued FROM CircIssueTransaction it "
     "JOIN CircUserManagement cu ON it.userid = cu.userid GROUP BY cu.departmentcode ORDER BY books_issued DESC"),
    # ---- member self-service: CALLER_CODE / CALLER_KEY are replaced by the caller's real ids (shown to role=member only)
    ("show my profile details",
     "SELECT name AS member_name, usercode AS member_code, classname AS member_group, departmentname AS department, program_name, session, "
     "validupto AS valid_upto, status, Issd AS books_issued, finebal AS fine_balance, email1 AS email, phone1 AS phone "
     "FROM vMember WHERE usercode = 'CALLER_CODE'"),
    ("what is my fine balance",
     "SELECT name AS member_name, finebal AS fine_balance FROM vMember WHERE usercode = 'CALLER_CODE'"),
    ("when does my membership expire",
     "SELECT name AS member_name, validupto AS valid_upto, status FROM vMember WHERE usercode = 'CALLER_CODE'"),
    ("which books do I have issued right now",
     "SELECT bc.title, it.issuedate AS issue_date, it.duedate AS due_date FROM CircIssueTransaction it "
     "JOIN bookaccessionmaster am ON it.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE it.userid = 'CALLER_KEY' AND it.status = 'Issued' ORDER BY it.duedate"),
    ("which of my books are overdue",
     "SELECT bc.title, it.duedate AS due_date, DATEDIFF(day, it.duedate, CAST(GETDATE() AS date)) AS days_overdue FROM CircIssueTransaction it "
     "JOIN bookaccessionmaster am ON it.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE it.userid = 'CALLER_KEY' AND it.status = 'Issued' AND it.duedate < CAST(GETDATE() AS date) ORDER BY days_overdue DESC"),
    ("my book return history",
     "SELECT TOP 50 bc.title, it.issuedate AS issue_date, rt.receivingdate AS returned_on, rt.fineamount AS fine FROM CircIssueTransaction it "
     "JOIN CircReceiveTransaction rt ON it.IssueId = rt.IssueId JOIN bookaccessionmaster am ON it.accno = am.accessionnumber "
     "JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no WHERE it.userid = 'CALLER_KEY' AND rt.userid = 'CALLER_KEY' ORDER BY rt.receivingdate DESC"),
    ("how many books have I borrowed in total",
     "SELECT COUNT(*) AS total_books_borrowed FROM CircIssueTransaction it WHERE it.userid = 'CALLER_KEY'"),
    ("my fine details for each book",
     "SELECT TOP 50 bc.title, rt.fineamount AS fine, rt.fineCause AS reason, rt.waivedAmt AS waived, rt.paidOn AS paid_on FROM CircReceiveTransaction rt "
     "JOIN bookaccessionmaster am ON rt.accno = am.accessionnumber JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no "
     "WHERE rt.userid = 'CALLER_KEY' AND rt.fineamount > 0 ORDER BY rt.receivingdate DESC"),
    ("my fine payments",
     "SELECT TOP 50 pp.Pay_amt AS amount_paid, pp.pay_date AS paid_on, pp.Rec_no AS receipt_no FROM Circpartpayment pp "
     "WHERE pp.user_id = 'CALLER_KEY' ORDER BY pp.pay_date DESC"),
    ("my books due in the next 7 days",
     "SELECT bc.title, it.duedate AS due_date FROM CircIssueTransaction it JOIN bookaccessionmaster am ON it.accno = am.accessionnumber "
     "JOIN bookcatalog bc ON am.ctrl_no = bc.ctrl_no WHERE it.userid = 'CALLER_KEY' AND it.status = 'Issued' "
     "AND it.duedate BETWEEN CAST(GETDATE() AS date) AND DATEADD(day, 7, CAST(GETDATE() AS date)) ORDER BY it.duedate"),
]


# ----------------------------------------------------------------------------- member self-profile fast path
# key = vMember column, value = (column, readable alias). Aadhaar / PAN / photo are deliberately absent.
SELF_FIELDS = {
    "name": ("name", "member_name"), "usercode": ("usercode", "member_code"), "classname": ("classname", "member_group"),
    "departmentname": ("departmentname", "department"), "program_name": ("program_name", "program"),
    "session": ("session", "session"), "validupto": ("validupto", "valid_upto"), "status": ("status", "membership_status"),
    "Issd": ("Issd", "books_currently_issued"), "finebal": ("finebal", "fine_balance"), "email1": ("email1", "email"),
    "phone1": ("phone1", "phone_number"), "peraddress": ("peraddress", "address"), "percity": ("percity", "city"),
    "Fathername": ("Fathername", "father_name"), "doj": ("doj", "joining_date"), "dob": ("dob", "date_of_birth"),
    "joinyear": ("joinyear", "joining_year"), "cat_name": ("cat_name", "category"),
}
_SELF_RE = re.compile(
    r"\b(my|mine|myself|me|i|"
    r"mera|meri|mere|mujhe|main|hum|ham|apna|apni|apne|"
    r"mai|mein|mujh|muze|mujhko|mujhe hi|"
    r"mera naam|meri details|meri info|mere bare|mere baare|mere baare me|mere bare me|mere bare mai|mere baare mai|mere bare mai batao|mere bare me batao|"
    r"meri jankari|mere bare mein|mere baare mein|mujhe batao|batao mere bare|batao meri details|apni details|apna profile|meri profile|mere profile)\b",
    re.IGNORECASE,
)
_OTHER_PERSON = re.compile(r"\b(friend|brother|sister|mother|wife|husband|son|daughter|classmate|colleague|teacher|professor|dost|bhai|behen|maa|papa|baap|biwi|patni|pati|beta|beti)\b", re.IGNORECASE)
_OTHER_DATA = re.compile(
    r"\b(books?|kitab\w*|pustak\w*|issue[ds]?|borrow\w*|udhaar|return\w*|wapas|vapas|due|overdue|history|transactions?|payments?|receipts?|list|recent|reserv\w*|"
    r"orders?|catalog\w*|authors?|lekhak|publishers?|prakashak|copies|digital|arrival\w*)\b",
    re.IGNORECASE,
)
_FIELD_RULES = [
    (r"\bfather|\bpita", ["Fathername"]),
    (r"\be-?mail|\bmail id|\bemail id", ["email1"]),
    (r"\b(phone|mobile|contact|cell|phone number|mobile number|contact number)", ["phone1"]),
    (r"\b(address|city)\b|\blive\b", ["peraddress", "percity"]),
    (r"\bbirth|\bdob\b|\bage\b", ["dob"]),
    (r"\bjoining|\bjoined|\bjoin date|\bdoj\b", ["doj", "joinyear"]),
    (r"\b(department|dept|vibhag)\b", ["departmentname"]),
    (r"\b(program|programme|course|degree|branch|prakram)\b", ["program_name"]),
    (r"\b(session|batch)\b", ["session"]),
    (r"\bcategory\b", ["cat_name"]),
    (r"\bvalid|\bexpir|\brenew", ["validupto", "status"]),
    (r"\b(status|active|inactive|sleep)\b", ["status"]),
    (r"\bfine|\bpenalt|\bdues|\bjurmana", ["finebal"]),
    (r"\bmember code|\busercode|\buser code|\blibrary card|\bcard number|\broll\b", ["usercode"]),
    (r"\b(group|class)\b", ["classname"]),
    (r"\bname|\bnaam", ["name"]),
]
_GENERIC = re.compile(
    r"\b(profile|details|information|info|about me|about myself|myself|everything|all about|who am i|account|record|"
    r"mere bare|mere baare|mere baare me|mere bare me|mere bare mai|mere baare mai|mere baare mein|mere bare mein|"
    r"meri details|meri info|meri jankari|mujhe batao|batao mere bare|batao meri details|apni details)\b",
    re.IGNORECASE,
)


def self_profile_fields(question: str) -> list[str] | None:
    """Deterministic detection of 'my phone / my profile / my fine ...' -> vMember columns. None = use the LLM path."""
    if not question:
        return None
    t = question.lower().replace("\u2019", "'").replace("\u201c", "").replace("\u201d", "")
    # Quick Hinglish/Hindi self phrases
    self_hint = re.search(
        r"(mere bare|mere baare|meri details|meri info|mera naam|meri jankari|mujhe batao|batao mere|mere profile|meri profile|apni details|mere baare me|mere bare me|mere bare mai|mere baare mai|mujhe details batao|apni info|about me hindi|meri puri details|mere bare mai batao)",
        t,
    )
    has_self = _SELF_RE.search(t) or self_hint
    if not has_self:
        return None
    # If it explicitly mentions others -> reject
    if _OTHER_PERSON.search(t):
        return None
    # Avoid blocking if clearly self
    if _OTHER_DATA.search(t):
        # reject if it's about other data (history/issues/returns/books etc.) even with 'my'
        return None
    t = re.sub(r"(program|programme|department|dept|course|father'?s?)\s+name", r"\1", t)
    fields: list[str] = []
    for pat, fs in _FIELD_RULES:
        if re.search(pat, t, re.IGNORECASE):
            fields += [f for f in fs if f not in fields]
    # Avoid false positives like "my language", "my books" etc.
    _NON_PROFILE = re.compile(r"\b(language|languages|book|books|author|authors|title|titles|skill|skills|hobby|hobbies|job|occupation|designation)\b", re.IGNORECASE)
    if not fields and _NON_PROFILE.search(t) and not self_hint:
        return None
    if fields:
        return fields
    if _GENERIC.search(t) or self_hint or has_self:
        # If generic but also has non-profile noun with no field match, be conservative
        if not fields and _NON_PROFILE.search(t):
            return None
        return list(SELF_FIELDS)
    return None
