"""Case table for core/aliases.py — this IS the specification.

GKG V2Organizations strings (experiment 0002) are lowercase, carry no dots, apostrophes or '&'
(the character is dropped and the gap collapses to one space), keep hyphens, and very often keep a
legal suffix ("apple inc", "goldman sachs group inc", "jpmorgan chase co"). Bare common-word
brands ("apple", "amazon", "target", "ford") did not occur at all on the measured day, so the
suffixed forms are what actually matches. Aliases are compared to org strings by exact equality.

Each MAKE case: (name, ticker, must_include, must_exclude). `must_include` must all be present in
make_aliases(name, ticker); `must_exclude` must all be absent. The first alias is the primary
(the bare core) and is pinned separately in PRIMARY_CASES.
"""

# --- make_aliases ---------------------------------------------------------------------------

PRIMARY_CASES: list[tuple[str, str, str]] = [
    # The first alias is the core: lowercase, legal suffixes and a leading 'the' removed.
    ("Apple Inc.", "AAPL", "apple"),
    ("The Home Depot, Inc.", "HD", "home depot"),
    ("JPMorgan Chase & Co.", "JPM", "jpmorgan chase"),
    ("Microsoft", "MSFT", "microsoft"),
    ("Alphabet Inc. (Class A)", "GOOGL", "alphabet"),
    ("Coca-Cola Company (The)", "KO", "coca-cola"),
    ("Lilly (Eli)", "LLY", "eli lilly"),
    ("U.S. Bancorp", "USB", "us bancorp"),
    ("McDonald's", "MCD", "mcdonalds"),
]

MAKE_CASES: list[tuple[str, str, set[str], set[str]]] = [
    # Legal suffixes (inc, incorporated, corp, corporation, co, company, companies, ltd, limited,
    # plc, holdings, holding, group, llc, lp) are stripped from the end, repeatedly, never down to
    # nothing. The full normalised name is kept too, since GKG often reports it verbatim.
    ("Apple Inc.", "AAPL", {"apple", "apple inc"}, {"apple inc."}),
    ("Oracle Corporation", "ORCL", {"oracle", "oracle corporation", "oracle corp"}, set()),
    ("Chubb Limited", "CB", {"chubb", "chubb limited", "chubb ltd"}, set()),
    ("Aon plc", "AON", {"aon", "aon plc"}, set()),
    ("Goldman Sachs Group, Inc.", "GS", {"goldman sachs", "goldman sachs group", "goldman sachs group inc"}, {"goldman"}),
    ("United Airlines Holdings", "UAL", {"united airlines", "united airlines holdings"}, set()),
    ("TKO Group Holdings", "TKO", {"tko", "tko group holdings"}, set()),
    ("Hubbell Incorporated", "HUBB", {"hubbell", "hubbell inc"}, set()),
    ("Co", "CO", {"co"}, set()),  # never strip the last token

    # Every core also gets the strong suffixed forms GKG actually emits: inc, corp, corporation,
    # co, company, companies, plc, ltd. A bare common-word core rarely appears in GKG at all;
    # "tesla inc" / "target corporation" do. A stage ending in group/holdings gets them too.
    ("Tesla, Inc.", "TSLA", {"tesla", "tesla inc", "tesla corp", "tesla co", "tesla company"}, set()),
    ("Target Corporation", "TGT", {"target", "target corporation", "target corp", "target inc"}, set()),
    ("Microsoft", "MSFT", {"microsoft corp", "microsoft corporation"}, set()),
    ("UnitedHealth Group", "UNH", {"unitedhealth", "unitedhealth group", "unitedhealth group inc"}, set()),
    ("Lowe's", "LOW", {"lowe companies", "lowes companies"}, set()),

    # "group" and "holdings" are never invented: on the measured day "cisco holdings",
    # "brunswick group" and "reliance group" were other companies.
    ("Cisco", "CSCO", {"cisco", "cisco inc"}, {"cisco holdings", "cisco group"}),
    ("Brunswick Corporation", "BC", {"brunswick", "brunswick corp"}, {"brunswick group"}),
    ("Goldman Sachs", "GS", {"goldman sachs", "goldman sachs co"}, {"goldman sachs group"}),

    # '&' is dropped (GKG form: "procter gamble", "m t bank", "s p global inc"); an 'and'
    # spelling is added as a second form. A trailing "& Co." strips like any suffix.
    ("Procter & Gamble", "PG", {"procter gamble", "procter and gamble", "procter gamble co"}, {"procter & gamble"}),
    ("JPMorgan Chase & Co.", "JPM", {"jpmorgan chase", "jpmorgan chase co", "jpmorgan chase and co"}, set()),
    ("KKR & Co.", "KKR", {"kkr", "kkr co"}, {"kkr and"}),
    ("S&P Global", "SPGI", {"s p global", "s p global inc", "s and p global"}, set()),
    ("AT&T", "T", {"at t", "at and t"}, {"att", "at&t"}),
    ("Deere & Company", "DE", {"deere", "deere company"}, set()),

    # Leading 'the' is dropped, whether written first or as a trailing "(The)".
    ("The Home Depot, Inc.", "HD", {"home depot", "home depot inc"}, {"the home depot"}),
    ("Home Depot (The)", "HD", {"home depot", "home depot inc"}, {"the home depot", "home depot the"}),
    ("Walt Disney Company (The)", "DIS", {"walt disney", "walt disney company", "walt disney co"}, set()),
    ("Hartford (The)", "HIG", {"hartford"}, {"the hartford"}),

    # Any other trailing parenthetical is a displaced first word and is moved to the front.
    ("Lilly (Eli)", "LLY", {"eli lilly", "eli lilly co", "eli lilly company"}, {"lilly eli", "lilly"}),

    # Dotted initials collapse into one token ("u.s." -> "us", "c.h." -> "ch"), with or without
    # spaces between them; any other dot becomes a space ("salesforce.com" -> "salesforce com").
    ("U.S. Bancorp", "USB", {"us bancorp"}, {"u s bancorp", "u.s. bancorp"}),
    ("C.H. Robinson", "CHRW", {"ch robinson", "ch robinson inc"}, {"c h robinson"}),
    ("A. O. Smith", "AOS", {"ao smith", "ao smith corporation"}, {"a o smith", "smith"}),
    ("J.B. Hunt", "JBHT", {"jb hunt"}, {"hunt"}),
    ("Arthur J. Gallagher & Co.", "AJG", {"arthur j gallagher", "arthur j gallagher co"}, set()),
    ("Salesforce.com, Inc.", "CRM", {"salesforce com", "salesforce com inc"}, set()),

    # Apostrophes vanish; a possessive 's also yields the bare stem, which GKG uses more often
    # ("mcdonald" 505 docs vs "mcdonalds" 123 on the measured day).
    ("McDonald's", "MCD", {"mcdonalds", "mcdonald", "mcdonald corporation", "mcdonalds corporation"}, {"mcdonald's", "mcdonald s"}),
    ("Lowe's", "LOW", {"lowes", "lowe", "lowes inc"}, set()),
    ("O'Reilly Automotive", "ORLY", {"oreilly automotive"}, {"o reilly automotive", "o'reilly automotive"}),

    # Hyphens are kept (GKG keeps them: "wal-mart", "mercedes-benz") and a spaced form is added
    # ("coca cola" is how GKG reported Coca-Cola on the measured day).
    ("Coca-Cola Company (The)", "KO", {"coca-cola", "coca cola", "coca-cola company", "coca cola company"}, set()),
    ("T-Mobile US", "TMUS", {"t-mobile us", "t mobile us"}, set()),

    # Other punctuation is removed; accents are folded ("estee") and, since GKG sometimes drops
    # non-ASCII letters outright ("hans-bckler"), the dropped form is added as well.
    ("Yum! Brands", "YUM", {"yum brands", "yum brands inc"}, {"yum! brands"}),
    ("Estée Lauder Companies (The)", "EL", {"estee lauder", "este lauder", "estee lauder companies"}, set()),
    ("Brown–Forman", "BF.B", {"brown-forman", "brown forman"}, set()),

    # Share classes map to one company: "(Class A)" / "Class B" are dropped, so both listings
    # produce identical aliases (see SAME_ALIAS_CASES).
    ("Alphabet Inc. (Class A)", "GOOGL", {"alphabet", "alphabet inc"}, {"alphabet inc class a", "alphabet class a", "googl"}),
    ("Fox Corporation (Class B)", "FOX", {"fox", "fox corporation", "fox corp"}, {"fox corporation class b"}),
    ("News Corp Class A", "NWSA", {"news corp", "news"}, {"news corp class a"}),

    # The ticker becomes an alias only when it spells the initials of the name (>= 3 letters):
    # GKG reports "ibm", "amd", "aig". Tickers are otherwise ignored ("t", "ko", "googl" are not
    # names anyone writes).
    ("International Business Machines", "IBM", {"ibm", "international business machines"}, set()),
    ("Advanced Micro Devices", "AMD", {"amd", "advanced micro devices"}, set()),
    ("American International Group", "AIG", {"aig", "american international group inc"}, set()),
    ("Automatic Data Processing", "ADP", {"adp"}, set()),
    ("Coca-Cola Company (The)", "KO", set(), {"ko"}),
    ("General Electric", "GE", set(), {"ge"}),  # 2 letters: too short to be an initialism alias

    # A handful of companies are reported under a brand no rule can derive from the legal name.
    # This is a fixed list, kept short; extend it only with measured evidence.
    ("Alphabet Inc. (Class C)", "GOOG", {"google"}, set()),
    ("Walt Disney Company (The)", "DIS", {"disney"}, set()),
    ("Meta Platforms", "META", {"meta platforms", "meta platforms inc", "meta"}, {"facebook"}),
    ("ExxonMobil", "XOM", {"exxonmobil", "exxon mobil", "exxon"}, set()),
]

# Pairs of listings that must produce exactly the same alias list (share classes).
SAME_ALIAS_CASES: list[tuple[tuple[str, str], tuple[str, str]]] = [
    (("Alphabet Inc. (Class A)", "GOOGL"), ("Alphabet Inc. (Class C)", "GOOG")),
    (("Fox Corporation (Class A)", "FOXA"), ("Fox Corporation (Class B)", "FOX")),
    (("News Corp (Class A)", "NWSA"), ("News Corp (Class B)", "NWS")),
]

# --- is_ambiguous ---------------------------------------------------------------------------

AMBIGUOUS_CASES: list[tuple[str, bool]] = [
    # Bare cores that are ordinary English words would match non-company org strings.
    ("apple", True),
    ("target", True),
    ("gap", True),
    ("ball", True),
    ("visa", True),
    ("oracle", True),
    ("discovery", True),
    ("block", True),
    ("progressive", True),
    ("southern", True),
    ("booking", True),
    ("news", True),
    ("meta", True),

    # Place, river and mountain names.
    ("amazon", True),
    ("hartford", True),
    ("corning", True),
    ("everest", True),

    # Common surnames and given names: GKG mislabels persons as orgs (codebook, V1Organizations).
    ("ford", True),
    ("lowe", True),
    ("emerson", True),
    ("parker", True),
    ("cooper", True),
    ("johnson", True),

    # Single tokens of one or two characters are too short to trust.
    ("ge", True),
    ("hp", True),
    ("3m", True),

    # Anything with a strong legal suffix, or a distinctive multi-word name, is a company.
    ("apple inc", False),
    ("target corporation", False),
    ("ford motor", False),
    ("best buy", False),
    ("home depot", False),
    ("southern company", False),
    ("gap inc", False),
    ("news corp", False),

    # ...but a weak suffix (group, holdings, co) on an ambiguous core is still generic.
    ("news group", True),
    ("news co", True),
    ("southern group", True),
    ("fox holdings", True),

    # Companies far more often cited as a source, market or authority than reported on, and
    # other generic strings that matched in experiment 0002.
    ("nasdaq", True),
    ("new york times", True),
    ("gartner", True),
    ("moody", True),
    ("adm", True),
    ("crown corporation", True),
    ("american international", True),
    ("ryder", True),

    # Named incidentally in most articles that list them (a platform, a venue, boilerplate such
    # as "follow us on Google News"), or a possessive stem that is mostly a surname.
    ("google", True),
    ("netflix", True),
    ("hyatt", True),
    ("mcdonald", True),
    ("mcdonalds", False),

    # Coined brand words are unambiguous even though they are single tokens.
    ("boeing", False),
    ("intel", False),
    ("pfizer", False),
    ("nvidia", False),
    ("exxonmobil", False),
    ("ibm", False),

    # Case and surrounding whitespace do not matter.
    ("  Apple ", True),
    ("Best Buy", False),
]
