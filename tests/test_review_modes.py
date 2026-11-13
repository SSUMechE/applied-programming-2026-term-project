"""Provided regression checks for practice/scoring and upload boundaries."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace, ModuleType
import json
import sys
import unittest
from unittest.mock import Mock, patch

from physics import review_tier_transport as review


class ReviewModeTests(unittest.TestCase):
    def test_empty_acceptance_is_separate_from_full_bead_success(self):
        plan = SimpleNamespace(sha256="plan", file_sha256="file", record=lambda: {})
        result = {
            "status": "TIER_MISSION_DEVICE_EMPTY_SAMPLED_PASS",
            "empty_mission_passed": True, "full_mission_passed": False,
            "source_bindings_unchanged": True, "saved_bundle_sha256": "plan",
            "saved_plan_file_sha256": "file", "cleanup_errors": [], "failure": None,
            "device_tier_execution": {"contents": "empty"},
        }
        module = ModuleType("physics.device_tier_mission")
        module.validate_device_tier_completion = Mock(return_value=True)
        calls = {"plan_student_motion": 0, "plan_joint_motion": 0, "ik": 0}
        with patch.dict(sys.modules, {module.__name__: module}):
            accepted, _ = review.assess_native(result, plan, calls, worker_exit_code=0,
                                               timed_out=False, sources_current=True, contents="empty")
            self.assertTrue(accepted)
            module.validate_device_tier_completion.assert_called_once_with(
                result["device_tier_execution"], plan_record={}, contents="empty")
            bead_accepted, _ = review.assess_native(result, plan, calls, worker_exit_code=0,
                                                   timed_out=False, sources_current=True, contents="beads")
            self.assertFalse(bead_accepted)

    def test_empty_practice_never_calls_upload_hook(self):
        args = SimpleNamespace(contents="empty")
        self.assertEqual(review.finalize_online_record(args, {"schema_version": "ap-leaderboard-rule-v3"}, {}),
                         {"status": "local_practice", "reason": "empty_cup_is_not_scored"})

    def test_network_failure_does_not_rewrite_native_outcome(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            original = b'{"review_accepted": true, "full_mission_passed": true}\n'
            (folder / "REVIEW_SUPERVISOR.json").write_bytes(original)
            args = SimpleNamespace(contents="beads", rule=Path("rule.json"), output=folder,
                                   server_url=None, token_file=None)
            module = ModuleType("leaderboard.auto_record")
            module.auto_record_from_review = Mock(side_effect=ConnectionError("private-token-must-not-appear"))
            with patch.dict(sys.modules, {module.__name__: module}):
                outcome = review.finalize_online_record(args, {"schema_version": "ap-leaderboard-rule-v3"}, {})
            self.assertEqual(outcome["status"], "recording_error")
            self.assertEqual((folder / "REVIEW_SUPERVISOR.json").read_bytes(), original)
            self.assertNotIn("private-token", json.dumps(outcome))
            self.assertEqual(json.loads((folder / "LEADERBOARD_STATUS.json").read_bytes()), outcome)

    def test_classify_empty_success_and_bead_success(self):
        self.assertEqual(review.classify_outcome({"review_accepted": True, "contents": "empty"}),
                         "ACCEPTED_EMPTY_PRACTICE")
        self.assertEqual(review.classify_outcome({"review_accepted": True, "contents": "beads"}),
                         "ACCEPTED_FULL_MISSION")


if __name__ == "__main__":
    unittest.main()
