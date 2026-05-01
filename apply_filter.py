"""
apply_filter.py — STOXX 600 headline filter v2.1

Changes from v2:
  - E3 (deduplication) removed — was an unauthorized addition not in headline_filter_v2.js.
  - E-MKT extended with exchange-code EUR/GBP price pattern (solo trigger, index 8).
  - E-PEER added to catch peer-comparison/benchmarking headlines (applied before E1).
  - E7 now checks firm aliases and a per-firm exclusion list (subsidiary-name handling).
  - filter_version bumped to 'v2.1'.

Usage:
    python apply_filter.py \\
        --input pilot_output/headlines_pilot.parquet \\
        --output-parquet pilot_output/headlines_pilot_filtered_v2_1.parquet \\
        --output-csv pilot_output/headlines_pilot_filtered_v2_1.csv \\
        [--sources Reuters,Bloomberg] \\
        [--compare-v2 pilot_output/headlines_pilot_filtered.parquet]
"""
from __future__ import annotations

import re
import sys
import argparse
from typing import Optional

import pandas as pd

# ═══════════════════════════════════════════════════════════════════════
# RULE LISTS
# ═══════════════════════════════════════════════════════════════════════

# ── E-MKT: Market-data / ticker-feed patterns ───────────────────────────
# Indices 1 and 8 are "solo triggers" — they fire E-MKT alone.
# All others require ≥2 pattern hits. See _MKT_SOLO below.
MKT_PATTERNS: list[re.Pattern] = [
    # 0 — Ticker in parens with $ price: (TICK: $12.34)
    re.compile(r'\([A-Z]{1,6}\.?[A-Z]?\s*:\s*[$£€]\d+[\d.,]*\)'),
    # 1 — Vol Index score [SOLO TRIGGER]
    re.compile(r'\bvol(?:ume)?\s+index\s+\d[\d.]*', re.IGNORECASE),
    # 2 — Change in brackets: -2c [-0.2%] or +4p [+0.3%]
    re.compile(r'[+\-]\d+(?:\.\d+)?[cp]\s*\[[+\-]\d+(?:\.\d+)?%\]'),
    # 3 — "decreases/increases on robust/light/average volume"
    re.compile(
        r'\b(?:decrease|increase|rise|fall|gain|drop)s?\s+on\s+'
        r'(?:robust|light|average|heavy|thin|below.avg|above.avg)\s+volume\b',
        re.IGNORECASE,
    ),
    # 4 — ADR price bulletin
    re.compile(r'\bADR\s*\([A-Z]{1,6}:?\s*[$£€]\d[\d.,]*\)', re.IGNORECASE),
    # 5 — "[price] in [time] trading" standalone price quote
    re.compile(
        r'^[\w\s,.\-]+(?:at|@)\s*[$£€CHF]?\d[\d.,]+\s+in\s+'
        r'(?:early|late|mid|morning|afternoon|pre-market|after-hours)\s+\w+\s+trading\.?\s*$',
        re.IGNORECASE,
    ),
    # 6 — Bare price move: "volume 18.4m shares"
    re.compile(r'\bvolume\s+\d[\d.,]+[mk]?\s+shares\b', re.IGNORECASE),
    # 7 — Intraday high/low with price
    re.compile(r'\b(?:intraday|session)\s+(?:high|low)\s+of\s+[$£€CHF]?\d[\d.,]+', re.IGNORECASE),
    # 8 — Exchange-code EUR/GBP/CHF price: (EN: EUR30.94), (LN: GBP12.34) [SOLO TRIGGER]
    re.compile(
        r'\([A-Z]{2,3}\s*:\s*(?:EUR|GBP|CHF|USD|JPY|SEK|NOK|DKK|CZK)\s*\d[\d.,]*\)',
        re.IGNORECASE,
    ),
]

# Patterns that trigger E-MKT on their own (no ≥2 requirement).
# Must be updated if MKT_PATTERNS indices shift.
_MKT_SOLO: frozenset[re.Pattern] = frozenset({MKT_PATTERNS[1], MKT_PATTERNS[8]})

# ── E1: Macroeconomic / market-wide keywords ────────────────────────────
MACRO_KW: list[str] = [
    'ecb ', 'ecb:', 'ecb,', 'european central bank', 'fed ', 'fed:', 'federal reserve',
    'bank of england', 'boe ', 'boj ', 'pboc ', 'central bank',
    'interest rate decision', 'rate decision', 'rate hold', 'rates steady', 'rates unchanged',
    'rate cut', 'rate hike', 'rate rise',
    'inflation data', 'cpi ', 'ppi ', 'gdp ', 'unemployment rate', 'nonfarm', 'payroll',
    'retail sales data', 'trade balance', 'current account deficit', 'current account surplus',
    'eurozone economy', 'eurozone inflation', 'eurozone gdp', 'eurozone growth', 'euro area gdp',
    'european stocks ', 'european equities ', 'stoxx ', 'dax falls', 'dax rises',
    'cac falls', 'cac rises', 'ftse falls', 'ftse rises',
    's&p 500', 'dow jones', 'nasdaq ', 'vix ',
    'treasury yield', 'bund yield', 'gilt yield', 'bond yield', 'sovereign yield', '10-year yield',
    'oil price', 'crude price', 'brent ', 'wti ', 'gas price', 'energy price', 'commodity price', 'gold price',
    'dollar index', 'euro dollar', 'eur/usd', 'usd/eur', 'gbp/usd', 'forex ', 'exchange rate',
    'geopolit', 'ukraine war', 'middle east conflict', 'taiwan strait', 'north korea', 'russia sanctions',
    'global trade', 'world trade', 'wto ', 'imf ', 'world bank ', 'oecd ',
]

# ── E2: Sector-wide keywords ────────────────────────────────────────────
SECTOR_KW: list[str] = [
    'auto sector', 'banking sector', 'pharma sector', 'energy sector', 'tech sector',
    'financial sector', 'retail sector', 'mining sector', 'insurance sector',
    'utilities sector', 'telecom sector', 'chip sector', 'semiconductor sector',
    'luxury sector', 'airline sector', 'property sector', 'real estate sector',
    'sector rally', 'sector sell-off', 'sector decline', 'sector gains',
    'sector falls', 'sector rises', 'sector under pressure', 'sector headwinds',
    'sector tailwinds', 'sector rotation',
    'european banks ', 'european automakers', 'european miners', 'european insurers',
    'european utilities', 'european retailers', 'european telecoms',
    'industry-wide', 'across the industry', 'across the sector',
    'throughout the sector', 'the broader industry', 'sector peers', 'industry peers',
]

# ── E4: Price-move-only patterns ────────────────────────────────────────
PRICE_ONLY: list[re.Pattern] = [
    re.compile(
        r'\bshares?\s+(?:rise|fall|gain|drop|climb|slip|jump|tumble|surge|plunge|rally|advance|retreat)\s+\d[\d.]*%',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:gains?|falls?|rises?|drops?|slides?|surges?|slumps?|climbs?|rallies)\s+\d[\d.]*\s*(?:%|percent|points?|p)\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:up|down)\s+\d[\d.]*\s*(?:%|percent)\s+on\s+'
        r'(?:monday|tuesday|wednesday|thursday|friday|the\s+day|trading|session)\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:leads?|tops?|paces?)\s+(?:the\s+)?(?:dax|ftse|cac|stoxx|index|market|gainers|fallers|risers|decliners)\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\bshares?\s+(?:hit|touch|reach)\s+(?:a\s+)?(?:new\s+)?(?:52.week|record|multi.year)\s+(?:high|low)\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:gains?|falls?|rises?|drops?)\s+\d+\s+(?:points?|bps|basis\s+points?)\s+(?:on|in)\s+'
        r'(?:monday|tuesday|wednesday|thursday|friday|morning|afternoon|trading|session)\b',
        re.IGNORECASE,
    ),
]

# ── E5: Analyst reiteration patterns ───────────────────────────────────
REITERATE: list[re.Pattern] = [
    re.compile(
        r'\b(?:reiterates?|maintains?|keeps?|retains?)\s+(?:its\s+)?'
        r'(?:buy|sell|hold|neutral|outperform|underperform|overweight|underweight|market.perform)\s+'
        r'(?:rating|recommendation|stance|call)\b',
        re.IGNORECASE,
    ),
    re.compile(r'\bno\s+change\s+to\s+(?:rating|outlook|target|guidance|recommendation)\b', re.IGNORECASE),
    re.compile(r'\bprice\s+target\s+unchanged\b', re.IGNORECASE),
    re.compile(r'\btarget\s+price\s+unchanged\b', re.IGNORECASE),
]

# ── E6: Administrative / routine patterns ──────────────────────────────
ADMIN: list[re.Pattern] = [
    re.compile(
        r'\b(?:to\s+)?(?:release|report|publish|announce)\s+'
        r'(?:q[1-4]|quarterly|annual|half.year|full.year)\s+results?\s+on\b',
        re.IGNORECASE,
    ),
    re.compile(r'\bannounces?\s+(?:agm|annual general meeting)\s+date\b', re.IGNORECASE),
    re.compile(r'\bfiles?\s+(?:annual\s+report|form\s+\w+|regulatory\s+filing|20-f|10-k|6-k)\b', re.IGNORECASE),
    re.compile(r'\bschedules?\s+(?:results?|earnings)\s+(?:for|on)\b', re.IGNORECASE),
    re.compile(r':\s*(?:update|filing|announcement)\s*$', re.IGNORECASE),
    re.compile(r'^[\w\s,.\-]+:\s*(?:update|filing|announcement|statement)\s*$', re.IGNORECASE),
    re.compile(r'\bmanagement\s+speaks?\b', re.IGNORECASE),
    re.compile(r'\bcompany\s+announcement\b', re.IGNORECASE),
    re.compile(r'\bex-dividend\s+date\b', re.IGNORECASE),
    re.compile(r'\brecord\s+date\s+for\s+dividend\b', re.IGNORECASE),
]

# ── I-AC: Analyst change (upgrade/downgrade/initiation) ─────────────────
ANALYST_CHANGE: list[re.Pattern] = [
    re.compile(r'\b(?:upgrades?|downgrades?|initiates?\s+coverage(?:\s+of)?)\b', re.IGNORECASE),
]

# ── I1+I2: Valuation channel inclusion patterns ─────────────────────────
VALUATION: list[re.Pattern] = [
    # Earnings, revenue, guidance, profit
    re.compile(
        r'\b(?:earnings?|net\s+income|operating\s+profit|ebit\b|ebitda\b'
        r'|revenues?\s+(?:beat|miss|grow|fall|rise|decline)'
        r'|profit\s+(?:warn|jump|slump|rise|fall|surge)'
        r'|loss\s+widen|margin\b|cash\s+flow'
        r'|guidance\s+(?:raise|cut|lower|lift|trim|revise|upgrad|downgrad)'
        r'|forecast\s+(?:raise|cut|lower|lift|revise)'
        r'|outlook\s+(?:raise|cut|upgrad|downgrad|lift|lower|improv|deteriorat)'
        r'|warn(?:ing)?\b'
        r'|results?\s+(?:beat|miss|top|disappoint)'
        r'|quarterly\s+result|full.year\s+result)\b',
        re.IGNORECASE,
    ),
    # Dividends & buybacks
    re.compile(
        r'\b(?:dividend|buyback|share\s+repurchase|capital\s+return|special\s+dividend)\b',
        re.IGNORECASE,
    ),
    # M&A & strategic deals
    re.compile(
        r'\b(?:acqui(?:re|res|red|ring|sition)|merger\b|takeover\b'
        r'|bid\s+for|offer\s+for|tender\s+offer'
        r'|divest(?:s|ed|ing|iture)?|disposal\b|spin.?off'
        r'|joint\s+venture|partnership\s+deal|strategic\s+alliance'
        r'|agrees?\s+to\s+(?:buy|sell|acquire|merge))\b',
        re.IGNORECASE,
    ),
    # Contracts & orders
    re.compile(
        r'\b(?:wins?\s+(?:contract|deal|order|tender)'
        r'|secures?\s+(?:contract|deal|order)'
        r'|loses?\s+(?:contract|deal)'
        r'|awarded\s+(?:contract|deal))\b',
        re.IGNORECASE,
    ),
    # Products & launches
    re.compile(
        r'\b(?:launch(?:es|ed)?\s+(?:new|product|\w+\s+model)|new\s+product\b|product\s+launch\b)\b',
        re.IGNORECASE,
    ),
    # Facility changes
    re.compile(r'\b(?:plant|factory|facility|site)\s+(?:clos(?:e|ure|ing)|shutdow|idl)\b', re.IGNORECASE),
    # Executive changes
    re.compile(
        r'\b(?:(?:ceo|cfo|coo|chairman|president)\b.*\b(?:depart|resign|step\s+down|replac|appoint|nam|leav)'
        r'|chief\s+executive.*(?:depart|resign|step\s+down|replac|appoint|nam|leav)'
        r'|appoints?\s+(?:new\s+)?(?:ceo|cfo|coo|chairman|president)\b)\b',
        re.IGNORECASE,
    ),
    # Regulatory & approvals
    re.compile(
        r'\b(?:regulator\w*\s+(?:approv|fine|sanction|ban|reject|block|clear|order|probe|investigat)'
        r'|fda\s+(?:approv|reject|clear)'
        r'|ema\s+(?:approv|reject|clear)'
        r'|eu\s+commission\s+(?:fine|approv|block|clear))\b',
        re.IGNORECASE,
    ),
    # Litigation & legal
    re.compile(
        r'\b(?:fine\b|penalt(?:y|ies)\b.*(?:\d|\bmillion\b|\bbillion\b)'
        r'|litigation\b|lawsuit\b'
        r'|legal\s+(?:action|proceed|settl|charg)'
        r'|settl(?:e|ement|ing)\b|verdict\b|judgment\b|court\s+(?:rules?|order))\b',
        re.IGNORECASE,
    ),
    # Credit ratings
    re.compile(
        r'\b(?:credit\s+rating\b'
        r'|(?:downgraded?|upgraded?)\s+(?:to|by)\b.*\b(?:moody|fitch|s&p|standard.*poor)'
        r'|moody.*(?:downgrad|upgrad)'
        r'|fitch.*(?:downgrad|upgrad))\b',
        re.IGNORECASE,
    ),
    # Capital markets
    re.compile(
        r'\b(?:(?:bond|debt|equity)\s+(?:issu|offer|rais)'
        r'|rights\s+issue\b|capital\s+(?:rais|increas)|refinanc)\b',
        re.IGNORECASE,
    ),
    # Accounting & audit
    re.compile(
        r'\b(?:restat(?:e|ement|ing)\b'
        r'|accounting\s+(?:error|restat|irregularit)'
        r'|audit\s+(?:qualif|concern|fail)'
        r'|impairment\b|writedown\b|write-down\b|goodwill\s+impairment)\b',
        re.IGNORECASE,
    ),
    # Activism & governance
    re.compile(r'\b(?:activist\b|proxy\s+(?:fight|contest|battle)|hostile\s+(?:bid|takeover))\b', re.IGNORECASE),
    # Cybersecurity
    re.compile(
        r'\b(?:cyberattack\b|data\s+breach\b|ransomware\b|hack(?:ed|ing)\b.*(?:firm|company|group|corp))\b',
        re.IGNORECASE,
    ),
    # Labour & operations
    re.compile(
        r'\b(?:strike\b.*(?:worker|employee|staff|union)|industrial\s+action\b|labour\s+dispute\b)\b',
        re.IGNORECASE,
    ),
    # Clinical / pharma
    re.compile(
        r'\b(?:phase\s+(?:i+|[123])\s+(?:trial|result|data|read|fail|success)\b'
        r'|clinical\s+trial\s+(?:result|fail|success|data)\b)\b',
        re.IGNORECASE,
    ),
    # Resource discovery
    re.compile(r'\b(?:discovery\b.*(?:oil|gas|mineral)|oil\s+field|gas\s+field)\b', re.IGNORECASE),
    # Restructuring & job cuts
    re.compile(
        r'\b(?:restructur\b|job\s+(?:cut|reduc|loss)|layoff\b|redundanc\b|workforce\s+(?:reduc|cut))\b',
        re.IGNORECASE,
    ),
]

# ── E-PEER: Peer benchmarking / comparison patterns ─────────────────────
PEER_COMPARISON: list[re.Pattern] = [
    # "How X's dividend compares to Y" / "How X's dividend stacks up against Y"
    re.compile(
        r"\bhow\s+[\w\s']+(?:dividend|earnings|tax\s+rate|valuation)\s+(?:compare|stack)",
        re.IGNORECASE,
    ),
    # "X's dividend in perspective: comparison with Y" / "compared with Y"
    re.compile(r'\bdividend\s+in\s+perspective\b.*\bcomparison\s+with\b', re.IGNORECASE),
    # "Benchmarking X vs Y:"
    re.compile(r'\bbenchmarking\s+[\w\s.&]+\s+vs\.?\s+[\w\s.&]+:', re.IGNORECASE),
    # "X outpaces Y" / "continues to outperform Y" / "shifts momentum to underperform"
    re.compile(
        r'\b(?:outpaces|bounces?\s+back\s+to\s+(?:out|under)perform'
        r'|continues\s+to\s+(?:out|under)perform'
        r'|shifts\s+momentum\s+to\s+(?:out|under)perform)\b',
        re.IGNORECASE,
    ),
    # "Earnings Growth Lags" / "earnings lag"
    re.compile(r'\b(?:earnings\s+growth\s+lags|earnings\s+lag)\b', re.IGNORECASE),
    # "How the dividend of X and Y compare"
    re.compile(r'\bhow\s+the\s+(?:dividend|earnings)\s+of\b.+\band\b.+\bcompare\b', re.IGNORECASE),
    # "X is good value by dividend yield despite N% price rise"
    re.compile(
        r'\b(?:good\s+value|fair\s+value|cheap|expensive)\s+by\s+\w+\s+yield\s+despite\b',
        re.IGNORECASE,
    ),
]

# ── E7: Firm aliases and exclusions ────────────────────────────────────
# TODO: For the full 600-firm run, load this from an external CSV
#       (firm_aliases.csv with columns: ric, alias) so it can be maintained
#       without touching source code.
FIRM_ALIASES: dict[str, list[str]] = {
    "BOUY.PA": [
        "Bouygues", "Colas", "Bouygues Telecom", "Bouygues Construction",
        "Bouygues Immobilier", "Bouygues Energies", "Equans", "TF1",
    ],
    "INGA.AS": [
        "ING Groep", "ING Group", "ING Bank", "ING N.V.", "ING NV",
        "ING ", "ING,", "ING:", "ING-",
    ],
    "GETP.PA": [
        "Getlink", "Eurotunnel", "Europorte", "ElecLink",
        "Channel Tunnel", "Chunnel",
    ],
}

# Headlines matching these substrings refer to a different listed entity,
# not the focal firm. Checked before aliases — alias must not rescue them.
FIRM_EXCLUSIONS: dict[str, list[str]] = {
    "INGA.AS": ["ING Bank Slaski", "ING Slaski", "ING Bank Śląski"],
}

# Source allow-list constant (mirrors JS DEFAULT_SOURCES; not used by default)
DEFAULT_SOURCES: list[str] = [
    'reuters', 'bloomberg', 'dow jones', 'dow jones newswires',
    'pr newswire', 'business wire', 'globe newswire',
    'afx', 'agence france-presse', 'afp',
    'financial times', 'ft.com',
    'wall street journal', 'wsj',
    'handelsblatt', 'les echos',
    'mni market news', 'mni',
]


# ═══════════════════════════════════════════════════════════════════════
# CORE LOGIC
# ═══════════════════════════════════════════════════════════════════════

def is_mkt_data(h: str) -> bool:
    """True if headline looks like an automated market-data/ticker-feed bulletin."""
    hits = 0
    has_definitive = False
    for p in MKT_PATTERNS:
        if p.search(h):
            hits += 1
            if p in _MKT_SOLO:
                has_definitive = True
    return hits >= 2 or has_definitive


def has_val(h: str) -> bool:
    """True if headline contains at least one identifiable valuation channel."""
    return any(p.search(h) for p in VALUATION)


def is_peer_comparison(h: str) -> bool:
    """True if headline is a peer benchmarking or comparative analysis template."""
    return any(p.search(h) for p in PEER_COMPARISON)


def _check_firm_mention(hl: str, fl: str, firm_ric: str) -> tuple[bool, bool]:
    """
    Check whether the focal firm is mentioned in the headline.

    Returns:
        (firm_mentioned, force_exclude) where:
        - firm_mentioned: True if the firm (or a known alias) appears.
        - force_exclude: True if an exclusion matched — this headline is about a
          different listed entity and must be E7-excluded regardless of has_val.
    """
    if fl in hl:
        return True, False

    exclusions = FIRM_EXCLUSIONS.get(firm_ric, [])
    if any(excl.lower() in hl for excl in exclusions):
        # Different entity — unconditional E7, even if has_val would save it.
        return False, True

    aliases = FIRM_ALIASES.get(firm_ric, [])
    if any(alias.lower() in hl for alias in aliases):
        return True, False

    return False, False


def classify(
    h: str,
    firm: str = '',
    firm_ric: str = '',
    source: str = '',
    sources: Optional[list[str]] = None,
) -> dict[str, str]:
    """
    Classify a single headline.

    Args:
        h:        Headline text (plain; no SOURCE| prefix expected).
        firm:     Focal firm name for E7 incidental-mention check.
        firm_ric: RIC of the focal firm, used to look up FIRM_ALIASES/EXCLUSIONS.
        source:   Source name string (from source_name column).
        sources:  Lowercase allow-list; None disables E-SRC.

    Returns:
        dict with keys: decision, rule, reason
    """
    h = h.strip()
    if not h:
        return {'decision': 'EXCLUDE', 'rule': 'E8', 'reason': 'Empty headline'}

    hl = h.lower()
    fl = firm.lower().strip()

    # ── E-SRC: source allow-list ──────────────────────────────────────
    if sources is not None and len(sources) > 0 and source:
        src_low = source.lower()
        if not any(s in src_low for s in sources):
            return {'decision': 'EXCLUDE', 'rule': 'E-SRC', 'reason': f'Source "{source}" not in allow-list'}

    # ── E8: Too short ─────────────────────────────────────────────────
    if len(h) < 12:
        return {'decision': 'EXCLUDE', 'rule': 'E8', 'reason': 'Too short to be interpretable'}

    # ── E-MKT: Market-data ticker feed ───────────────────────────────
    if is_mkt_data(h):
        if not has_val(h):
            return {
                'decision': 'EXCLUDE',
                'rule': 'E-MKT',
                'reason': 'Automated market-data / ticker-feed bulletin; no new information',
            }

    # ── E-PEER: Peer benchmarking / comparison ────────────────────────
    if is_peer_comparison(h):
        return {
            'decision': 'EXCLUDE',
            'rule': 'E-PEER',
            'reason': 'Peer benchmarking/comparison; no firm-specific event',
        }

    # ── E1: Macroeconomic ─────────────────────────────────────────────
    if any(kw in hl for kw in MACRO_KW):
        if not has_val(h):
            return {
                'decision': 'EXCLUDE',
                'rule': 'E1',
                'reason': 'Macroeconomic/market-wide headline; no firm-specific valuation content',
            }

    # ── E2: Sector-wide ───────────────────────────────────────────────
    if any(kw in hl for kw in SECTOR_KW):
        if not has_val(h):
            return {
                'decision': 'EXCLUDE',
                'rule': 'E2',
                'reason': 'Sector-wide headline; no firm-specific valuation content',
            }

    # ── E6: Administrative / routine ─────────────────────────────────
    if any(p.search(h) for p in ADMIN):
        return {
            'decision': 'EXCLUDE',
            'rule': 'E6',
            'reason': 'Administrative/routine disclosure; no valuation-relevant new information',
        }

    # ── E4: Price-move only ───────────────────────────────────────────
    if any(p.search(h) for p in PRICE_ONLY):
        if not has_val(h):
            return {
                'decision': 'EXCLUDE',
                'rule': 'E4',
                'reason': 'Price-move headline with no new underlying information',
            }

    # ── E5: Analyst reiteration ───────────────────────────────────────
    if any(p.search(h) for p in REITERATE):
        return {
            'decision': 'EXCLUDE',
            'rule': 'E5',
            'reason': 'Analyst reiteration/unchanged rating; no new information',
        }

    # ── E7: Firm not mentioned (alias-aware) ──────────────────────────
    if fl:
        firm_mentioned, force_exclude = _check_firm_mention(hl, fl, firm_ric)
        if force_exclude:
            # Exclusion list match — headline is about a different listed entity.
            # Unconditional E7 regardless of valuation content.
            return {
                'decision': 'EXCLUDE',
                'rule': 'E7',
                'reason': 'Headline refers to a different listed entity (FIRM_EXCLUSIONS match)',
            }
        if not firm_mentioned and not has_val(h):
            return {
                'decision': 'EXCLUDE',
                'rule': 'E7',
                'reason': 'Focal firm not mentioned; no identifiable valuation channel',
            }

    # ── I-AC: Analyst upgrade/downgrade with valuation content ───────
    if any(p.search(h) for p in ANALYST_CHANGE) and has_val(h):
        return {'decision': 'INCLUDE', 'rule': 'I-AC', 'reason': 'Analyst rating change with valuation-relevant content'}

    # ── E8 (catch-all): No valuation channel ─────────────────────────
    if not has_val(h):
        return {'decision': 'EXCLUDE', 'rule': 'E8', 'reason': 'No identifiable valuation channel; too vague or generic'}

    # ── I1+I2: Default include ────────────────────────────────────────
    return {'decision': 'INCLUDE', 'rule': 'I1+I2', 'reason': 'Firm-specific headline with identifiable valuation channel'}


# ═══════════════════════════════════════════════════════════════════════
# DATASET APPLICATION
# ═══════════════════════════════════════════════════════════════════════

def apply_filter(df: pd.DataFrame, sources: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Apply classify() to every row of df, using each row's firm_name as the
    focal firm (E7) and firm_ric for alias/exclusion lookups.
    """
    decisions: list[str] = []
    rules: list[str] = []
    reasons: list[str] = []

    total = len(df)
    for i, (_, row) in enumerate(df.iterrows()):
        if i > 0 and i % 5000 == 0:
            print(f'  ... {i:,}/{total:,} rows processed', file=sys.stderr)

        h = str(row['headline_text']) if pd.notna(row['headline_text']) else ''
        firm = str(row['firm_name']) if pd.notna(row['firm_name']) else ''
        firm_ric = str(row['firm_ric']) if pd.notna(row['firm_ric']) else ''
        source = str(row['source_name']) if pd.notna(row['source_name']) else ''

        result = classify(h, firm, firm_ric, source, sources)
        decisions.append(result['decision'])
        rules.append(result['rule'])
        reasons.append(result['reason'])

    out = df.copy()
    out['decision'] = decisions
    out['rule'] = rules
    out['reason'] = reasons
    out['filter_version'] = 'v2.1'
    return out


# ═══════════════════════════════════════════════════════════════════════
# SUMMARY & COMPARISON
# ═══════════════════════════════════════════════════════════════════════

def print_summary(df: pd.DataFrame) -> None:
    """Print v2.1 filter statistics to stdout."""
    total = len(df)
    inc = int((df['decision'] == 'INCLUDE').sum())
    exc = int((df['decision'] == 'EXCLUDE').sum())

    def pct(n: int) -> str:
        return f'{n / total * 100:.1f}%' if total else '0.0%'

    print()
    print('Headline filter v2.1 — results')
    print('─' * 50)
    print(f'  Total   : {total:,}')
    print(f'  Include : {inc:,} ({pct(inc)})')
    print(f'  Exclude : {exc:,} ({pct(exc)})')
    print()

    print('  Rule breakdown (sorted):')
    rule_counts = df['rule'].value_counts().sort_index()
    for rule, count in rule_counts.items():
        print(f'    {rule:<10} {count:>6}  ({count / total * 100:.1f}%)')
    if 'E3' not in rule_counts.index:
        print('    (E3 not present — confirmed)')
    print()

    firm_stats = (
        df.groupby('firm_name')['decision']
        .apply(lambda x: (x == 'INCLUDE').mean() * 100)
        .rename('include_pct')
        .sort_values(ascending=False)
    )

    print('  Per-firm include rate — top 10 highest:')
    for firm, pct_val in firm_stats.head(10).items():
        n = int((df['firm_name'] == firm).sum())
        print(f'    {firm:<40} {pct_val:5.1f}%  (n={n:,})')
    print()

    print('  Per-firm include rate — top 10 lowest:')
    for firm, pct_val in firm_stats.tail(10).sort_values().items():
        n = int((df['firm_name'] == firm).sum())
        print(f'    {firm:<40} {pct_val:5.1f}%  (n={n:,})')
    print()


def print_comparison(df_new: pd.DataFrame, df_old: pd.DataFrame) -> None:
    """Print a diff between v2.1 (new) and v2 (old) output, aligned by row position."""
    if len(df_new) != len(df_old):
        print(f'WARNING: row counts differ ({len(df_new):,} vs {len(df_old):,}); skipping diff.', file=sys.stderr)
        return

    old_dec = df_old['decision'].values
    new_dec = df_new['decision'].values
    old_rule = df_old['rule'].values
    new_rule = df_new['rule'].values

    changed_mask = old_dec != new_dec
    n_changed = int(changed_mask.sum())
    exc_to_inc = int(((old_dec == 'EXCLUDE') & (new_dec == 'INCLUDE')).sum())
    inc_to_exc = int(((old_dec == 'INCLUDE') & (new_dec == 'EXCLUDE')).sum())

    print('=== v2 → v2.1 diff ===')
    print(f'  Rows that changed decision : {n_changed:,} ({n_changed / len(df_new) * 100:.1f}%)')
    print(f'    EXCLUDE → INCLUDE        : {exc_to_inc:,}')
    print(f'    INCLUDE → EXCLUDE        : {inc_to_exc:,}')
    print()

    transitions: dict[tuple[str, str], int] = {}
    for i in range(len(df_new)):
        if changed_mask[i]:
            key = (str(old_rule[i]), str(new_rule[i]))
            transitions[key] = transitions.get(key, 0) + 1

    print('  Top 10 rule transitions (old_rule → new_rule):')
    for (old_r, new_r), count in sorted(transitions.items(), key=lambda x: -x[1])[:10]:
        print(f'    {old_r:<10} → {new_r:<10}  {count:,}')
    print()


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Apply headline filter v2.1 to a Parquet dataset.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--input', required=True, metavar='FILE', help='Input Parquet file')
    parser.add_argument('--output-parquet', required=True, metavar='FILE', help='Output Parquet file')
    parser.add_argument('--output-csv', required=True, metavar='FILE', help='Output CSV file')
    parser.add_argument(
        '--sources',
        default=None,
        metavar='X,Y,Z',
        help='Comma-separated source allow-list (enables E-SRC). E.g. Reuters,Bloomberg',
    )
    parser.add_argument(
        '--compare-v2',
        default=None,
        metavar='FILE',
        help='v2 output Parquet to diff against (optional)',
    )
    args = parser.parse_args()

    sources: Optional[list[str]] = None
    if args.sources:
        sources = [s.strip().lower() for s in args.sources.split(',')]
        print(f'E-SRC active — allow-list: {sources}', file=sys.stderr)

    print(f'Reading {args.input} ...', file=sys.stderr)
    df = pd.read_parquet(args.input)
    print(f'  {len(df):,} rows loaded.', file=sys.stderr)

    print('Applying filter v2.1 ...', file=sys.stderr)
    out = apply_filter(df, sources)
    print(f'  Done ({len(out):,} rows classified).', file=sys.stderr)

    print(f'Writing {args.output_parquet} ...', file=sys.stderr)
    out.to_parquet(args.output_parquet, index=False)

    print(f'Writing {args.output_csv} ...', file=sys.stderr)
    out.to_csv(args.output_csv, index=False)

    print_summary(out)

    if args.compare_v2:
        print(f'Loading v2 output for comparison: {args.compare_v2} ...', file=sys.stderr)
        df_v2 = pd.read_parquet(args.compare_v2)
        print_comparison(out, df_v2)


if __name__ == '__main__':
    main()
