"""Regression cases derived from recorded failures, with unrelated controlled repositories."""

import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from src.engineering_intelligence.answer_grounding import freshness_text, metadata_lines, grounding_problem, model_tool_result
from src.engineering_intelligence.local_agent import LocalAgent, MAX_TOOL_RESULT_CHARS


def result(repository="example/project", pages=3, complete=False, timestamp="2026-10-10T00:00:00Z"):
    return {
        "tool": "get_repository_kpis", "repository": repository, "status": "ok",
        "data": {"metrics": {"total_observed_prs": 5}},
        "evidence": {
            "repository_url": f"https://github.com/{repository}", "github_live_status_verified": False,
            "earliest_pr_extraction_at_utc": timestamp, "latest_pr_extraction_at_utc": timestamp,
            "data_quality": {
                "latest_ingestion_run": {"run_id": "latest", "extracted_at_utc": timestamp,
                                         "pages_fetched": pages, "pagination_complete": complete,
                                         "stop_reason": "page_limit"},
                "all_observation_runs_pagination_complete": complete,
                "freshness": {"latest_extraction_age_hours": 245.0, "latest_extraction_is_stale": True,
                              "checked_at_utc": "2026-10-20T05:00:00Z", "stale_after_hours": 24,
                              "stale_pr_observation_count": 5},
                "warnings": ["incomplete_pagination", "stale_pr_observations"],
            },
        },
    }


class AnswerGroundingTests(unittest.TestCase):
    def test_freshness_conversion_is_deterministic_and_unknown_stays_unknown(self):
        self.assertEqual(freshness_text({"latest_extraction_age_hours": 245}), "245.00 hours (10.21 days)")
        self.assertEqual(freshness_text({"latest_extraction_age_hours": 0}), "0.00 hours (0.00 days)")
        for value in (None, -1, float("nan"), float("inf"), True, "245"):
            with self.subTest(value=value):
                self.assertIn("unknown", freshness_text({"latest_extraction_age_hours": value}))

    def test_latest_and_older_contributing_run_counts_are_not_conflated(self):
        evidence = result()["evidence"]
        evidence["data_quality"]["observation_runs"] = [
            {"run_id": "old", "extracted_at_utc": "2026-10-01T00:00:00Z", "pages_fetched": 1,
             "pagination_complete": None, "stop_reason": "legacy"}]
        text = "\n".join(metadata_lines(evidence))
        self.assertIn("Latest-run pages_fetched=3", text)
        self.assertIn("Contributing run old", text)
        self.assertIn("pages_fetched=1", text)
        self.assertIn("245.00 hours (10.21 days)", text)
        evidence["data_quality"]["latest_ingestion_run"]["pages_fetched"] = None
        self.assertIn("Latest-run pages_fetched=unknown", "\n".join(metadata_lines(evidence)))

    def test_unsupported_page_counts_and_source_url_inference_are_rejected(self):
        for claim in ("Only the first page of PRs was fetched.", "We fetched one page.",
                      "The ingestion stopped at page 1.", "One page was retrieved.",
                      "The source URL has page=1, so pagination is incomplete."):
            with self.subTest(claim=claim):
                self.assertIsNotNone(grounding_problem(claim, [result()]))
        self.assertIsNone(grounding_problem("Three pages were fetched. Pagination was not complete.", [result()]))
        self.assertIsNone(grounding_problem("Five pages were fetched.", [result(pages=5)]))
        self.assertIsNotNone(grounding_problem("One page was fetched.", [result(pages=None)]))
        self.assertIsNotNone(grounding_problem("Pagination was complete.", [result()]))
        self.assertIsNotNone(grounding_problem("Incomplete pagination.", [result(complete=None)]))
        self.assertIsNone(grounding_problem("Pagination was complete.", [result(complete=True)]))

    def test_counts_and_exact_timestamps_are_bound_to_repository(self):
        first = result()
        second = result("other/repository", 5, timestamp="2026-10-11T00:00:00Z")
        self.assertIsNone(grounding_problem("example/project: Three pages were fetched.\nother/repository: Five pages were fetched.", [first, second]))
        self.assertIsNotNone(grounding_problem("other/repository: Three pages were fetched.", [first, second]))
        self.assertIsNotNone(grounding_problem("Three pages were fetched.", [first, second]))
        self.assertIsNone(grounding_problem("other/repository: extracted at 2026-10-11T00:00:00Z.", [first, second]))
        self.assertIsNotNone(grounding_problem("other/repository: extracted at 2026-10-10T00:00:00Z.", [first, second]))
        self.assertIsNotNone(grounding_problem("unknown/repository: Three pages were fetched.", [first]))

    def test_all_numerical_freshness_claims_are_left_to_python(self):
        for claim in ("Extraction was 245 hours ago (10 years and 2 months).",
                      "The data is stale, approximately 4.2 days old.",
                      "Freshness age is 245 hours (10.21 days)."):
            with self.subTest(claim=claim):
                self.assertIsNotNone(grounding_problem(claim, [result()]))
        self.assertIsNone(grounding_problem("Median merge time: 4.2 hours.\nThe observations are stale.", [result()]))
        self.assertIsNone(grounding_problem("PR #42 was observed open at extraction with age 9 days.", [result()]))
        self.assertIsNotNone(grounding_problem("Extraction age: 245 hours.", [result()]))

    def test_correct_policy_threshold_is_allowed_but_not_confused_with_age(self):
        self.assertIsNone(grounding_problem("The freshness policy threshold is 24 hours.", [result()]))
        self.assertIsNotNone(grounding_problem("The freshness policy threshold is 12 hours.", [result()]))
        self.assertIsNotNone(grounding_problem("Extraction age is 245 hours, beyond the 24 hours freshness policy.", [result()]))

    def test_valid_stale_threshold_bound_does_not_become_an_exact_age_claim(self):
        self.assertIsNone(grounding_problem("The extraction occurred more than 24 hours ago.", [result()]))
        self.assertIsNone(grounding_problem("The observations are stale (older than 24 hours).", [result()]))
        self.assertIsNotNone(grounding_problem("The extraction occurred more than 12 hours ago.", [result()]))
        self.assertIsNotNone(grounding_problem("The data was last extracted 24 hours ago.", [result()]))
        fresh = result()
        fresh["evidence"]["data_quality"]["freshness"]["latest_extraction_is_stale"] = False
        self.assertIsNotNone(grounding_problem("The extraction occurred more than 24 hours ago.", [fresh]))
        self.assertIsNotNone(grounding_problem("The extraction occurred more than 24 hours ago, per freshness policy.", [fresh]))
        self.assertIsNotNone(grounding_problem("The extraction occurred 24 hours ago, per freshness policy.", [result()]))

    def test_negated_and_false_flag_pagination_statements_are_not_positive_claims(self):
        for claim in ("The run did not complete pagination.", "The data lacks complete pagination.",
                      "There is a lack of complete pagination.", "Both have a pagination complete status of false.",
                      "Pagination is incomplete.", "Pagination was not completed."):
            with self.subTest(claim=claim):
                self.assertIsNone(grounding_problem(claim, [result()]))
        self.assertIsNotNone(grounding_problem("The run did complete pagination.", [result()]))

    def test_slash_separated_state_names_do_not_change_repository_scope(self):
        self.assertIsNone(grounding_problem("PR states (open/merged) may have changed.\nPagination was not complete.", [result()]))

    def test_python_owned_metadata_is_omitted_only_from_model_view(self):
        original = result()
        original["data"]["metrics"].update(average_time_to_merge_hours=12.0, average_open_age_days=3.0)
        original["evidence"]["data_quality"]["latest_ingestion_run"]["source_start_url"] = "https://api.github.com/repos/example/project/pulls?page=1"
        unchanged = deepcopy(original)
        view = model_tool_result(original)
        freshness = view["evidence"]["data_quality"]["freshness"]
        self.assertNotIn("latest_extraction_age_hours", freshness)
        self.assertNotIn("checked_at_utc", freshness)
        self.assertTrue(freshness["latest_extraction_is_stale"])
        self.assertNotIn("source_start_url", view["evidence"]["data_quality"]["latest_ingestion_run"])
        self.assertEqual(view["evidence"]["data_quality"]["latest_ingestion_run"]["pages_fetched"], 3)
        self.assertEqual(view["data"], original["data"])
        self.assertEqual(original, unchanged)
        self.assertIn("intentionally omitted", view["model_view_metadata"])

    def test_page_index_is_not_inferred_from_page_count(self):
        self.assertIsNotNone(grounding_problem("The extraction stopped at page 3.", [result(pages=3)]))

    def test_staleness_is_not_open_age_or_subsequent_activity(self):
        for claim in ("Stale observations indicate that PRs have since been closed or merged.",
                      "Staleness means these are old PRs.",
                      "Since extraction these PRs have merged."):
            with self.subTest(claim=claim):
                self.assertIsNotNone(grounding_problem(claim, [result()]))
        self.assertIsNone(grounding_problem("Staleness does not indicate PR open age or later GitHub events.", [result()]))

    def test_stale_observations_do_not_prove_pr_states_are_no_longer_current(self):
        claim = "The stale PR observation count is 5, indicating that 5 PRs were observed in a state that is no longer current."
        self.assertIsNotNone(grounding_problem(claim, [result()]))

    def test_placeholder_or_incomplete_iso_timestamps_cannot_pass_as_calendar_dates(self):
        for timestamp in ("2026-10-10T00:00:XXZ", "2026-10-10T00:00:00", "2026-10-10T12:00:00Z"):
            with self.subTest(timestamp=timestamp):
                self.assertIsNotNone(grounding_problem("The extraction timestamp is " + timestamp, [result()]))
        self.assertIsNone(grounding_problem("The extraction timestamp is 2026-10-10T00:00:00Z.", [result()]))

    def test_productivity_and_causal_claims_are_rejected_but_refusals_are_allowed(self):
        for claim in ("Productivity rankings require per-developer PR counts.",
                      "Author attribution would enable developer productivity rankings.",
                      "PR counts can measure developer performance.",
                      "Reviews are slow because the team is understaffed."):
            with self.subTest(claim=claim):
                self.assertIsNotNone(grounding_problem(claim, [result()]))
        self.assertIsNone(grounding_problem("PR counts cannot establish productivity even with author attribution.\nReview bottleneck causes are not measured.", [result()]))

    def test_rejected_metadata_uses_existing_bounded_rewrite_and_keeps_correct_appendix(self):
        client = Mock()
        client.chat.side_effect = [
            {"content": "", "tool_calls": [{"function": {"name": "get_repository_kpis", "arguments": {"repository": "example/project"}}}]},
            {"content": "Only the first page of PRs was fetched.", "tool_calls": []},
            {"content": "There are five observed PRs. This cohort is incomplete; see the evidence below.", "tool_calls": []},
        ]
        tools = Mock()
        tools.call_tool.return_value = result()
        response = LocalAgent(client, tools).ask("Show example/project KPIs.")
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["recoveries"], 1)
        tools.call_tool.assert_called_once()
        self.assertEqual(client.chat.call_args_list[-1].args[1], [])
        self.assertIn("answer_rejected", [event["event"] for event in response["trace"]])
        self.assertNotIn("Only the first page", response["answer"])
        self.assertIn("pages_fetched=3", response["answer"])
        self.assertIn("245.00 hours (10.21 days)", response["answer"])
        self.assertIn("even with author attribution", response["answer"])
        rejected = next(event for event in response["trace"] if event["event"] == "answer_rejected")
        self.assertIn("Only the first page", rejected["untrusted_draft"])
        self.assertFalse(any("Only the first page" in message.get("content", "")
                             for message in client.chat.call_args_list[-1].args[0]))

    def test_model_view_cannot_bypass_existing_tool_result_size_limit(self):
        client = Mock()
        client.chat.side_effect = [
            {"content": "", "tool_calls": [{"function": {"name": "get_repository_kpis", "arguments": {"repository": "example/project"}}}]},
            {"content": "No evidence.", "tool_calls": []},
        ]
        tools = Mock()
        tools.call_tool.return_value = result()
        with patch("src.engineering_intelligence.local_agent.model_tool_result", return_value={"data": "x" * MAX_TOOL_RESULT_CHARS}):
            response = LocalAgent(client, tools, max_recoveries=0).ask("Show example/project KPIs.")
        self.assertEqual(response["status"], "answer_withheld")
        self.assertEqual(response["tool_calls"][0]["result"]["error"]["code"], "tool_result_too_large")
        self.assertIn("tool_error", [event["event"] for event in response["trace"]])

    def test_repeated_unsupported_claims_are_withheld_without_fallback_facts(self):
        client = Mock()
        client.chat.side_effect = [
            {"content": "", "tool_calls": [{"function": {"name": "get_repository_kpis", "arguments": {"repository": "example/project"}}}]},
            *[{"content": "Freshness is 245 hours, about 10 years.", "tool_calls": []}] * 3,
        ]
        tools = Mock()
        tools.call_tool.return_value = result()
        response = LocalAgent(client, tools).ask("Show example/project KPIs.")
        self.assertEqual(response["status"], "answer_withheld")
        self.assertEqual(response["recoveries"], 2)
        self.assertEqual(client.chat.call_count, 4)
        tools.call_tool.assert_called_once()
        self.assertNotIn("about 10 years", response["answer"])
        self.assertIn("10.21 days", response["answer"])

    def test_quality_threshold_is_rendered_by_python_not_supplied_as_a_model_age(self):
        evidence = result()
        evidence["tool"] = "get_data_quality_status"
        evidence["data"] = deepcopy(evidence["evidence"]["data_quality"])
        original = deepcopy(evidence)
        client = Mock()
        client.chat.side_effect = [
            {"content": "", "tool_calls": [{"function": {"name": "get_data_quality_status", "arguments": {"repository": "example/project"}}}]},
            {"content": "The stored observations are stale. Pagination is incomplete; see the recorded metadata below.", "tool_calls": []},
        ]
        tools = Mock()
        tools.call_tool.return_value = evidence
        response = LocalAgent(client, tools).ask("How fresh is our example/project source?")
        tool_message = next(message for message in client.chat.call_args.args[0] if message["role"] == "tool")
        view = json.loads(tool_message["content"])["untrusted_tool_result"]
        for freshness in (view["data"]["freshness"], view["evidence"]["data_quality"]["freshness"]):
            self.assertNotIn("stale_after_hours", freshness)
            self.assertNotIn("latest_extraction_age_hours", freshness)
            self.assertTrue(freshness["latest_extraction_is_stale"])
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["tool_calls"][0]["result"], original)
        self.assertEqual(evidence, original)
        self.assertIn("stale threshold=24 hours", response["answer"])
        self.assertIn("245.00 hours (10.21 days)", response["answer"])

    def test_recorded_quality_failure_still_withholds_unsupported_or_ambiguous_drafts(self):
        # Exact freshness paragraphs from the three rejected drafts in the preserved
        # agent_evaluation_20261020T211103817733Z.json; no ignored files are needed.
        drafts = [
            "- **Freshness**: The latest extraction is **stale** (older than 24 hours), with a staleness policy of 24 hours. This means the data was extracted more than 24 hours ago, and no recent updates have been captured. The stale PR observation count is 90, indicating that 90 PRs were observed in a state that is no longer current.",
            "- **Freshness**: The latest extraction is stale (24 hours after extraction), and 90 PR observations are marked as stale. This reflects the time since extraction, not PR open age or subsequent GitHub events.",
            "- **Freshness**: The latest extraction is stale (24 hours after extraction), and 90 PR observations are marked as stale. This reflects the time since extraction, not PR open age or subsequent GitHub events.",
        ]
        evidence = result()
        evidence["tool"] = "get_data_quality_status"
        evidence["evidence"]["data_quality"]["freshness"].update(
            latest_extraction_age_hours=246.66510822805554, stale_pr_observation_count=90)
        evidence["data"] = deepcopy(evidence["evidence"]["data_quality"])
        client = Mock()
        client.chat.side_effect = [
            {"content": "", "tool_calls": [{"function": {"name": "get_data_quality_status", "arguments": {"repository": "example/project"}}}]},
            *[{"content": draft, "tool_calls": []} for draft in drafts],
        ]
        tools = Mock()
        tools.call_tool.return_value = evidence
        response = LocalAgent(client, tools).ask("How fresh is our example/project source?")
        rejected = [event for event in response["trace"] if event["event"] == "answer_rejected"]
        self.assertEqual([event["untrusted_draft"] for event in rejected], drafts)
        self.assertIn("subsequent GitHub events", rejected[0]["reason"])
        self.assertTrue(all("numerical extraction-age" in event["reason"] for event in rejected[1:]))
        self.assertEqual(response["status"], "answer_withheld")
        self.assertEqual(response["recoveries"], 2)
        self.assertEqual(client.chat.call_count, 4)
        tools.call_tool.assert_called_once()
        self.assertIn("246.67 hours (10.28 days)", response["answer"])

    def test_review_negative_controls_keep_genuinely_false_metadata_rejected(self):
        for claim in ("The extraction occurred 24 hours ago.",
                      "The freshness policy threshold is 12 hours.",
                      "Extraction was 245 hours ago (10 years and 2 months).",
                      "Only one page was fetched.", "Pagination was complete.",
                      "Stale observations indicate that PR states are no longer current."):
            with self.subTest(claim=claim):
                self.assertIsNotNone(grounding_problem(claim, [result()]))


if __name__ == "__main__":
    unittest.main()
