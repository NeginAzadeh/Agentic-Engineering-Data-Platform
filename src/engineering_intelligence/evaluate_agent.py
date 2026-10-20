"""Evaluate the unchanged local Agent; never treat automated checks as prose accuracy."""

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .agent_tools import AgentTools, DEFAULT_DATABASE, validate_repository
from .local_agent import DEFAULT_MODEL, LocalAgent, repository_scope
from .report_kpis import METRIC_NAMES
from .tool_schemas import TOOL_SCHEMAS


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_DIR / "evaluations" / "agent_cases.json"
REPORT_DIR = PROJECT_DIR / "data" / "reports"
ALLOWED_TOOLS = {schema["name"] for schema in TOOL_SCHEMAS}
METRIC_LABELS = {
    "total_observed_prs": r"total (?:observed )?prs|observed pr count|total_observed_prs",
    "open_prs": r"open prs|open_prs",
    "merge_rate": r"merge rate|merge_rate",
    "median_time_to_merge_hours": r"median (?:time to merge|merge time)|median_time_to_merge_hours",
    "average_time_to_merge_hours": r"(?:average|mean) (?:time to merge|merge time)|average_time_to_merge_hours",
}
NUMBER = r"(-?\d+(?:\.\d+)?)\s*(%|percent|hours?|hrs?|days?|minutes?|mins?)?"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def load_cases(path=DEFAULT_DATASET):
    """Fail early on a malformed test contract, before invoking any model."""
    dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    if dataset.get("schema_version") != 1 or not isinstance(dataset.get("cases"), list) or not dataset["cases"]:
        raise ValueError("Dataset must have schema_version 1 and a nonempty cases list.")
    ids = set()
    for case in dataset["cases"]:
        if not isinstance(case.get("id"), str) or not case["id"] or case["id"] in ids:
            raise ValueError("Case IDs must be unique nonempty strings.")
        ids.add(case["id"])
        if not isinstance(case.get("question"), str) or not 0 < len(case["question"].strip()) <= 4000:
            raise ValueError("Each question must contain 1 through 4000 characters.")
        if case.get("expected_behavior") not in ("answer", "withhold"):
            raise ValueError("expected_behavior must be answer or withhold.")
        if not isinstance(case.get("category"), str) or not case["category"]:
            raise ValueError("Each case needs a category.")
        if not isinstance(case.get("human_review_criteria"), list) or not case["human_review_criteria"]:
            raise ValueError("Each case needs explicit human review criteria.")
        if not isinstance(case.get("expected_calls"), list):
            raise ValueError("expected_calls must be a list.")
        if not isinstance(case.get("allowed_tools"), list) or not set(case["allowed_tools"]) <= ALLOWED_TOOLS:
            raise ValueError("allowed_tools must use the existing allowlist.")
        for expected in case["expected_calls"]:
            name, arguments = expected.get("tool"), expected.get("arguments")
            if name not in case["allowed_tools"] or expected.get("status") not in ("ok", "not_found"):
                raise ValueError("Expected calls require an allowed tool and ok/not_found status.")
            # Reuse production argument validation, but replace the database operation.
            # A dataset is trusted configuration, never passed through to SQL or code.
            # Validation lives in the public method; a missing database stops before SQL.
            with TemporaryDirectory() as directory:
                check = AgentTools(Path(directory) / "absent.duckdb").call_tool(name, arguments)
            if check.get("error", {}).get("code") != "warehouse_unavailable":
                raise ValueError("Invalid expected tool arguments.")
        for check in case.get("numerical_checks", []):
            validate_repository(check["repository"])
            if check.get("metric") not in METRIC_LABELS or check["metric"] not in METRIC_NAMES:
                raise ValueError("Numerical check uses an unsupported metric.")
            if not any(item["tool"] == "get_repository_kpis" and
                       item["arguments"]["repository"].lower() == check["repository"].lower()
                       for item in case["expected_calls"]):
                raise ValueError("Numerical checks require a corresponding KPI evidence expectation.")
    return dataset["cases"]


def same_arguments(actual, expected):
    if not isinstance(actual, dict) or set(actual) != set(expected):
        return False
    for key, value in expected.items():
        observed = actual[key]
        if key == "repository":
            try:
                if validate_repository(observed) != validate_repository(value):
                    return False
            except ValueError:
                return False
        elif isinstance(observed, bool) or not isinstance(observed, (int, float)) or observed != value:
            return False
        elif key in ("pr_number", "limit") and type(observed) is not int:
            return False
    return True


def model_answer(result):
    """Do not credit the deterministic Python appendix as LLM-generated evidence use."""
    return result.get("answer", "").split("\n\nEvidence and limits:", 1)[0]


def stable_quality(quality):
    """Ignore only elapsed-clock values; retain stale flags, warnings, errors, and provenance."""
    if quality is None:
        return None
    quality = json.loads(json.dumps(quality))
    freshness = quality.get("freshness", {})
    for key in ("checked_at_utc", "latest_extraction_age_hours", "oldest_pr_observation_age_hours", "newest_pr_observation_age_hours"):
        freshness.pop(key, None)
    return quality


def same_evidence(retrieved, reference):
    expected = reference.get("evidence", {})
    actual = retrieved.get("evidence", {})
    keys = ("repository_url", "earliest_pr_extraction_at_utc", "latest_pr_extraction_at_utc", "observation_run_ids")
    return (bool(expected) and actual.get("github_live_status_verified") is False and
            all(actual.get(key) == expected.get(key) for key in keys) and
            stable_quality(actual.get("data_quality")) == stable_quality(expected.get("data_quality")))


def numeric_claims(text, repository, metric):
    """Conservative labelled lines/Markdown tables only; ambiguous prose needs review."""
    labels = METRIC_LABELS[metric]
    claims = []
    context = None
    table_columns = None
    for raw_line in text.splitlines():
        line = re.sub(r"[*`]", "", raw_line).lower()
        label_pattern = labels
        mentioned = repository_scope(line)
        if len(mentioned) == 1:
            context = mentioned[0]
        elif len(mentioned) > 1:
            context = None
        if "|" in line:
            columns = [cell.strip() for cell in line.strip(" |").split("|")]
            metric_columns = [i for i, cell in enumerate(columns) if re.search(label_pattern, cell)]
            if metric_columns and not mentioned:
                table_columns = [(i, columns[i]) for i in metric_columns]
                continue
            if table_columns and mentioned == [repository]:
                for index, header in table_columns:
                    if index < len(columns):
                        match = re.fullmatch(NUMBER, columns[index])
                        if match:
                            unit = match[2] or ("hours" if "hour" in header else
                                               "days" if "day" in header else
                                               "%" if "%" in header or "percent" in header else "")
                            claims.append((match[1], unit, raw_line))
                continue
        if context != repository:
            continue
        # Bind the number directly to its metric label; never search for a value anywhere in prose.
        label_line = re.sub(r"^\s*(?:[-+•]|\d+\.)?\s*", "", line)
        label_line = re.sub(rf"^{re.escape(repository)}\s*[:—-]\s*", "", label_line)
        pattern = rf"(?:{label_pattern})\s*(?:\((hours?|days?|%)\))?\s*[:=]\s*{NUMBER}\s*(?:\([^\n]*\))?\s*[.,;]?"
        match = re.fullmatch(pattern, label_line)
        if match:
            claims.append((match[2], match[3] or match[1] or "", raw_line))
    return claims


def check_numbers(case, result, references):
    checks = []
    for specification in case.get("numerical_checks", []):
        repository, metric = specification["repository"].lower(), specification["metric"]
        reference = next((item["result"] for item in references if item["tool"] == "get_repository_kpis"
                          and item["arguments"]["repository"].lower() == repository), {})
        expected = (reference.get("data") or {}).get("metrics", {}).get(metric)
        entries = []
        for number, unit, line in numeric_claims(model_answer(result), repository, metric):
            value = float(number)
            factor = 1.0
            if metric.endswith("_hours"):
                if unit.startswith("day"):
                    factor = 24.0
                elif unit.startswith("min"):
                    factor = 1 / 60
                elif not unit.startswith(("hour", "hr")):
                    entries.append({"text": line, "status": "human_review", "reason": "Duration unit missing."})
                    continue
            elif metric == "merge_rate":
                if unit in ("%", "percent"):
                    factor = 0.01
                elif unit or abs(value) > 1:
                    entries.append({"text": line, "status": "human_review", "reason": "Rate unit ambiguous."})
                    continue
            elif unit:
                entries.append({"text": line, "status": "human_review", "reason": "Unexpected count unit."})
                continue
            decimals = len(number.split(".")[1]) if "." in number else 0
            tolerance = (0.5 * 10 ** -decimals * factor + 1e-9) if not metric.endswith("_prs") else 1e-9
            matches = isinstance(expected, (int, float)) and math.isfinite(expected) and abs(value * factor - expected) <= tolerance
            entries.append({"text": line, "value_in_metric_units": value * factor,
                            "rounding_tolerance": tolerance, "status": "verified" if matches else "mismatch"})
        status = ("mismatch" if any(entry["status"] == "mismatch" for entry in entries) else
                  "verified" if entries and all(entry["status"] == "verified" for entry in entries) else "human_review")
        checks.append({**specification, "expected_from_warehouse": expected, "status": status, "claims": entries})
    return checks


def score_case(case, result, references, latency_seconds):
    calls, trace = result.get("tool_calls", []), result.get("trace", [])
    expected_names = {item["tool"] for item in case["expected_calls"]}
    actual_names = {item.get("name") for item in calls}
    tool_selection = expected_names <= actual_names and actual_names <= set(case["allowed_tools"])
    operations = []
    for expected, reference in zip(case["expected_calls"], references):
        matches = [call for call in calls if call.get("name") == expected["tool"] and
                   same_arguments(call.get("arguments"), expected["arguments"])]
        evidence_matches = []
        for call in matches:
            retrieved = call.get("result", {})
            # The independent read is the reference, not a guessed answer or model assertion.
            # Exclude checked-at freshness clock differences from data comparison only for KPIs/PRs.
            same_data = retrieved.get("data") == reference["result"].get("data")
            if expected["tool"] == "get_data_quality_status":
                same_data = stable_quality(retrieved.get("data")) == stable_quality(reference["result"].get("data"))
            evidence_matches.append(retrieved.get("status") == expected["status"] and
                                    retrieved.get("repository") == reference["result"].get("repository") and
                                    same_data and same_evidence(retrieved, reference["result"]))
        operations.append({**expected, "arguments_selected": bool(matches), "expected_result_retrieved": any(evidence_matches)})
    arguments_correct = all(item["arguments_selected"] for item in operations) and all(
        any(call.get("name") == expected["tool"] and same_arguments(call.get("arguments"), expected["arguments"])
            for expected in case["expected_calls"]) for call in calls)
    evidence_correct = all(item["expected_result_retrieved"] for item in operations)
    status = result.get("status")
    # Do not conflate a transport/limit error with an intentional application-level withholding.
    behavior_correct = status == ("ok" if case["expected_behavior"] == "answer" else "answer_withheld")
    completed = status == "ok" and bool(model_answer(result).strip())
    numerical_checks = check_numbers(case, result, references)
    errors = [call for call in calls if call.get("result", {}).get("status") == "error"]
    explicit_recoveries = result.get("recoveries", 0)
    repaired = bool(explicit_recoveries or errors or any(event.get("event") in
                   ("recovery", "answer_recovery", "malformed_call", "tool_error") for event in trace))
    operational_pass = tool_selection and arguments_correct and evidence_correct and behavior_correct and not any(
        check["status"] == "mismatch" for check in numerical_checks)
    return {
        "tool_selection_correct": tool_selection, "arguments_correct": arguments_correct,
        "expected_evidence_retrieved": evidence_correct, "operations": operations,
        "successful_evidence_calls": sum(call.get("result", {}).get("status") == "ok" for call in calls),
        "not_found_calls": sum(call.get("result", {}).get("status") == "not_found" for call in calls),
        "answer_completed": completed, "application_withheld": status == "answer_withheld",
        "expected_behavior_matched": behavior_correct,
        "semantic_refusal": "human_review", "numerical_checks": numerical_checks,
        "recovery_attempts": explicit_recoveries, "tool_call_count": len(calls),
        "tool_error_count": len(errors), "tool_rounds": result.get("tool_rounds", 0),
        "tool_error_codes": dict(Counter(call.get("result", {}).get("error", {}).get("code", "unspecified") for call in errors)),
        "proposed_tool_call_count": sum(event.get("call_count", 0) for event in trace if event.get("event") == "tool_calls_proposed"),
        "no_tool_response_count": sum(event.get("event") == "no_tool_calls" for event in trace),
        "model_request_count": sum(event.get("event") == "model_request" for event in trace),
        "latency_seconds": round(latency_seconds, 4), "operational_pass": operational_pass,
        "success_path": ("recovery_assisted" if explicit_recoveries else "tool_error_repaired" if repaired else "first_attempt") if operational_pass else "failed",
        "human_review_required": True,
        "human_review_criteria": case["human_review_criteria"],
        "accuracy_claim": "Operational checks only; generated prose is not fully verified.",
    }


def summarize(records):
    evaluated = [record for record in records if record["execution"] == "evaluated"]
    scores = [record["scores"] for record in evaluated]
    dimensions = ("tool_selection_correct", "arguments_correct", "expected_evidence_retrieved",
                  "answer_completed", "application_withheld", "expected_behavior_matched", "operational_pass")
    return {
        "selected": len(records), "evaluated": len(evaluated),
        "skipped_preconditions": sum(record["execution"] == "skipped_precondition" for record in records),
        "runner_errors": sum(record["execution"] == "runner_error" for record in records),
        "dimensions": {key: {"passed": sum(record["scores"][key] for record in evaluated
                                          if key not in ("arguments_correct", "expected_evidence_retrieved") or record["expected_calls"]),
                             "total": sum(key not in ("arguments_correct", "expected_evidence_retrieved") or bool(record["expected_calls"])
                                          for record in evaluated)} for key in dimensions},
        "success_paths": dict(Counter(score["success_path"] for score in scores)),
        "numerical_checks": dict(Counter(check["status"] for score in scores for check in score["numerical_checks"])),
        "tool_calls": sum(score["tool_call_count"] for score in scores),
        "recovery_attempts": sum(score["recovery_attempts"] for score in scores),
        "latency_seconds": {"total": round(sum(score["latency_seconds"] for score in scores), 4),
                            "median": statistics.median(score["latency_seconds"] for score in scores) if scores else None},
        "failures": [record["id"] for record in records if record["execution"] != "evaluated" or not record["scores"]["operational_pass"]],
        "human_review_required": [record["id"] for record in evaluated],
    }


def ollama_runtime(client):
    """Record the installed server/tag identity; do not download or reconfigure anything."""
    import requests
    metadata = {}
    try:
        version = client.session.get("http://127.0.0.1:11434/api/version", timeout=(5, 10), allow_redirects=False)
        version.raise_for_status()
        metadata["server_version"] = version.json().get("version")
        tags = client.session.get("http://127.0.0.1:11434/api/tags", timeout=(5, 10), allow_redirects=False)
        tags.raise_for_status()
        model = next((item for item in tags.json().get("models", []) if item.get("name") == DEFAULT_MODEL), None)
        metadata["installed_model"] = model
    except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
        metadata["metadata_error"] = type(error).__name__
    return metadata


def run_evaluation(cases, agent, reference_tools, clock=time.perf_counter, progress=None):
    """Dependency injection keeps tests offline. Every ask uses the existing Agent loop."""
    records = []
    for case in cases:
        record = {"id": case["id"], "category": case["category"], "question": case["question"],
                  "expected_calls": case["expected_calls"], "expected_behavior": case["expected_behavior"]}
        try:
            references = [{**expected, "result": reference_tools.call_tool(expected["tool"], expected["arguments"])}
                          for expected in case["expected_calls"]]
            record["reference_evidence"] = references
            if any(reference["result"].get("status") != reference["status"] for reference in references):
                record.update(execution="skipped_precondition", reason="Warehouse fixture does not match expected presence/absence; not counted as a model success.")
            else:
                started = clock()
                result = agent.ask(case["question"])
                latency = clock() - started
                record.update(execution="evaluated", result=result, scores=score_case(case, result, references, latency))
        except Exception as error:
            # Keep the whole run inspectable even if a case or its reference read fails.
            record.update(execution="runner_error", error_type=type(error).__name__, error=str(error))
        records.append(record)
        if progress:
            progress(record)
    return {"report_version": 1, "model": DEFAULT_MODEL, "cases": records, "summary": summarize(records)}


def print_case(record):
    if record["execution"] != "evaluated":
        print(f"{record['id']}: {record['execution']}", flush=True)
        return
    score = record["scores"]
    print(f"{record['id']}: {score['success_path']}; tools={score['tool_selection_correct']} "
          f"args={score['arguments_correct']} evidence={score['expected_evidence_retrieved']} "
          f"behavior={score['expected_behavior_matched']}; calls={score['tool_call_count']} "
          f"recoveries={score['recovery_attempts']}; {score['latency_seconds']:.2f}s", flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--cases", nargs="+", help="Case IDs; omitted means all cases. No automatic retries.")
    parser.add_argument("--list", action="store_true", help="List dataset cases without contacting Ollama")
    args = parser.parse_args()
    try:
        cases = load_cases(args.dataset)
        if args.cases:
            unknown = set(args.cases) - {case["id"] for case in cases}
            if unknown:
                parser.error("Unknown case IDs: " + ", ".join(sorted(unknown)))
            cases = [case for case in cases if case["id"] in args.cases]
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    if args.list:
        for case in cases:
            print(f"{case['id']} [{case['category']}]: {case['question']}")
        return 0
    started_at = utc_now()
    database_before = file_hash(DEFAULT_DATABASE)
    agent = LocalAgent()
    try:
        runtime = ollama_runtime(agent.client)
        report = run_evaluation(cases, agent, AgentTools(), progress=print_case)
    finally:
        agent.client.close()
    report.update(started_at_utc=started_at, finished_at_utc=utc_now(), ollama=runtime,
                  dataset={"path": str(args.dataset), "sha256": file_hash(args.dataset)},
                  configuration={"max_rounds": agent.max_rounds, "max_recoveries": agent.max_recoveries,
                                 "temperature": 0, "num_ctx": 8192, "num_predict": 1024,
                                 "repeats_per_case": 1, "latency_scope": "Agent.ask including model and tool time; excludes reference reads"},
                  source_hashes={name: file_hash(PROJECT_DIR / "src" / "engineering_intelligence" / name)
                                 for name in ("local_agent.py", "agent_tools.py", "evaluate_agent.py")},
                  warehouse={"path": str(DEFAULT_DATABASE), "sha256_before": database_before,
                             "sha256_after": file_hash(DEFAULT_DATABASE)},
                  limitations=["Operational pass is not full answer accuracy; every answer needs semantic review.",
                               "Numerical checking covers only recognized labelled claims, not all prose or inferred winners.",
                               "References use the existing validated tools; shared formula bugs need separate analytics tests.",
                               "One local attempt per selected case; no population-level reliability or security guarantee.",
                               "The warehouse is an observed, potentially incomplete cohort, not live GitHub history."])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / ("agent_evaluation_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {path}\nAll generated prose requires human review; no fully accurate answer score is claimed.")
    return 1 if report["summary"]["failures"] or report["warehouse"]["sha256_before"] != report["warehouse"]["sha256_after"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
