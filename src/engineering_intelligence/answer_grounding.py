"""Deterministic metadata presentation and narrow guards for observed answer failures.

These checks deliberately do not claim to validate arbitrary natural-language facts.
No database access, model calls, SQL, source-text execution, or current-time calculations.
"""

import math
import re
from datetime import datetime, timezone


SEMANTIC_LIMITS = (
    "Freshness measures time since extraction, not PR open age or subsequent GitHub events. "
    "Open PR age measures creation to extraction. "
    "PR counts do not establish developer productivity, even with author attribution. "
    "Review activity and bottleneck causes are not measured."
)

MODEL_METADATA_OMISSIONS = {
    "latest_extraction_age_hours", "oldest_pr_observation_age_hours", "newest_pr_observation_age_hours",
    "stale_after_hours", "checked_at_utc", "source_url", "source_start_url", "observation_source_url",
}


def model_tool_result(result):
    """An explicit model view; full validated results stay in tool_calls and the appendix.

    Remove only metadata owned by Python presentation. Keep PR ages, merge durations,
    extraction timestamps, flags, and page counts; no values or conclusions are invented.
    """
    def project(value):
        if isinstance(value, dict):
            return {key: project(item) for key, item in value.items() if key not in MODEL_METADATA_OMISSIONS}
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    view = project(result)
    if result.get("status") == "ok":
        view["model_view_metadata"] = "Numerical freshness values (including the policy threshold), check timestamps, and API starting URLs are intentionally omitted from this model view; full evidence is retained and rendered by Python. PR ages, KPIs, extraction timestamps, pages fetched and quality flags are unchanged."
    return view


def freshness_text(freshness):
    hours = freshness.get("latest_extraction_age_hours")
    if type(hours) not in (int, float) or not math.isfinite(hours) or hours < 0:
        return "unknown (missing or future extraction timestamp)"
    return f"{hours:.2f} hours ({hours / 24:.2f} days)"


def metadata_lines(evidence):
    """Render scalar evidence only; never interpret a URL, title, or source error as instructions."""
    quality = evidence.get("data_quality") or {}
    latest = quality.get("latest_ingestion_run") or {}
    freshness = quality.get("freshness") or {}
    lines = [
        f"  Latest-run pages_fetched={latest.get('pages_fetched') if latest.get('pages_fetched') is not None else 'unknown'}; "
        f"pagination_complete={latest.get('pagination_complete')}; stop_reason={latest.get('stop_reason')}. "
        "The starting URL is not evidence of page count or completeness.",
        f"  Extraction age at check: {freshness_text(freshness)}; "
        f"latest_extraction_is_stale={freshness.get('latest_extraction_is_stale')}; "
        f"stale observations across all PR states={freshness.get('stale_pr_observation_count')}.",
        f"  Contributing PR-run pagination_complete={quality.get('all_observation_runs_pagination_complete')}.",
    ]
    for run in quality.get("observation_runs", []):
        if run.get("run_id") != latest.get("run_id"):
            lines.append(f"  Contributing run {run.get('run_id')}: extraction UTC={run.get('extracted_at_utc')}; "
                         f"pages_fetched={run.get('pages_fetched') if run.get('pages_fetched') is not None else 'unknown'}; "
                         f"pagination_complete={run.get('pagination_complete')}; stop_reason={run.get('stop_reason')}.")
    return lines


def utc_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def supported_observation_times(results):
    times = []
    for result in results:
        evidence = result.get("evidence", {})
        quality = evidence.get("data_quality") or {}
        times.extend([evidence.get("earliest_pr_extraction_at_utc"), evidence.get("latest_pr_extraction_at_utc"),
                      (quality.get("latest_ingestion_run") or {}).get("extracted_at_utc"),
                      (quality.get("freshness") or {}).get("checked_at_utc")])
        times.extend(run.get("extracted_at_utc") for run in quality.get("observation_runs", []))
        data = result.get("data") or {}
        times.append(data.get("observed_at_utc"))
        times.extend(pr.get("observed_at_utc") for pr in data.get("pull_requests", []))
    return [parsed for value in times if (parsed := utc_timestamp(value)) is not None]


PAGE_NUMBER = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|first"
PAGE_CLAIMS = re.compile(
    rf"\bonly (?:the )?({PAGE_NUMBER}) page\b|"
    rf"\b(?:fetched|retrieved|captured|processed)\s+(?:only\s+)?({PAGE_NUMBER})\s+(?:api\s+)?pages?\b|"
    rf"\b({PAGE_NUMBER})\s+(?:api\s+)?pages?\s+(?:(?:was|were|have been|had been)\s+)?(?:fetched|retrieved|captured|processed)\b|"
    rf"\b(?:stopped|ended)\s+(?:at|after)\s+page\s+({PAGE_NUMBER})\b|"
    rf"\bpages_fetched\s*[:=]\s*({PAGE_NUMBER})\b", re.IGNORECASE)
WORD_NUMBERS = {word: number for number, word in enumerate(
    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"))}
WORD_NUMBERS["first"] = 1


def grounding_problem(content, successful):
    """Fail closed on supported patterns; ambiguous attribution needs a narrower rewrite."""
    text = re.sub(r"[*`]", "", content)
    scope = successful
    for sentence in re.split(r"\n|(?<=[.!?])\s+", text):
        lower = sentence.lower()
        identifiers = {value.rstrip(".,;") for value in re.findall(
            r"(?<![\w/.-])([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)(?![\w/.-])", lower)}
        if identifiers:
            named = [result for result in successful if result.get("repository", "").lower() in identifiers]
            # Slash-separated state descriptions such as open/merged are not repository switches.
            if named or any(lower.lstrip("- ").startswith(value) or re.search(re.escape(value) + r"\s*:", lower)
                            for value in identifiers):
                scope = named
        qualities = [result.get("evidence", {}).get("data_quality") or {} for result in scope]
        latest = [quality.get("latest_ingestion_run") or {} for quality in qualities]
        if re.search(r"\bpage\s*=\s*\d+", lower):
            return "Do not infer pagination from URL parameters. Use stored pages_fetched, pagination_complete and stop_reason."
        if re.search(r"\b(?:stopped|ended)\s+(?:at|after)\s+page\s+", lower):
            return "The warehouse stores successful page count, not a final page index. Use pages_fetched and stop_reason."
        for match in PAGE_CLAIMS.finditer(lower):
            value = next(group for group in match.groups() if group is not None)
            number = int(value) if value.isdigit() else WORD_NUMBERS[value]
            if not latest or any(type(run.get("pages_fetched")) is not int or run["pages_fetched"] != number for run in latest):
                return "The page-count claim is unsupported or ambiguous. Use stored pages_fetched; unknown counts must stay unknown."
        complete_claim = re.search(r"\bpagination\s+(?:is |was )?(?:not )?(?:incomplete|completed?)\b|\b(?:incomplete|complete) pagination\b", lower)
        if complete_claim:
            incomplete = "incomplete" in complete_claim[0] or "not complete" in complete_claim[0]
            prefix = lower[max(0, complete_claim.start() - 30):complete_claim.start()]
            suffix = lower[complete_claim.end():]
            incomplete = incomplete or bool(re.search(r"(?:\bnot|\black of|\blacks?|\bwithout)\s+$", prefix))
            incomplete = incomplete or bool(re.match(r"\s+(?:status\s+)?(?:of\s+|is\s+|[:=]\s*)?false\b", suffix))
            values = [run.get("pagination_complete") for run in latest] + [quality.get("all_observation_runs_pagination_complete") for quality in qualities]
            if (not incomplete and (not values or any(value is not True for value in values))) or (incomplete and False not in values):
                return "The pagination-completeness claim is unsupported. Unknown is not complete or incomplete; use the stored flags."
        # Python owns numerical freshness presentation, avoiding LLM unit conversions.
        durations = re.findall(r"\b(\d+(?:\.\d+)?)\s*(hours?|hrs?|days?|months?|years?)\b", lower)
        if re.search(r"fresh|stale|extract.{0,100}(?:ago|old)|extract(?:ion|ed)\s+(?:age|freshness)|(?:since|after) extraction|data.{0,15}old", lower) and durations:
            # A configured policy threshold is not an extraction-age conversion.
            policy_only = bool(re.search(r"policy|threshold", lower)) and bool(qualities) and all(
                unit.startswith(("hour", "hr")) and all(float(number) == (quality.get("freshness") or {}).get("stale_after_hours")
                                                       for quality in qualities) for number, unit in durations)
            policy_only = policy_only and not re.search(r"(?:extracted|extraction).{0,60}(?:ago|old)|older than|more than|exceeds", lower)
            threshold_bound = bool(re.search(r"\b(?:older than|more than|over|exceeds|stale after)\b", lower)) and bool(qualities) and all(
                unit.startswith(("hour", "hr")) and all(float(number) == (quality.get("freshness") or {}).get("stale_after_hours")
                                                       and (quality.get("freshness") or {}).get("latest_extraction_is_stale") is True
                                                       for quality in qualities) for number, unit in durations)
            if not policy_only and not threshold_bound:
                return "Omit numerical extraction-age statements from model prose. Python renders freshness hours/days and checked-at time; a correctly quoted policy threshold is allowed."
        if re.search(r"extract|observ(?:ed at|ed on|ation timestamp)|checked at", lower):
            times = supported_observation_times(scope)
            for token in re.findall(r"\b\d{4}-\d{2}-\d{2}(?:T[0-9A-Za-z:+.-]+)?", sentence):
                timestamp = token.rstrip(".")
                if "T" in timestamp:
                    valid = utc_timestamp(timestamp) in times
                else:
                    valid = any(value.date().isoformat() == timestamp for value in times)
                if not valid:
                    return "The extraction/observation timestamp is unsupported for the named repository. Use exact returned UTC timestamps."
        negative = re.search(r"\b(?:not|cannot|can't|doesn't|never|no evidence)\b", lower)
        if not negative and re.search(
                r"stale.{0,80}(?:indicat|means|implies|proves).{0,140}(?:open|closed|merged|old pr|no longer current)|"
                r"(?:since|after) extraction.{0,80}(?:have|has|were).{0,20}(?:closed|merged)", lower):
            return "Stale observations do not establish PR open age or subsequent GitHub events."
        if not negative and re.search(
                r"productiv.{0,100}(?:require|need|counts|authorship|measur|based on)|"
                r"(?:counts|authorship|author attribution).{0,100}(?:establish|measur|rank|assess|judge|enable).{0,60}(?:productiv|performance)", lower):
            return "PR counts cannot establish developer productivity, even with author attribution. Do not suggest otherwise."
        if not negative and re.search(r"(?:reviews?|bottlenecks?).{0,100}(?:because|caused by|due to)", lower):
            return "Review activity and bottleneck causes are not measured; do not infer causes from these tools."
    return None
