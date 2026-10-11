"""ADR 0015 amendment 3: mechanical, dated awardee -> issuer (CIK) mapping. Pure: no I/O.

Inputs are built by the caller from SEC data only (company names with their validity dates and
last filing date, and the subsidiary entries of Exhibit 21 documents); prices, financial news
and price sites are never used.

A company name ("title") is valid at d if valid_from <= d < valid_to and d is on or before the
CIK's last EDGAR filing (a CIK that stopped filing no longer owns its names).

Rules, in order, for one sole-award paragraph (awardee name, release day d):
1. `jv`: a joint venture ('jv' or 'joint venture' in the normalised name) maps to nothing.
2. `title`: the normalised name equals a normalised title valid at d. Several CIKs ->
   `ambiguous`.
3. `title_head`: the name is 'Legal Entity Corp., Division' (the part before the first comma
   ends with a legal suffix) and that head matches a title as in 2.
4. `unsearchable`: names too short or made only of generic words are not searched further.
5. `ex21`: the normalised name (else its head) equals a normalised subsidiary entry of an
   Exhibit 21 filed in [d - lookback, d): exactly one distinct filer CIK -> that CIK; several
   -> `ambiguous`.
6. `title_prefix`: the normalised name starts (at a word boundary) with a title valid at d that
   is distinctive (>= 2 words not all common/generic, or one uncommon word of >= 6 letters);
   the longest such title wins; several CIKs at that length -> `ambiguous`.
7. `none` otherwise.
Titles are tried for any non-empty, not-all-generic name (so 'KBR Inc.' or '3M Co.' can match
exactly); only the full-text search and the prefix rule need `searchable`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta

from bs4 import BeautifulSoup

from eventtrade.core.dod_parse import COUNTRIES, REGIONS, US_PLACES

LOOKBACK_DAYS = 400
EX21_PARSER_VERSION = 2  # bump when ex21_entries changes: cached entries are then re-parsed

LEGAL_SUFFIXES: tuple[tuple[str, ...], ...] = tuple(sorted(
    (tuple(s.split()) for s in (
        "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "l l c", "lp",
        "l p", "llp", "l l p", "ltd", "limited", "plc", "pllc")),
    key=len, reverse=True))
_SUFFIX_WORDS = frozenset(s[0] for s in LEGAL_SUFFIXES if len(s) == 1)

GENERIC = frozenset("""
and of the for a an in at on
service services solution solutions system systems technology technologies tech
engineering engineers group international industries industry enterprise enterprises
associates consulting consultants construction constructors contractors contracting
management global federal defense defence government corporation company research
development logistics support operations operation products product manufacturing
partners holdings resources sciences science professional professionals integrated
integration specialties specialty solutions innovations innovative strategic analytics
consolidated contracts contract builders building facilities facility maintenance
supply supplies equipment materials training mission readiness infrastructure
environmental architects architecture design designs network networks communications
information data security consultancy partnership worldwide usa us america
aerospace aviation marine medical healthcare health energy electronics electronic
shipbuilding ordnance
""".split())  # noqa: SIM905 (a word list reads better as text)

_DBA = re.compile(r"[,;]?\s*\b(?:doing\s+business\s+as|d/b/a|dba)\b.*$", re.IGNORECASE)
_PAREN = re.compile(r"\([^()]*\)")
# EDGAR state/disambiguation tags at the end of conformed names: 'CORP /DE/', 'CO/', 'INC/NEW/'
_EDGAR_TAG = re.compile(r"(?:\s+/[A-Z]{1,5}/?|/[A-Z]{1,5}/|/)\s*$")
# EDGAR conformed names only (titles), also 'INC/MD', 'CORP/NEW', 'CO /PA/ /NEW/':
_EDGAR_TAG_TITLE = re.compile(r"\s*/[A-Za-z .]{0,12}/?\s*$")
_APOS = re.compile(r"['’`]")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

Titles = dict[str, list[tuple[str, date | None, date | None]]]
Ex21 = dict[str, list[tuple[str, date]]]


def _tokens(name: str) -> list[str]:
    """Case-folded alphanumeric tokens of a name, '&' -> 'and', apostrophes dropped,
    parentheticals and any 'doing business as ...' tail removed."""
    s = _DBA.sub("", str(name))
    while (t := _EDGAR_TAG.sub("", s)) != s:
        s = t
    s = _PAREN.sub(" ", s)
    s = _APOS.sub("", s.casefold().replace("&", " and "))
    return _NON_ALNUM.sub(" ", s).split()


def _strip(tokens: list[str]) -> list[str]:
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    changed = True
    while changed and tokens:
        changed = False
        for suf in LEGAL_SUFFIXES:
            if len(tokens) > len(suf) and tuple(tokens[-len(suf):]) == suf:
                tokens = tokens[:-len(suf)]
                if len(tokens) > 1 and tokens[-1] == "and":  # 'Eli Lilly & Co.'
                    tokens = tokens[:-1]
                changed = True
                break
    if len(tokens) == 1 and (tokens[0],) in LEGAL_SUFFIXES:
        return []
    return tokens


def normalize(name: str) -> str:
    """'The Boeing Co.' -> 'boeing'; 'Kellogg Brown & Root Services, Inc.' -> 'kellogg brown and
    root services'; 'X Co., doing business as Y' -> 'x'; a trailing '*' mark disappears."""
    return " ".join(_strip(_tokens(name)))


def head(name: str) -> str | None:
    """Normalised part before the first comma when it ends with a legal suffix and something
    follows it ('Lockheed Martin Corp., Missiles and Fire Control' -> 'lockheed martin');
    otherwise None."""
    s = _DBA.sub("", str(name))
    if "," not in s:
        return None
    first, rest = s.split(",", 1)
    toks = _tokens(first)
    if len(toks) < 2 or toks[-1] not in _SUFFIX_WORDS or not _tokens(rest):
        return None
    out = " ".join(_strip(toks))
    return out or None


def is_generic(norm: str) -> bool:
    return all(t in GENERIC for t in norm.split())


def searchable(norm: str) -> bool:
    """Worth a full-text search: >= 2 tokens, or 1 token of >= 5 letters; not only generic
    words."""
    toks = norm.split()
    if not toks or is_generic(norm):
        return False
    return len(toks) >= 2 or sum(ch.isalpha() for ch in toks[0]) >= 5


def is_jv(norm: str) -> bool:
    toks = norm.split()
    return "jv" in toks or " joint venture" in f" {norm} "


def ex21_query(norm: str) -> str:
    """EFTS query for a normalised name. EDGAR's index drops '&' but keeps 'and', so a name
    with 'and' is searched both with and without it ('pratt and whitney' also finds 'Pratt &
    Whitney')."""
    toks = norm.split()
    q = f'"{norm}"'
    if "and" in toks and len([t for t in toks if t != "and"]) >= 1:
        q += f' OR "{" ".join(t for t in toks if t != "and")}"'
    return q


def _valid(d: date, start: date | None, end: date | None) -> bool:
    return (start is None or start <= d) and (end is None or d < end)


def title_ciks(norm: str, d: date, titles: Titles) -> set[str]:
    return {cik for cik, a, b in titles.get(norm, ()) if _valid(d, a, b)}


COMMON = GENERIC | frozenset("""
american general national united international pacific western southern northern eastern
atlantic central advanced applied integrated global north south east west first new great
royal standard universal precision superior premier allied continental modern digital
alpha delta omega summit eagle liberty patriot apex pioneer frontier
mountain coastal gulf southwest northwest southeast northeast midwest sierra phoenix titan
atlas vector vertex quantum tactical capital select unified interstate metro city county
state states nation americas world trans tri
""".split())  # noqa: SIM905 (a word list reads better as text)


def _one(hit: set[str], rule: str) -> tuple[str | None, str] | None:
    if len(hit) == 1:
        return next(iter(hit)), rule
    return (None, "ambiguous") if hit else None


def _title_stage(awardee: str, norm: str, d: date, titles: Titles) -> tuple[str | None, str] | None:
    if is_generic(norm):
        return None
    got = _one(title_ciks(norm, d, titles), "title")
    if got:
        return got
    h = head(awardee)
    if h and h != norm and not is_generic(h):
        return _one(title_ciks(h, d, titles), "title_head")
    return None


def ex21_phrases(awardee: str, d: date, titles: Titles) -> list[str]:
    """Normalised phrases whose Exhibit 21 entries `resolve` would consult for this awardee at
    d: [] if a title resolves it (or it is a JV / unsearchable), else the name and its head."""
    norm = normalize(awardee)
    if not norm or is_jv(norm) or _title_stage(awardee, norm, d, titles) or not searchable(norm):
        return []
    h = head(awardee)
    return [norm] + ([h] if h and h != norm and searchable(h) else [])


def prefix_qualifies(title_norm: str) -> bool:
    """A title distinctive enough to own every name that starts with it."""
    toks = title_norm.split()
    if not toks or all(t in COMMON for t in toks):
        return False
    return len(toks) >= 2 or sum(ch.isalpha() for ch in toks[0]) >= 6


def prefix_ciks(norm: str, d: date, titles: Titles) -> set[str]:
    """CIKs of the longest distinctive title valid at d that is a proper word prefix of norm."""
    toks = norm.split()
    for k in range(len(toks) - 1, 0, -1):
        key = " ".join(toks[:k])
        if key in titles and prefix_qualifies(key):
            hit = title_ciks(key, d, titles)
            if hit:
                return hit
    return set()


def resolve(awardee: str, d: date, titles: Titles, ex21: Ex21,
            lookback_days: int = LOOKBACK_DAYS) -> tuple[str | None, str]:
    """(cik, rule) for one sole awardee on release day d; see the module docstring.
    `ex21` maps a normalised Exhibit 21 entry to its (filer CIK, file date) list."""
    norm = normalize(awardee)
    if not norm:
        return None, "unsearchable"
    if is_jv(norm):
        return None, "jv"
    got = _title_stage(awardee, norm, d, titles)
    if got:
        return got
    if not searchable(norm):
        return None, "unsearchable"
    lo = d - timedelta(days=lookback_days)
    prefix = prefix_ciks(norm, d, titles)
    for key in ex21_phrases(awardee, d, titles):
        got = _one({cik for cik, fd in ex21.get(key, ()) if lo <= fd < d}, "ex21")
        if got:
            if got[0] is not None and prefix and got[0] not in prefix:
                return None, "ambiguous"   # conflicting evidence (e.g. a JV partner in EX-21)
            return got
    got = _one(prefix, "title_prefix")
    return got or (None, "none")


def build_titles(rows: Iterable[tuple]) -> Titles:
    """(cik, name, valid_from, valid_to[, last_filing]) rows (ISO dates or None) ->
    {norm: [(cik, from, end)]}, end = the earlier of valid_to and the day after the CIK's last
    filing. Names that normalise to nothing or to generic words only are skipped."""
    out: Titles = {}
    for row in rows:
        cik, name, a, b = row[:4]
        last = _d(row[4]) if len(row) > 4 else None
        s = str(name or "")
        while (t := _EDGAR_TAG_TITLE.sub("", s)) != s:
            s = t
        norm = normalize(s)
        if not norm or is_generic(norm):
            continue
        start, end = _d(a), _d(b)
        if last is not None:
            stop = last + timedelta(days=1)
            end = stop if end is None else min(end, stop)
        if start is not None and end is not None and start >= end:
            continue
        out.setdefault(norm, []).append((str(cik), start, end))
    return out


def build_ex21(rows: Iterable[tuple[str, str, str]]) -> Ex21:
    """(entry_norm, cik, file_date) rows -> {entry_norm: [(cik, file_date)]}."""
    out: Ex21 = {}
    for phrase, cik, fd in rows:
        if fd:
            out.setdefault(phrase, []).append((str(cik), date.fromisoformat(str(fd)[:10])))
    return out


# ---- Exhibit 21 documents -> normalised subsidiary entries -------------------------------

_EXTRA_JURIS = ("cayman islands", "bermuda", "luxembourg", "china", "prc",
                "peoples republic of china", "british virgin islands", "bvi", "hong kong sar",
                "england and wales", "scotland", "jersey", "guernsey", "isle of man", "panama",
                "liberia", "marshall islands", "barbados", "bahamas", "curacao", "netherlands",
                "singapore", "dc", "us", "u s a", "u s")
_JURIS = tuple(sorted({tuple(_NON_ALNUM.sub(" ", j.casefold()).split())
                       for j in (*US_PLACES, *COUNTRIES, *REGIONS, *_EXTRA_JURIS)} - {()},
                      key=len, reverse=True))
_JURIS_SET = frozenset(_JURIS)
_BLOCKS = ["td", "th", "p", "li", "div", "pre", "dt", "dd"]
_LINE_SPLIT = re.compile(r"\t|\s{2,}|\s[-–—]\s|\.{3,}|_{3,}|\|")
_NUMBERING = re.compile(r"^\s*(?:\(?\d{1,3}[.)]|[•·*\-–—])\s+")
_PCT = re.compile(r"\d+(?:\.\d+)?\s*%")
_MAX_ENTRY_TOKENS = 15


def _entry_variants(piece: str) -> set[str]:
    piece = _PCT.sub(" ", _NUMBERING.sub("", piece)).strip(" *†‡#")
    out: set[str] = set()
    if not piece or len(piece) > 250:
        return out
    variants = {piece}
    if "," in piece:  # 'X Corp., a Delaware corporation' / 'X Corp., Delaware', not 'Harris, Y Inc.'
        first, tail = piece.split(",", 1)
        tail_toks = _tokens(tail)
        if tail_toks and (tail_toks[0] in ("a", "an") or tuple(tail_toks) in _JURIS_SET):
            variants.add(first)
    for v in variants:
        toks = _tokens(v)
        if not toks or len(toks) > _MAX_ENTRY_TOKENS:
            continue
        out.add(" ".join(_strip(toks)))
        for j in _JURIS:  # 'Electric Boat Corporation Delaware' -> 'electric boat'
            if len(toks) > len(j) + 1 and tuple(toks[-len(j):]) == j \
                    and toks[-len(j) - 1] in _SUFFIX_WORDS:
                out.add(" ".join(_strip(toks[:-len(j)])))
                break
    out.discard("")
    return out


def ex21_entries(doc: str) -> set[str]:
    """Normalised subsidiary names of one Exhibit 21 document (HTML or plain text): each table
    cell, paragraph or text line, split on column gaps (tabs, runs of spaces, ' - ', dot
    leaders) into its first column, without numbering, bullets, percentages, footnote marks,
    parenthesised tags ('(Delaware)'), a ', a Delaware corporation' / ', Delaware' tail, or a
    jurisdiction after a legal suffix. Header and jurisdiction cells give junk entries, which
    match no awardee name; text after ' - ' or after a comma that is not a jurisdiction never
    becomes an entry of its own ('KW Fund VI - Raytheon LLC', 'Harris, Rothenberg Inc.')."""
    if re.search(r"<(?:html|body|table|p|div|tr|font|pre)\b", doc[:5000], re.IGNORECASE):
        soup = BeautifulSoup(doc, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        texts = [el.get_text("") for el in soup.find_all(_BLOCKS)
                 if not el.find([*_BLOCKS, "table", "tr"])]
        if not texts:
            texts = [soup.get_text("\n")]
    else:
        texts = [doc]
    out: set[str] = set()
    for text in texts:
        for line in text.replace("\xa0", " ").splitlines():
            line = line.strip()
            if not line:
                continue
            # the whole line, and its first column (name) when column gaps, ' - ' or dot leaders
            # separate it from a jurisdiction; later columns are never names
            cols = [c for c in _LINE_SPLIT.split(line) if re.search(r"[A-Za-z]", c)]
            for piece in {line, *cols[:1]}:
                out |= _entry_variants(piece)
    return out


def _d(s: str | None) -> date | None:
    if s is None or isinstance(s, float) or str(s).strip() in ("", "None"):  # None, NaN, ''
        return None
    return date.fromisoformat(str(s)[:10])
