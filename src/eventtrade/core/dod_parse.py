"""Parse one US Department of Defense daily contract announcement (defense.gov "Contracts For
<Month> <D>, <YYYY>" article, as archived by the Wayback Machine) into its awards. Pure.

The article body (`div.body`) is a run of paragraphs: agency headings (centred, upper case:
ARMY, NAVY, AIR FORCE, DEFENSE LOGISTICS AGENCY, ...) each followed by one paragraph per award:

    Lockheed Martin Corp., Fort Worth, Texas, is awarded a $1,234,567,890 modification (P00012)
    to ...

Multiple-award paragraphs list the companies before the verb, separated by semicolons
("A Inc.,* City, State (N00024-25-D-4325); and B Corp., City, State (...), are awarded ...").
A '*' after a name marks a small business. CORRECTION / UPDATE paragraphs are kept with kind
'correction' and no awardees. A paragraph cut mid-award by the page markup ('Work will be
performed ...' on its own) is joined back to the award above it.

Each award: seq (0-based, article order), agency (heading above it, upper case; None before the
first heading), awardees (names as written, location and contract-number parentheticals removed),
amount (first '$' amount, float, None if none), kind (contract / modification / order / other /
correction), small_business (a '*' marks an awardee), text (the paragraph).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_TITLE_DATE = re.compile(r"contracts\s+for\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", re.IGNORECASE)
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_PUBLISHED_META = (("itemprop", "datePublished"), ("property", "article:published_time"),
                   ("name", "DC.date.issued"), ("name", "date"))

_VERB = re.compile(
    r"\b(?:is|are|was|were|has|have|had|will\s+be)\s+(?:[a-z]+\s+){0,3}?"
    r"(?:awarded|issued|selected|modified|received|receiving|exercised|granted)\b"
    r"|,\s*\*?\s*(?:awarded|issued|received|receives|wins|won)\b", re.IGNORECASE)
# Fallback end of the subject, before the first '$': ", will compete for each order of an $X".
_PREDICATE = re.compile(r"[,);]\s*\*?\s+(?:will|is|are|was|were|has|have|had)\s",
                        re.IGNORECASE)
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\s?\d{3})+|\d+)(\.\d+)?(?:\s+(million|billion))?", re.IGNORECASE)
# 'option' (exercised on an existing contract) is a modification; 'options', 'option years' and
# 'option contract' describe a new contract.
_KIND_WORDS = re.compile(r"\b(?:modifications?|modified|option(?!s|\s+years?\b|\s+contract)|"
                         r"orders?|contracts?|agreements?)\b", re.IGNORECASE)
_KIND_WINDOW = 250
_PAREN_DIGIT = re.compile(r"\s*\([^()]*\d[^()]*\)")
_HEADING = re.compile(r"^[A-Z0-9][A-Z0-9 .,&'()/\-]*$")
# Text that never precedes the award verb: the verb found lies past the first sentence.
_NOT_SUBJECT = re.compile(r"\bwork will be\b|\bfiscal\s+(?:year\s+)?\d{4}\b|contracting activity"
                          r"|\bis expected\b|\bfunds in the amount\b|\bpercent\)",
                          re.IGNORECASE)
_TERMINAL = re.compile(r"[.!?)\"”:;]\s*$")
_DOLLARS = re.compile(r",?\s*\$\s?\d[\d,\s]*(?:\.\d+)?(?:\s+(?:million|billion))?", re.IGNORECASE)
_NOTE = re.compile(r"^\*?\s*small\s+business", re.IGNORECASE)
_CORRECTION = re.compile(r"^\s*(?:correct(?:ions?|ed|on)|updates?|clarifications?)\b",
                         re.IGNORECASE)
_LIST_INTRO = re.compile(
    r"^\s*(?:the\s+)?(?:following\b|(?:two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|\d+)\s+(?:\w+\s+)?(?:companies|contractors|firms|vendors|offerors)\b)", re.IGNORECASE)

US_STATES = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming"})
US_PLACES = US_STATES | {"guam", "district of columbia", "d.c", "washington d.c", "puerto rico",
                         "virgin islands", "u.s. virgin islands", "american samoa",
                         "northern mariana islands"}
# Regions written between a foreign city and its country ("Lara, Victoria, Australia").
REGIONS = frozenset({
    "ontario", "quebec", "quebec province", "british columbia", "alberta", "nova scotia",
    "new brunswick", "manitoba", "saskatchewan", "newfoundland", "victoria", "new south wales",
    "queensland", "south australia", "western australia", "tasmania", "okinawa", "tokyo",
    "kanagawa", "aomori", "nagasaki", "yamaguchi", "hiroshima", "fukuoka", "miyazaki", "osaka",
    "naples", "merseyside", "lancashire", "hampshire", "bavaria", "hesse", "rhineland-palatinate",
    "baden-wurttemberg", "gyeonggi-do", "seoul"})
COUNTRIES = frozenset({
    "afghanistan", "australia", "austria", "bahrain", "belgium", "canada", "colombia", "denmark",
    "djibouti", "egypt", "england", "finland", "france", "germany", "greece", "india", "iraq",
    "israel", "italy", "japan", "jordan", "kenya", "kuwait", "netherlands", "the netherlands",
    "new zealand", "norway", "oman", "philippines", "poland", "portugal", "qatar", "romania",
    "saudi arabia", "scotland", "singapore", "south korea", "republic of korea", "korea", "spain",
    "sweden", "switzerland", "taiwan", "thailand", "turkey", "united arab emirates",
    "united kingdom", "wales", "ukraine", "bulgaria", "czech republic", "estonia", "latvia",
    "lithuania", "hungary", "slovakia", "brazil", "mexico", "chile", "peru", "pakistan",
    "indonesia", "malaysia", "vietnam", "honduras", "el salvador", "kosovo", "cuba", "greenland",
    "iceland", "ireland", "northern ireland", "morocco", "tunisia", "niger", "nigeria", "ghana",
    "uganda", "ethiopia", "somalia", "cyprus", "malta", "croatia", "serbia", "bosnia",
    "georgia (country)", "kazakhstan", "kyrgyzstan", "uzbekistan", "mongolia", "lebanon",
    "south africa", "argentina", "hong kong", "united states", "usa", "u.k", "uk"})


def parse_dod_header(html: str) -> tuple[date | None, bool]:
    """(release date, is a 'Contracts For' article) without reading the awards: callers check
    the date against the hold-out before parsing anything else."""
    soup = BeautifulSoup(html, "html.parser")
    title_date = _title_date(soup)
    return title_date or _published_date(soup), title_date is not None


def parse_dod_article(html: str) -> tuple[date | None, list[dict[str, Any]]]:
    """(release date, awards) of one article's HTML. The date comes from the 'Contracts For'
    title (h1, og:title or <title>), else from a published-date meta; None if neither parses.
    Awards are returned only for a 'Contracts For' article (an empty list otherwise)."""
    soup = BeautifulSoup(html, "html.parser")
    title_date = _title_date(soup)
    release = title_date or _published_date(soup)
    if title_date is None:
        return release, []
    body = soup.find("div", class_="body") or soup.find(class_=re.compile(r"\bbody\b")) or soup
    return release, _awards(body)


def _title_date(soup: BeautifulSoup) -> date | None:
    cands = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])]
    og = soup.find("meta", attrs={"property": "og:title"})
    if og is not None:
        cands.append(str(og.get("content") or ""))
    if soup.title is not None:
        cands.append(soup.title.get_text(" ", strip=True))
    for c in cands:
        m = _TITLE_DATE.search(c)
        if m and (d := _mk_date(m.group(3), m.group(1), m.group(2))):
            return d
    return None


def _published_date(soup: BeautifulSoup) -> date | None:
    for attr, val in _PUBLISHED_META:
        tag = soup.find("meta", attrs={attr: val})
        if tag is not None and (m := _ISO_DATE.search(str(tag.get("content") or ""))):
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def _mk_date(y: str, mon: str, d: str) -> date | None:
    month = _MONTHS.get(mon[:3].lower())
    if month is None:
        return None
    try:
        return date(int(y), month, int(d))
    except ValueError:
        return None


def _clean(s: str) -> str:
    s = s.replace("\xa0", " ").replace("‐", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", s).strip()


def _blocks(body: Tag) -> list[tuple[str, bool]]:
    """(text, centred) of each paragraph-like block in reading order."""
    out = []
    for el in body.find_all(["p", "h2", "h3", "h4", "h5", "h6"]):
        if el.find_parent(["p"]) is not None:
            continue
        txt = _clean(el.get_text(" "))
        if not txt:
            continue
        style = (el.get("style") or "").replace(" ", "").lower()
        centred = "text-align:center" in style or el.name != "p"
        out.append((txt, centred))
    if not out:  # a body without paragraph markup: split on blank lines / <br>
        for part in re.split(r"\n\s*\n", body.get_text("\n")):
            if txt := _clean(part):
                out.append((txt, False))
    return out


def _is_heading(text: str, centred: bool) -> bool:
    t = text.strip(" *:")
    if not t or len(t) > 80 or "$" in t or not _HEADING.match(t):
        return False
    return centred or len(t.split()) <= 6


def _awards(body: Tag) -> list[dict[str, Any]]:
    groups: list[list[Any]] = []  # [agency, text]; a paragraph cut mid-sentence is re-joined
    agency: str | None = None
    joinable = False
    for text, centred in _blocks(body):
        if _is_heading(text, centred):
            t = text.strip(" *:")
            if t.upper() not in ("CONTRACTS", "CONTRACT"):
                agency = t
            joinable = False
            continue
        if _NOTE.match(text):
            joinable = False
            continue
        if joinable and _continues(text, groups[-1][1]):
            groups[-1][1] += " " + text
            continue
        if agency is None and not _VERB.search(text):
            continue
        groups.append([agency, text])
        joinable = True
    awards: list[dict[str, Any]] = []
    for ag, text in groups:
        aw = parse_award(text)
        if aw is not None:
            aw.update(seq=len(awards), agency=ag)
            awards.append(aw)
    return awards


def _continues(text: str, previous: str) -> bool:
    """A paragraph that continues the previous award: it starts in lower case, or it carries no
    award verb near its start and either the previous paragraph stops mid-sentence or it reads
    like the tail of an award ('Work will be performed ...', 'Fiscal 2018 ... funds')."""
    if text[:1].islower():
        return True
    if _CORRECTION.match(text):
        return False
    head = text[:400]
    if _VERB.search(head) or re.search(r"\bawarding\b", head, re.IGNORECASE):
        return False
    return not _TERMINAL.search(previous) or bool(_NOT_SUBJECT.search(head))


def parse_award(text: str) -> dict[str, Any] | None:
    """One award paragraph -> awardees, amount, kind, small_business, text; None for a paragraph
    that is neither an award nor a correction (no award verb and no '$' amount)."""
    amount, amt_end = _amount(text)
    if _CORRECTION.match(text):
        return {"awardees": [], "amount": amount, "kind": "correction",
                "small_business": False, "text": text}
    verb = _VERB.search(text)
    if verb is None or _NOT_SUBJECT.search(text[:verb.start()]):
        # no award verb (or a misspelt one): ", will compete for each order of an $X ..."
        pred = _PREDICATE.search(text)
        first_dollar = text.find("$")
        if pred is not None and (first_dollar < 0 or pred.start() < first_dollar):
            verb = pred
    if verb is None and amount is None:
        return None
    subject = text[:verb.start() + (verb.group(0)[0] == ")")] if verb else ""
    names, small = _awardees(subject)
    if not names and verb is not None and ":" in text[verb.end():]:
        # "The following N companies have been awarded ...: A, City, State (..); B, ..."
        tail = text[verb.end():].split(":", 1)[1]
        names, small = _awardees(tail.split(". ")[0] if ";" in tail else "")
    if not names and verb is not None:
        # "Two companies have been awarded contracts ... . Columbia Helicopters Inc., Aurora,
        # Oregon, was awarded ...": every later sentence whose subject is a company + location
        for later in _VERB.finditer(text, verb.end()):
            sent = _last_sentence(text[:later.start()])
            if len(sent) > 400 or _NOT_SUBJECT.search(sent) or not any(
                    _is_place(p) or _city_state(p) for p in sent.split(",")):
                continue
            more, sb = _awardees(sent)
            names += [n for n in more if n not in names]
            small = small or sb
    kind = _kind(text[verb.end():verb.end() + _KIND_WINDOW]) if verb is not None else "other"
    if kind == "other" and amount is not None:
        kind = _kind(text[amt_end:amt_end + _KIND_WINDOW])
    return {"awardees": names, "amount": amount, "kind": kind, "small_business": small,
            "text": text}


_ABBREV = frozenset({"inc", "co", "corp", "ltd", "llc", "jr", "sr", "st", "ft", "mt", "u.s",
                     "no", "dr", "bros", "assoc", "intl", "jan", "feb", "aug", "sept", "oct",
                     "nov", "dec"})


def _last_sentence(text: str) -> str:
    """Text after the last sentence end ('. ' + capital, not after Inc., Co., U.S., A. ...)."""
    start = 0
    for m in re.finditer(r"\.\s+(?=[A-Z])", text):
        word = re.split(r"[\s,(]", text[:m.start()])[-1].lower()
        if word in _ABBREV or len(word) <= 1:
            continue
        start = m.end()
    return text[start:]


def _amount(text: str) -> tuple[float | None, int]:
    m = _AMOUNT.search(text)
    if m is None:
        return None, 0
    v = float(m.group(1).replace(",", "").replace(" ", "") + (m.group(2) or ""))
    scale = (m.group(3) or "").lower()
    if v < 1e6:   # "$49,000,000 million" is a typo in the release: the digits are the amount
        v *= 1e9 if scale == "billion" else 1e6 if scale == "million" else 1.0
    return v, m.end()


def _kind(window: str) -> str:
    """contract / modification / order / other: the first of those nouns in the words that
    follow the award verb, through the amount ("a $5 delivery order against contract ..." is an
    order); "contract (action) modification" is a modification."""
    # multiple-award IDIQs: "..., will compete for each order of the $X ... contract"
    window = re.sub(r"^\s*compete\s+for\s+(?:each|individual|future)\s+(?:task\s+|delivery\s+)?"
                    r"orders?\s+(?:of|under)\b", "", window, flags=re.IGNORECASE)
    m = _KIND_WORDS.search(window)
    if m is None:
        return "other"
    w = m.group(0).lower()
    if w.startswith(("modifi", "option")):  # "a $9 cost-plus-fixed-fee option to ..."
        return "modification"
    if w.startswith("order"):
        return "order"
    if re.match(r"\s*(?:action\s+)?modification", window[m.end():], re.IGNORECASE):
        return "modification"
    return "contract"


def _awardees(subject: str) -> tuple[list[str], bool]:
    """Company names (as written, without location) of the text before the award verb."""
    subject = _DOLLARS.sub("", subject)  # "X, City, State (N-1), $1,011,111,314; Y, ..."
    if ":" in subject:  # "... the actual award date is today: Tetra Tech/Pond JV, Louisville, ..."
        subject = subject.rsplit(":", 1)[1]
    subject = re.sub(r"[,\s]*\b(?:each|both|all|jointly|separately)\s*$", "", subject.strip(),
                     flags=re.IGNORECASE).rstrip(", ")
    if not subject or _LIST_INTRO.search(subject) and ";" not in subject:
        return [], False
    small = "*" in subject
    names: list[str] = []
    # a contract-number parenthetical ends an entry (a missing ';' in the source is common)
    for entry in _PAREN_DIGIT.sub(";", subject).split(";"):
        entry = re.sub(r"^[\s,]*(?:and|&)\s+", "", entry).replace("*", "")
        parts = [p.strip() for p in entry.split(",") if p.strip()]
        if not parts:
            continue
        # the location is 'City, [Region,] State|Country': cut before the city of the first
        # place; 'Irvine California' (comma missing) is a whole location
        # ('Washington, D.C.', 'New York, New York': a city named like a state comes first)
        place = next((j for j in range(1, len(parts)) if _is_place(parts[j]) and not (
            j + 1 < len(parts) and _norm_place(parts[j + 1]) in US_PLACES)), None)
        city_state = next((j for j in range(1, len(parts)) if _city_state(parts[j])), None)
        if city_state is not None and (place is None or city_state < place - 1):
            parts = parts[:city_state]
        elif place is not None:
            if place == 1 and len(parts) == 2 and names:
                continue  # 'Marietta, Georgia': another location of the previous company
            parts = parts[:max(1, place - 1)]
        elif len(parts) >= 3:
            parts = parts[:-2]
        name = ", ".join(parts).strip(" ,")
        if name and not (names and _is_place(name)):
            names.append(name)
    return names, small


def _norm_place(s: str) -> str:
    return re.sub(r"\s*\(.*\)$", "", s.strip(" .*").lower())


def _is_place(s: str) -> bool:
    s = _norm_place(s)
    return s in US_PLACES or s in COUNTRIES or s in REGIONS


def _city_state(s: str) -> bool:
    """'Stratford Connecticut', 'Epping New Hampshire': a city and US state without a comma."""
    words = _norm_place(s).split()
    if {"of", "and", "the", "for", "&"} & set(words):  # 'University of Washington'
        return False
    return 2 <= len(words) <= 5 and any(
        " ".join(words[-k:]) in US_STATES and len(words) > k for k in (1, 2))
