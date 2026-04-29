import json
from dataclasses import dataclass

from anthropic import Anthropic
from bot.config import ANTHROPIC_API_KEY

# ── System prompt blocks ──────────────────────────────────────────────────────

_ENTRY_SCHEMA = """You are a quantitative analyst evaluating a stock trade signal.
Respond with ONLY valid JSON matching this exact schema:
{"conviction": <int 1-10>, "position_pct": <float>, "rationale": <str>, "entry": <"buy"|"skip">, "risk_flags": [<str>]}

## Conviction → Position Size Rules
- conviction 1-4: entry="skip", position_pct=0
- conviction 5-6: position_pct 1.0-2.0
- conviction 7-8: position_pct 3.0-5.0
- conviction 9-10: position_pct 6.0-8.0

## Entry Hurdle
- Only set entry="buy" if expected return exceeds estimated_cost_pct by at least 2x"""

_CONGRESSIONAL_RULES = """
## Congressional Signal Rules

## Lag Decay
- lag_days 15-30: penalise conviction -2
- lag_days 31-45: penalise conviction -3 and cap position_pct at 2.0

## Cluster Signal Boost
- cluster_count 2-3 (other members buying same stock in last 30d): +1 conviction
- cluster_count 4+: +2 conviction (strong institutional knowledge signal)

## Transaction Size
- Amount > $100,000: +1 conviction (large conviction trade)
- Amount $50,001-$100,000: full conviction
- Amount $15,001-$50,000: neutral (no bonus)"""

_FUNDAMENTAL_RULES = """
## Fundamental Factor Score Rules
The composite factor score (0-99) combines value, momentum, and quality percentile ranks within the S&P 500 + Russell 1000 universe.
- score 80-99: strong factor signal, +2 conviction
- score 60-79: moderate factor signal, +1 conviction
- score 40-59: neutral
- score <40: weak factor signal, -1 conviction"""

_BOTH_BONUS = """
## Combined Signal Bonus
A congressional member recently purchased this ticker (disclosure details not shown here) AND the fundamental factor screen flags it: +1 conviction bonus. Apply this bonus regardless of whether you see congressional details in the prompt."""

_RESEARCH_ADJUSTMENTS = """
## Fundamental Adjustment (if research provided)
- Cyclical company at peak earnings (high ROE, high margins, late-cycle sector like Materials/Energy): mentally normalize earnings — do NOT take headline P/E at face value
- Negative earnings (P/E = n/a): conviction -1 unless revenue growth >30% and sector is high-growth tech/biotech
- Clearly overvalued (EV/EBITDA >30x with <10% growth): conviction -2
- High short interest (>15% of float) with congressional purchase: SHORT SQUEEZE potential → +1 conviction
- Deteriorating fundamentals (revenue growth negative + margin compression): conviction -2
- Financially healthy, undervalued, positive momentum: conviction +1 to +2"""

_EXIT_SYSTEM = """You are a quantitative analyst reviewing an open stock position.
Respond with ONLY valid JSON: {"action": <"hold"|"exit"|"reduce">, "rationale": <str>}

## Actions
- exit: sell entire position at next open
- reduce: sell 50% at next open
- hold: keep position

## Exit Rules
- P&L < -12%: exit immediately (approaching hard stop — don't wait for -15%)
- P&L > +40%: exit (full profit-taking)
- P&L +25% to +40%: reduce (lock in half the gain; let the other half run)
- days_held > 60 with P&L < +5%: exit (cost of capital exceeds return; redeploy)
- days_held > 90: exit regardless (information advantage fully priced in by now)
- Hold if P&L -12% to +25% and no material negative news

## Research Adjustment
- If research shows deteriorating fundamentals (margins falling, revenue declining): exit even if P&L positive
- If research shows strong momentum + positive earnings growth: hold even near the +25% reduce level"""

_VALID_ENTRY_VALUES = {"buy", "skip"}
_VALID_ACTION_VALUES = {"hold", "exit", "reduce"}
_VALID_SIGNAL_TYPES = {"congressional", "fundamental", "both"}

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _build_entry_system(signal_type: str, has_disclosure: bool = True) -> str:
    if signal_type not in _VALID_SIGNAL_TYPES:
        raise ValueError(f"signal_type {signal_type!r} not in {_VALID_SIGNAL_TYPES}")
    parts = [_ENTRY_SCHEMA]
    # Only include congressional lag/cluster rules when actual disclosure data is present
    if signal_type in ("congressional", "both") and has_disclosure:
        parts.append(_CONGRESSIONAL_RULES)
    if signal_type in ("fundamental", "both"):
        parts.append(_FUNDAMENTAL_RULES)
    if signal_type == "both":
        parts.append(_BOTH_BONUS)
    parts.append(_RESEARCH_ADJUSTMENTS)
    return "\n".join(parts)


@dataclass(frozen=True)
class EntryScore:
    conviction: int
    position_pct: float
    rationale: str
    entry: str
    risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class ExitDecision:
    action: str
    rationale: str


def parse_entry_response(text: str) -> EntryScore:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned invalid JSON for entry: {text!r}") from exc
    conviction = int(data["conviction"])
    if not (1 <= conviction <= 10):
        raise ValueError(f"conviction {conviction} out of range 1-10")
    entry = data["entry"]
    if entry not in _VALID_ENTRY_VALUES:
        raise ValueError(f"entry {entry!r} not in {_VALID_ENTRY_VALUES}")
    return EntryScore(
        conviction=conviction,
        position_pct=float(data["position_pct"]),
        rationale=data["rationale"],
        entry=entry,
        risk_flags=tuple(data.get("risk_flags", [])),
    )


def parse_exit_response(text: str) -> ExitDecision:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned invalid JSON for exit: {text!r}") from exc
    action = data["action"]
    if action not in _VALID_ACTION_VALUES:
        raise ValueError(f"action {action!r} not in {_VALID_ACTION_VALUES}")
    return ExitDecision(action=action, rationale=data["rationale"])


def score_entry(
    disclosure: dict | None,
    committees: list[str],
    sector: str,
    lag_days: int,
    estimated_cost_pct: float,
    research: "ResearchReport | None" = None,
    cluster_count: int = 1,
    signal_type: str = "congressional",
    factor_score: int | None = None,
    ticker: str | None = None,
) -> EntryScore:
    from bot.researcher import format_research_for_prompt
    _ticker = (disclosure["ticker"] if disclosure else ticker) or "UNKNOWN"

    lines = [f"Ticker: {_ticker} | Sector: {sector}"]

    if signal_type in ("congressional", "both") and disclosure:
        lines += [
            f"Politician: {disclosure['politician']}",
            f"Transaction date: {disclosure['transaction_date']} | "
            f"Disclosure date: {disclosure['disclosure_date']}",
            f"Lag days: {lag_days}",
            f"Amount range: {disclosure['amount_range']}",
            f"Committees held: {', '.join(committees)}",
            f"Cluster count (other members buying same stock last 30d): {cluster_count}",
        ]

    if signal_type in ("fundamental", "both") and factor_score is not None:
        lines.append(f"Composite factor score: {factor_score}/99")

    lines.append(f"Estimated round-trip cost: {estimated_cost_pct:.2f}% of position")

    if research is not None:
        lines.append("\n" + format_research_for_prompt(research))

    lines.append("Score this signal.")
    prompt = "\n".join(lines)

    system_text = _build_entry_system(signal_type, has_disclosure=disclosure is not None)
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{"type": "text", "text": system_text,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_entry_response(resp.content[0].text)


def review_exit(ticker: str, entry_price: float, current_price: float,
                days_held: int, research: "ResearchReport | None" = None) -> ExitDecision:
    from bot.researcher import format_research_for_prompt
    pnl_pct = (current_price - entry_price) / entry_price * 100
    prompt = (
        f"Ticker: {ticker}\n"
        f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
        f"P&L: {pnl_pct:+.1f}%\n"
        f"Days held: {days_held}\n"
    )
    if research is not None:
        prompt += "\n" + format_research_for_prompt(research) + "\n"
    prompt += "Hold, reduce, or exit?"
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=[{"type": "text", "text": _EXIT_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_exit_response(resp.content[0].text)
