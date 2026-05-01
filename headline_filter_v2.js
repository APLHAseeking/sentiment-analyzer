// ============================================================
// Headline Filter v2 — STOXX Europe 600 Thesis
// Thomas Vromen, MSc Finance, Tilburg University 2025-2026
//
// Usage (Node.js — no dependencies):
//   node headline_filter_v2.js headlines.txt [firm_name] [--sources Reuters,Bloomberg]
//
// Arguments:
//   headlines.txt      Plain text file, one headline per line
//   firm_name          Optional focal firm name (enables E7 incidental-mention check)
//   --sources X,Y,Z    Optional comma-separated source allow-list (enables E-SRC)
//                      Each headline line may optionally be prefixed: SOURCE|headline text
//
// Output:
//   headline_filter.csv written to the current directory
// ============================================================

'use strict';
const fs = require('fs');

// ════════════════════════════════════════════════════════════
// RULE LISTS — edit these to tune the filter
// ════════════════════════════════════════════════════════════

// ── E-MKT: Market-data / ticker-feed patterns ────────────────
// Matches automated wire price bulletins like:
//   "Bouygues ADR (BOUYY: $10.38) decreases on robust volume; -2c [-0.2%] Vol Index 1.7"
const MKT_PATTERNS = [
  // Ticker in parens with currency price: (TICK: $12.34)
  /\([A-Z]{1,6}\.?[A-Z]?\s*:\s*[$£€]\d+[\d.,]*\)/,
  // Vol Index score
  /\bvol(?:ume)?\s+index\s+\d[\d.]*/i,
  // Change in brackets: -2c [-0.2%] or +4p [+0.3%]
  /[+\-]\d+(?:\.\d+)?[cp]\s*\[[+\-]\d+(?:\.\d+)?%\]/,
  // "decreases/increases on robust/light/average volume"
  /\b(?:decrease|increase|rise|fall|gain|drop)s?\s+on\s+(?:robust|light|average|heavy|thin|below.avg|above.avg)\s+volume\b/i,
  // ADR price bulletin
  /\bADR\s*\([A-Z]{1,6}:?\s*[$£€]\d[\d.,]*\)/i,
  // "[price] in [time] trading" with no other content (standalone price quote)
  /^[\w\s,.\-]+(?:at|@)\s*[$£€CHF]?\d[\d.,]+\s+in\s+(?:early|late|mid|morning|afternoon|pre-market|after-hours)\s+\w+\s+trading\.?\s*$/i,
  // Bare price move: "Barclays: volume 18.4m shares"
  /\bvolume\s+\d[\d.,]+[mk]?\s+shares\b/i,
  // Intraday high/low with price
  /\b(?:intraday|session)\s+(?:high|low)\s+of\s+[$£€CHF]?\d[\d.,]+/i,
];

// A headline is excluded under E-MKT if ≥2 markers match, or
// if the Vol Index pattern alone matches with no valuation channel.
function isMktData(h) {
  let hits = 0;
  let hasVolIdx = false;
  for (const p of MKT_PATTERNS) {
    if (p.test(h)) {
      hits++;
      if (p === MKT_PATTERNS[1]) hasVolIdx = true;
    }
  }
  return hits >= 2 || hasVolIdx;
}

// ── E1: Macroeconomic / market-wide keywords ─────────────────
const MACRO_KW = [
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
];

// ── E2: Sector-wide keywords ─────────────────────────────────
const SECTOR_KW = [
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
];

// ── E4: Price-move-only patterns ─────────────────────────────
const PRICE_ONLY = [
  /\bshares?\s+(?:rise|fall|gain|drop|climb|slip|jump|tumble|surge|plunge|rally|advance|retreat)\s+\d[\d.]*%/i,
  /\b(?:gains?|falls?|rises?|drops?|slides?|surges?|slumps?|climbs?|rallies)\s+\d[\d.]*\s*(?:%|percent|points?|p)\b/i,
  /\b(?:up|down)\s+\d[\d.]*\s*(?:%|percent)\s+on\s+(?:monday|tuesday|wednesday|thursday|friday|the\s+day|trading|session)\b/i,
  /\b(?:leads?|tops?|paces?)\s+(?:the\s+)?(?:dax|ftse|cac|stoxx|index|market|gainers|fallers|risers|decliners)\b/i,
  /\bshares?\s+(?:hit|touch|reach)\s+(?:a\s+)?(?:new\s+)?(?:52.week|record|multi.year)\s+(?:high|low)\b/i,
  /\b(?:gains?|falls?|rises?|drops?)\s+\d+\s+(?:points?|bps|basis\s+points?)\s+(?:on|in)\s+(?:monday|tuesday|wednesday|thursday|friday|morning|afternoon|trading|session)\b/i,
];

// ── E5: Analyst reiteration patterns ─────────────────────────
const REITERATE = [
  /\b(?:reiterates?|maintains?|keeps?|retains?)\s+(?:its\s+)?(?:buy|sell|hold|neutral|outperform|underperform|overweight|underweight|market.perform)\s+(?:rating|recommendation|stance|call)\b/i,
  /\bno\s+change\s+to\s+(?:rating|outlook|target|guidance|recommendation)\b/i,
  /\bprice\s+target\s+unchanged\b/i,
  /\btarget\s+price\s+unchanged\b/i,
];

// ── E6: Administrative / routine patterns ────────────────────
const ADMIN = [
  /\b(?:to\s+)?(?:release|report|publish|announce)\s+(?:q[1-4]|quarterly|annual|half.year|full.year)\s+results?\s+on\b/i,
  /\bannounces?\s+(?:agm|annual general meeting)\s+date\b/i,
  /\bfiles?\s+(?:annual\s+report|form\s+\w+|regulatory\s+filing|20-f|10-k|6-k)\b/i,
  /\bschedules?\s+(?:results?|earnings)\s+(?:for|on)\b/i,
  /:\s*(?:update|filing|announcement)\s*$/i,
  /^[\w\s,.\-]+:\s*(?:update|filing|announcement|statement)\s*$/i,
  /\bmanagement\s+speaks?\b/i,
  /\bcompany\s+announcement\b/i,
  /\bex-dividend\s+date\b/i,
  /\brecord\s+date\s+for\s+dividend\b/i,
];

// ── I-AC: Analyst change (upgrade/downgrade/initiation) ──────
const ANALYST_CHANGE = [
  /\b(?:upgrades?|downgrades?|initiates?\s+coverage(?:\s+of)?)\b/i,
];

// ── I1+I2: Valuation channel inclusion patterns ───────────────
// A headline must match ≥1 of these to be included.
const VALUATION = [
  // Earnings, revenue, guidance, profit
  /\b(?:earnings?|net\s+income|operating\s+profit|ebit\b|ebitda\b|revenues?\s+(?:beat|miss|grow|fall|rise|decline)|profit\s+(?:warn|jump|slump|rise|fall|surge)|loss\s+widen|margin\b|cash\s+flow|guidance\s+(?:raise|cut|lower|lift|trim|revise|upgrad|downgrad)|forecast\s+(?:raise|cut|lower|lift|revise)|outlook\s+(?:raise|cut|upgrad|downgrad|lift|lower|improv|deteriorat)|warn(?:ing)?\b|results?\s+(?:beat|miss|top|disappoint)|quarterly\s+result|full.year\s+result)\b/i,
  // Dividends & buybacks
  /\b(?:dividend|buyback|share\s+repurchase|capital\s+return|special\s+dividend)\b/i,
  // M&A & strategic deals
  /\b(?:acqui(?:re|res|red|ring|sition)|merger\b|takeover\b|bid\s+for|offer\s+for|tender\s+offer|divest(?:s|ed|ing|iture)?|disposal\b|spin.?off|joint\s+venture|partnership\s+deal|strategic\s+alliance|agrees?\s+to\s+(?:buy|sell|acquire|merge))\b/i,
  // Contracts & orders
  /\b(?:wins?\s+(?:contract|deal|order|tender)|secures?\s+(?:contract|deal|order)|loses?\s+(?:contract|deal)|awarded\s+(?:contract|deal))\b/i,
  // Products & launches
  /\b(?:launch(?:es|ed)?\s+(?:new|product|\w+\s+model)|new\s+product\b|product\s+launch\b)\b/i,
  // Facility changes
  /\b(?:plant|factory|facility|site)\s+(?:clos(?:e|ure|ing)|shutdow|idl)\b/i,
  // Executive changes
  /\b(?:(?:ceo|cfo|coo|chairman|president)\b.*\b(?:depart|resign|step\s+down|replac|appoint|nam|leav)|chief\s+executive.*(?:depart|resign|step\s+down|replac|appoint|nam|leav)|appoints?\s+(?:new\s+)?(?:ceo|cfo|coo|chairman|president)\b)\b/i,
  // Regulatory & approvals
  /\b(?:regulator\w*\s+(?:approv|fine|sanction|ban|reject|block|clear|order|probe|investigat)|fda\s+(?:approv|reject|clear)|ema\s+(?:approv|reject|clear)|eu\s+commission\s+(?:fine|approv|block|clear))\b/i,
  // Litigation & legal
  /\b(?:fine\b|penalt(?:y|ies)\b.*(?:\d|\bmillion\b|\bbillion\b)|litigation\b|lawsuit\b|legal\s+(?:action|proceed|settl|charg)|settl(?:e|ement|ing)\b|verdict\b|judgment\b|court\s+(?:rules?|order))\b/i,
  // Credit ratings
  /\b(?:credit\s+rating\b|(?:downgraded?|upgraded?)\s+(?:to|by)\b.*\b(?:moody|fitch|s&p|standard.*poor)|moody.*(?:downgrad|upgrad)|fitch.*(?:downgrad|upgrad))\b/i,
  // Capital markets
  /\b(?:(?:bond|debt|equity)\s+(?:issu|offer|rais)|rights\s+issue\b|capital\s+(?:rais|increas)|refinanc)\b/i,
  // Accounting & audit
  /\b(?:restat(?:e|ement|ing)\b|accounting\s+(?:error|restat|irregularit)|audit\s+(?:qualif|concern|fail)|impairment\b|writedown\b|write-down\b|goodwill\s+impairment)\b/i,
  // Activism & governance
  /\b(?:activist\b|proxy\s+(?:fight|contest|battle)|hostile\s+(?:bid|takeover))\b/i,
  // Cybersecurity
  /\b(?:cyberattack\b|data\s+breach\b|ransomware\b|hack(?:ed|ing)\b.*(?:firm|company|group|corp))\b/i,
  // Labour & operations
  /\b(?:strike\b.*(?:worker|employee|staff|union)|industrial\s+action\b|labour\s+dispute\b)\b/i,
  // Clinical / pharma
  /\b(?:phase\s+(?:i+|[123])\s+(?:trial|result|data|read|fail|success)\b|clinical\s+trial\s+(?:result|fail|success|data)\b)\b/i,
  // Resource discovery
  /\b(?:discovery\b.*(?:oil|gas|mineral)|oil\s+field|gas\s+field)\b/i,
  // Restructuring & job cuts
  /\b(?:restructur\b|job\s+(?:cut|reduc|loss)|layoff\b|redundanc\b|workforce\s+(?:reduc|cut))\b/i,
];

function hasVal(h) {
  return VALUATION.some(r => r.test(h));
}

// ════════════════════════════════════════════════════════════
// SOURCE ALLOW-LIST (optional — Section 8 of framework)
// ════════════════════════════════════════════════════════════

// Default allow-list — edit or extend as needed.
// To disable, pass an empty array or don't supply --sources.
const DEFAULT_SOURCES = [
  'reuters', 'bloomberg', 'dow jones', 'dow jones newswires',
  'pr newswire', 'business wire', 'globe newswire',
  'afx', 'agence france-presse', 'afp',
  'financial times', 'ft.com',
  'wall street journal', 'wsj',
  'handelsblatt', 'les echos',
  'mni market news', 'mni',
];

// ════════════════════════════════════════════════════════════
// CORE CLASSIFIER
// ════════════════════════════════════════════════════════════

/**
 * Classify a single headline.
 *
 * @param {string}   raw      Headline text (may be prefixed "SOURCE|text")
 * @param {string}   firm     Focal firm name (optional)
 * @param {object}   seen     Deduplication map {normText: true}
 * @param {string[]|null} sources  Allow-list of source names (lowercase). Null = disabled.
 * @returns {{ source:string, headline:string, d:string, r:string, j:string }|null}
 */
function classify(raw, firm = '', seen = {}, sources = null) {
  const trimmed = raw.trim();
  if (!trimmed) return null;

  // Parse optional source prefix: "Reuters|Headline text..."
  let source = '';
  let h = trimmed;
  const pipeIdx = trimmed.indexOf('|');
  if (pipeIdx > 0 && pipeIdx < 60) {
    source = trimmed.slice(0, pipeIdx).trim();
    h = trimmed.slice(pipeIdx + 1).trim();
  }

  const hl = h.toLowerCase();
  const fl = firm.toLowerCase().trim();

  // ── E-SRC: source allow-list ─────────────────────────────────
  if (sources && sources.length > 0 && source) {
    const srcLow = source.toLowerCase();
    const allowed = sources.some(s => srcLow.includes(s));
    if (!allowed) {
      return { source, headline: h, d: 'EXCLUDE', r: 'E-SRC', j: `Source "${source}" not in allow-list` };
    }
  }

  // ── E3: Duplicate ────────────────────────────────────────────
  const norm = hl.replace(/\s+/g, ' ').trim();
  if (seen[norm]) {
    return { source, headline: h, d: 'EXCLUDE', r: 'E3', j: 'Duplicate or near-duplicate' };
  }
  seen[norm] = true;

  // ── E8: Too short ────────────────────────────────────────────
  if (h.length < 12) {
    return { source, headline: h, d: 'EXCLUDE', r: 'E8', j: 'Too short to be interpretable' };
  }

  // ── E-MKT: Market-data ticker feed ──────────────────────────
  if (isMktData(h)) {
    if (!hasVal(h)) {
      return { source, headline: h, d: 'EXCLUDE', r: 'E-MKT', j: 'Automated market-data / ticker-feed bulletin; no new information' };
    }
  }

  // ── E1: Macroeconomic ────────────────────────────────────────
  if (MACRO_KW.some(kw => hl.includes(kw))) {
    if (!hasVal(h)) {
      return { source, headline: h, d: 'EXCLUDE', r: 'E1', j: 'Macroeconomic/market-wide headline; no firm-specific valuation content' };
    }
  }

  // ── E2: Sector-wide ──────────────────────────────────────────
  if (SECTOR_KW.some(kw => hl.includes(kw))) {
    if (!hasVal(h)) {
      return { source, headline: h, d: 'EXCLUDE', r: 'E2', j: 'Sector-wide headline; no firm-specific valuation content' };
    }
  }

  // ── E6: Administrative / routine ─────────────────────────────
  if (ADMIN.some(r => r.test(h))) {
    return { source, headline: h, d: 'EXCLUDE', r: 'E6', j: 'Administrative/routine disclosure; no valuation-relevant new information' };
  }

  // ── E4: Price-move only ──────────────────────────────────────
  if (PRICE_ONLY.some(r => r.test(h))) {
    if (!hasVal(h)) {
      return { source, headline: h, d: 'EXCLUDE', r: 'E4', j: 'Price-move headline with no new underlying information' };
    }
  }

  // ── E5: Analyst reiteration ──────────────────────────────────
  if (REITERATE.some(r => r.test(h))) {
    return { source, headline: h, d: 'EXCLUDE', r: 'E5', j: 'Analyst reiteration/unchanged rating; no new information' };
  }

  // ── E7: Firm not mentioned (only when firm name supplied) ────
  if (fl && !hl.includes(fl)) {
    if (!hasVal(h)) {
      return { source, headline: h, d: 'EXCLUDE', r: 'E7', j: 'Focal firm not mentioned; no identifiable valuation channel' };
    }
  }

  // ── I-AC: Analyst upgrade/downgrade with valuation content ───
  if (ANALYST_CHANGE.some(r => r.test(h)) && hasVal(h)) {
    return { source, headline: h, d: 'INCLUDE', r: 'I-AC', j: 'Analyst rating change with valuation-relevant content' };
  }

  // ── E8 (catch-all): No valuation channel ─────────────────────
  if (!hasVal(h)) {
    return { source, headline: h, d: 'EXCLUDE', r: 'E8', j: 'No identifiable valuation channel; too vague or generic' };
  }

  // ── I1+I2: Default include ───────────────────────────────────
  return { source, headline: h, d: 'INCLUDE', r: 'I1+I2', j: 'Firm-specific headline with identifiable valuation channel' };
}

// ════════════════════════════════════════════════════════════
// PROGRAMMATIC API (use as module)
// ════════════════════════════════════════════════════════════

/**
 * Filter an array of headline strings.
 *
 * @param {string[]} headlines   Array of headline strings (may include "SOURCE|text" prefix)
 * @param {string}   firm        Focal firm name (optional)
 * @param {string[]|null} sources  Source allow-list (optional, null = disabled)
 * @returns {Array<{n,source,headline,d,r,j}>}
 */
function filterHeadlines(headlines, firm = '', sources = null) {
  const seen = {};
  const results = [];
  for (const h of headlines) {
    const r = classify(h, firm, seen, sources);
    if (r) results.push({ n: results.length + 1, ...r });
  }
  return results;
}

module.exports = { classify, filterHeadlines, VALUATION, MACRO_KW, SECTOR_KW, MKT_PATTERNS };

// ════════════════════════════════════════════════════════════
// CLI ENTRY POINT
// ════════════════════════════════════════════════════════════

function parseArgs() {
  const args = process.argv.slice(2);
  let inputFile = null;
  let firmName  = '';
  let sources   = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--sources' && args[i + 1]) {
      sources = args[i + 1].toLowerCase().split(',').map(s => s.trim());
      i++;
    } else if (!inputFile) {
      inputFile = args[i];
    } else if (!firmName) {
      firmName = args[i];
    }
  }
  return { inputFile, firmName, sources };
}

function escCsv(v) { return '"' + String(v).replace(/"/g, '""') + '"'; }

function main() {
  const { inputFile, firmName, sources } = parseArgs();

  if (!inputFile) {
    console.error('Usage: node headline_filter_v2.js <headlines.txt> [firm_name] [--sources Reuters,Bloomberg]');
    console.error('');
    console.error('  headlines.txt    One headline per line.');
    console.error('                   Optionally prefix each line with source: "Reuters|Headline text"');
    console.error('  firm_name        Optional focal firm name (activates E7 incidental-mention check)');
    console.error('  --sources X,Y    Optional comma-separated source allow-list (activates E-SRC)');
    process.exit(1);
  }

  if (!fs.existsSync(inputFile)) {
    console.error('File not found: ' + inputFile);
    process.exit(1);
  }

  const lines   = fs.readFileSync(inputFile, 'utf8').split('\n');
  const results = filterHeadlines(lines, firmName, sources);

  const inc = results.filter(r => r.d === 'INCLUDE').length;
  const exc = results.filter(r => r.d === 'EXCLUDE').length;
  const pct = n => Math.round(n / results.length * 100);

  console.log('');
  console.log('Headline filter v2 — results');
  console.log('─'.repeat(40));
  console.log(`  Total   : ${results.length}`);
  console.log(`  Include : ${inc} (${pct(inc)}%)`);
  console.log(`  Exclude : ${exc} (${pct(exc)}%)`);
  if (firmName) console.log(`  Firm    : ${firmName}`);
  if (sources)  console.log(`  Sources : ${sources.join(', ')}`);
  console.log('');

  // Rule breakdown
  const ruleCounts = {};
  for (const r of results) ruleCounts[r.r] = (ruleCounts[r.r] || 0) + 1;
  console.log('  Rule breakdown:');
  for (const [rule, count] of Object.entries(ruleCounts).sort()) {
    console.log(`    ${rule.padEnd(10)} ${count}`);
  }
  console.log('');

  const header = ['#', 'Source', 'Headline', 'Decision', 'Rule', 'Reason'];
  const rows   = results.map(r => [r.n, r.source, r.headline, r.d, r.r, r.j]);
  const csv    = [header, ...rows].map(row => row.map(escCsv).join(',')).join('\n');

  const outFile = 'headline_filter.csv';
  fs.writeFileSync(outFile, csv, 'utf8');
  console.log('Results written to: ' + outFile);
  console.log('');
}

if (require.main === module) main();
