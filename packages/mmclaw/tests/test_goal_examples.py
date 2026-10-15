import json
import unittest

from mmclaw.agentic_loop import GoalLoop
from mmclaw.evidence import EvidenceStore
from mmclaw.goal_examples import EXAMPLES, VERSION, render_examples
from mmclaw.tool_control import ToolLoopController
from mmclaw.tool_schemas import get_native_tool_schemas


class GoalExampleTests(unittest.TestCase):
    def loop(self, profile="finance_chat_readonly", **settings):
        loop = GoalLoop({"tool_profile": profile, "agentic_loop": {"enabled": True, **settings}}, "A new unrelated request")
        loop.bind_tools(get_native_tool_schemas({"tool_profile": profile}, route=False))
        return loop

    def test_finance_profile_gets_four_domains_and_a_non_finance_counterexample(self):
        loop = self.loop()
        for phase, limit in (("plan", 5000), ("execute", 5000), ("review", 4500)):
            text = loop.examples(phase)
            for key in ("DEMO_MARKET", "DEMO_COMPANY", "DEMO_FACTOR", "DEMO_GRAPH", "DEMO_NO_TOOLS"):
                self.assertIn(key, text)
            self.assertNotIn("DEMO_FILE_COMPARE", text)
            self.assertLess(len(text), limit)
            self.assertIn("NOT CURRENT TASK DATA", text)

    def test_general_profile_does_not_inherit_finance_examples(self):
        loop = self.loop("default")
        self.assertIn("DEMO_FILE_COMPARE", loop.examples("plan"))
        self.assertNotIn("DEMO_FACTOR", loop.examples("plan"))
        self.assertNotIn("DEMO_MARKET", loop.examples("execute"))

    def test_examples_respect_the_actual_capability_boundary(self):
        text = render_examples("execute", finance=True, available_tools={"finance_time_context"})
        self.assertIn("DEMO_NO_TOOLS", text)
        self.assertNotIn("finance_document_retrieve", text)
        self.assertNotIn("file_read", text)

    def test_example_plans_and_calls_match_registered_protocols(self):
        for example in EXAMPLES:
            loop = self.loop("finance_chat_readonly" if example["domain"] == "finance" else "default")
            loop.accept_plan(example["plan"])
            self.assertEqual(loop.rejected_tools, [], example["id"])
            for turn in example["execute"]:
                for call in turn["assistant"]["tools"]:
                    self.assertIsNone(loop.argument_error(call["name"], call["args"]), example["id"])

    def test_demonstrations_never_become_live_evidence_or_completed_work(self):
        loop = self.loop()
        evidence = EvidenceStore("f" * 32, loop.user_text)
        self.assertIn("DEMO_MARKET", loop.plan_messages([])[0]["content"])
        loop.instruction(evidence, ToolLoopController())
        messages = loop.review_messages(evidence, "实际任务的候选回答")
        current = json.loads(messages[-1]["content"])
        self.assertEqual(current["evidence"], [])
        self.assertEqual(current["tool_receipts"], [])
        self.assertEqual(current["delivered_answers"], [])
        self.assertEqual(current["original_request"], loop.user_text)
        self.assertEqual(loop.requirements, [])
        self.assertEqual(loop.selected_tools, [])

    def test_switch_is_reversible_without_disabling_goal_loop(self):
        loop = self.loop(few_shot_enabled=False)
        self.assertTrue(loop.enabled)
        self.assertEqual(loop.examples("plan"), "")
        self.assertNotIn("DEMO_", loop.instruction(EvidenceStore("f"*32, "task"), ToolLoopController()))
        self.assertIsNone(loop.summary()["few_shot_version"])
        self.assertEqual(self.loop().summary()["few_shot_version"], VERSION)

    def test_copied_example_receipts_cannot_prove_live_completion(self):
        loop = self.loop()
        example = next(item for item in EXAMPLES if item["id"] == "DEMO_GRAPH")
        loop.accept_plan(example["plan"])
        loop.accept_review(example["review"][0]["assistant"],
                           EvidenceStore("f" * 32, loop.user_text), "A proposed answer")
        self.assertNotEqual(loop.review_status, "complete")
        self.assertTrue(all(item["status"] == "missing" for item in loop.requirements))
        self.assertTrue(all(not item["tool_call_ids"] for item in loop.requirements))

    def test_background_jobs_do_not_get_interactive_examples(self):
        loop = GoalLoop({"tool_profile": "finance_chat_readonly"}, "日报", background=True)
        self.assertEqual(loop.examples("plan"), "")


if __name__ == "__main__":
    unittest.main()
