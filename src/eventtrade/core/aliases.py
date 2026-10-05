"""Company legal name -> lowercase alias strings as they appear in GKG V2Organizations.

Pure. The rules are judgement calls; tests/core/alias_cases.py is their specification and
docs/experiments/0002-gkg-org-alias-precision.md is the measurement they were tuned on.
Aliases are meant for exact equality against an org string with its ",offset" removed.
"""

import re
import unicodedata

# Trailing tokens stripped from a name to reach its core.
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "limited", "plc", "holdings", "holding", "group", "llc", "lp",
})
# Strong suffixes appended to the core, and to any strip stage ending in a weak suffix
# ("unitedhealth group" -> "unitedhealth group inc"). GKG often keeps the suffix ("apple inc").
# "group"/"holdings" are never invented: "cisco holdings", "brunswick group", "reliance group"
# are other companies (experiment 0002).
_SUFFIX_FORMS = ("inc", "corp", "corporation", "co", "company", "companies", "plc", "ltd")
_WEAK_SUFFIXES = frozenset({"group", "holdings", "holding"})
# A weak suffix, or "co", on an ambiguous core is still generic ("news group", "news co").
_GENERIC_ON_AMBIGUOUS = _WEAK_SUFFIXES | {"co"}
# Brands no rule derives from the legal name, keyed by core. Extend only with measured evidence.
_BRAND_EXTRAS: dict[str, tuple[str, ...]] = {
    "alphabet": ("google",),
    "walt disney": ("disney",),
    "meta platforms": ("meta",),
    "exxonmobil": ("exxon mobil", "exxon"),
}
_INITIALISM_STOPWORDS = frozenset({"of", "and", "the"})

# Aliases that are ordinary words, places, person names or mostly incidental mentions. Grown
# from the cores of the S&P 500 and 400 lists and the URL sample of experiment 0002; an alias in
# this set should not be matched without context.
_AMBIGUOUS = frozenset({
    # ordinary English words
    "apple", "target", "gap", "ball", "visa", "oracle", "discovery", "block", "progressive",
    "southern", "booking", "news", "meta", "fox", "match", "snap", "square", "mosaic", "arch",
    "marathon", "travelers", "united", "general", "american", "first", "public", "dominion",
    "carrier", "crown", "best", "regions", "principal", "intuitive", "pool", "insight", "flex",
    "align", "steel", "equity", "gen", "coherent", "vertex", "monster", "global", "edge",
    "trust", "national", "dollar", "progress", "range", "ally", "valley", "power", "energy",
    "republic", "universal", "realty", "post", "builders", "hub",
    # places, rivers, mountains
    "amazon", "hartford", "corning", "everest", "cincinnati", "texas", "boston", "carolina",
    "phoenix", "arrow", "old dominion", "summit",
    # experiment 0003: "eaton" (Eaton Canyon) matched the January 2025 Eaton Fire, 12,396 docs
    # in one week against ~100 a week before; "eaton corp(oration)" stays
    "eaton",
    # surnames and given names (GKG can label persons as organisations)
    "ford", "lowe", "emerson", "parker", "cooper", "johnson", "morgan", "henry", "gallagher",
    "hunt", "smith", "harris", "edison", "duke", "williams", "warner", "chase", "lilly",
    "wells", "graham", "jackson", "hamilton", "taylor", "ross",
    # generic phrases and street names
    "waste management", "state street", "iron mountain",
    # in GKG these are almost always an index or a military rank, not the company
    "adm",
    # cited as a source or authority far more often than reported on (see also _CITED_ANY_FORM)
    "new york times", "moody",
    # named incidentally (a platform, a venue, boilerplate) in most articles that list them:
    # about 1-2 of 12 sampled articles were about the company (experiment 0002)
    "google", "netflix", "hyatt",
    # possessive stems that are mostly surnames ("michael mcdonald", "luke mcdonald")
    "mcdonald",
    # other generic matches seen in experiment 0002
    "american international", "crown corporation", "ryder",
})
# Cores cited as a market, source or data vendor in boilerplate that itself carries the legal
# suffix ("source: Morningstar Inc", "Nasdaq Inc" listing notices). Ambiguous bare AND with any
# trailing legal suffix. Experiment 0003 dev counts, mostly not about the company: morningstar inc
# 138k, nasdaq inc 77k, gartner inc 47k docs. Extend only with measured evidence.
_CITED_ANY_FORM = frozenset({"nasdaq", "gartner", "morningstar"})


def make_aliases(name: str, ticker: str) -> list[str]:
    """Lowercase GKG-style aliases of a company name; the first is the bare core."""
    forms: list[str] = []
    cores: list[str] = []
    suffixable: list[str] = []
    for text in _text_forms(name):
        tokens = _drop_leading_the(text.split())
        stages = _strip_stages(tokens)
        cores.append(stages[-1])
        forms += [stages[-1], *stages[:-1]]
        suffixable += [stages[-1]] + [st for st in stages if st.split()[-1] in _WEAK_SUFFIXES]
    out = list(forms)
    for core in cores:
        out += list(_BRAND_EXTRAS.get(core, ()))
    out += [f"{st} {s}" for st in suffixable for s in _SUFFIX_FORMS]
    initialism = _initialism_alias(ticker, forms)
    if initialism:
        out.append(initialism)
    return list(dict.fromkeys(a for a in out if a))


def is_ambiguous(alias: str) -> bool:
    """True if the alias is a common word, place, person, incidental mention, or too short."""
    a = " ".join(alias.lower().split())
    if a in _AMBIGUOUS or _core_of(a) in _CITED_ANY_FORM:
        return True
    head, _, last = a.rpartition(" ")
    if last in _GENERIC_ON_AMBIGUOUS and head in _AMBIGUOUS:
        return True
    return " " not in a and len(a) <= 2


def _core_of(alias: str) -> str:
    """The alias with every trailing legal suffix removed (never down to nothing)."""
    tokens = alias.split()
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _text_forms(name: str) -> list[str]:
    """Every normalised spelling: non-ASCII folding x '&' x apostrophe x hyphen variants."""
    out: list[str] = []
    for base in _bases(name):
        amps = (" ", " and ") if "&" in base else (" ",)
        for amp in amps:
            for text in _apostrophe_forms(base):
                for hyph in ((False, True) if "-" in text else (False,)):
                    full = _normalise(text, amp, hyphen_to_space=hyph)
                    if full:
                        out.append(full)
    return list(dict.fromkeys(out))


def _bases(name: str) -> list[str]:
    """Class markers removed, parentheticals placed, and the two non-ASCII foldings."""
    s = unicodedata.normalize("NFKC", name).replace("–", "-").replace("—", "-")
    s = re.sub(r"\bclass\s+[a-z]\b", " ", s, flags=re.IGNORECASE)
    front: list[str] = []

    def _take(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if inner:
            front.append(inner)
        return " "

    s = re.sub(r"\(([^)]*)\)", _take, s)
    s = " ".join(front + [s]).lower()
    folded = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    folded = folded.encode("ascii", "ignore").decode()
    dropped = s.encode("ascii", "ignore").decode()
    return list(dict.fromkeys([folded, dropped]))


def _apostrophe_forms(s: str) -> list[str]:
    """Apostrophes removed; a possessive 's also yields the bare stem."""
    s = s.replace("’", "'")
    plain = s.replace("'", "")
    stem = re.sub(r"'s\b", "", s).replace("'", "")
    return list(dict.fromkeys([plain, stem]))


def _normalise(s: str, amp: str, hyphen_to_space: bool) -> str:
    s = s.replace("&", amp)
    # dotted initials ("u.s.", "a. o.", "j.") collapse into one token
    s = re.sub(r"\b(?:[a-z]\.\s*)+", lambda m: re.sub(r"[.\s]", "", m.group(0)) + " ", s)
    s = s.replace(".", " ")
    if hyphen_to_space:
        s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9\- ]", " ", s)
    tokens = [t.strip("-") for t in s.split()]
    return " ".join(t for t in tokens if t)


def _drop_leading_the(tokens: list[str]) -> list[str]:
    return tokens[1:] if len(tokens) > 1 and tokens[0] == "the" else tokens


def _strip_stages(tokens: list[str]) -> list[str]:
    """The name after each legal-suffix strip, longest first; the last is the core."""
    t = list(tokens)
    stages = [" ".join(t)]
    while len(t) > 1 and (t[-1] in _LEGAL_SUFFIXES or t[-1] == "and"):
        t.pop()
        if t[-1] != "and":
            stages.append(" ".join(t))
    if stages[-1] != " ".join(t):
        stages.append(" ".join(t))
    return stages


def _initialism_alias(ticker: str, forms: list[str]) -> str | None:
    """The ticker, if it has >= 3 letters and spells the initials of one of the name forms."""
    t = re.sub(r"[^a-z0-9]", "", ticker.lower().split(".")[0])
    if len(t) < 3:
        return None
    for form in forms:
        words = [w for w in form.split() if w not in _INITIALISM_STOPWORDS]
        if len(words) == len(t) and "".join(w[0] for w in words) == t:
            return t
    return None
