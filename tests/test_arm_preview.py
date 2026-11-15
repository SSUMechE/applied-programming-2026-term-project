"""CPU-only admission/control tests for standalone-arm preview exporters.

Native records here are explicit unit fixtures. Mocking the native validator
only checks delegation and rejection order, never qualifies physical motion.
"""
import base64
import copy
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import export_student_view as nominal
import export_physics_view as recorded


class PreviewControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = self.root/'saved_arm_mission.json'; self.saved.write_text('unit fixture')

    def test_nominal_arm_uses_embedded_identity_without_sibling(self):
        plan = SimpleNamespace(record=lambda: {'task_manifest_sha256': 'same'},
            current_source_pins=lambda: {}, input_identity=lambda: {'world': {}, 'scenario': {}, 'design': {}},
            task_contract={'difficulty': 'hard'})
        reads = []
        def read(path):
            reads.append(Path(path).name)
            return {'world.json': {'case_record': {'case_id': 'hard_industrial_v1'}},
                'initialization.json': {}, 'difficulty_target.json': {'difficulty': 'hard', 'case_id': 'hard_industrial_v1'},
                'manifest.json': {'files': {}}}[Path(path).name]
        with patch('physics.arm_mission_plan.ArmMissionPlan.from_file', return_value=plan), \
                patch('physics.tier_mission_plan.TierMissionPlan.from_file', side_effect=AssertionError('Full loader forbidden')), \
                patch.object(nominal, 'read', side_effect=read), patch.object(nominal, 'sha', return_value='same'):
            with self.assertRaisesRegex(ValueError, 'Saved source root is ambiguous'):
                nominal.build_preview(self.saved, independent_arm=True)
        self.assertNotIn('input_identity.json', reads)
        self.assertFalse((self.root/'input_identity.json').exists())

    def test_nominal_rejects_malformed_embedded_identity(self):
        plan = SimpleNamespace(record=lambda: {}, current_source_pins=lambda: {},
                               input_identity=lambda: {'world': {}}, task_contract={'difficulty': 'hard'})
        with patch('physics.arm_mission_plan.ArmMissionPlan.from_file', return_value=plan):
            with self.assertRaisesRegex(ValueError, 'Malformed saved input identity'):
                nominal.build_preview(self.saved, independent_arm=True)

    def test_nominal_cli_blocks_genesis_integration_and_ik_then_restores(self):
        import mujoco
        from supplied_support.cpu_geometry import CpuOracle
        old_step, old_ik = mujoco.mj_step, CpuOracle.ik
        before = list(sys.meta_path)
        def build(path, **kwargs):
            self.assertTrue(kwargs['independent_arm'])
            self.assertEqual(path, self.saved)
            with self.assertRaisesRegex(RuntimeError, 'must not import Genesis'):
                importlib.import_module('genesis')
            with self.assertRaisesRegex(RuntimeError, 'integration is forbidden'):
                mujoco.mj_step(None, None)
            with self.assertRaisesRegex(RuntimeError, 'integration is forbidden'):
                CpuOracle.ik(None)
            return {'stages': [], 'scope': {'geometry_only': True, 'physical_steps': 0}}
        output = self.root/'output'/'nominal.json'
        with patch.object(nominal, 'ROOT', self.root), patch.object(nominal, 'build_preview', side_effect=build), \
                patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(nominal.main(['--saved-arm', str(self.saved), '--output', str(output)]), 0)
        self.assertIs(mujoco.mj_step, old_step); self.assertIs(CpuOracle.ik, old_ik)
        self.assertEqual(sys.meta_path, before)
        self.assertEqual(json.loads(output.read_bytes())['scope']['physical_steps'], 0)

    def test_cli_guards_restore_after_export_error_and_write_no_result(self):
        import mujoco
        from supplied_support.cpu_geometry import CpuOracle
        old_step, old_ik = mujoco.mj_step, CpuOracle.ik
        before = list(sys.meta_path)
        output = self.root/'output'/'failed.json'
        with patch.object(nominal, 'ROOT', self.root), patch.object(nominal, 'build_preview', side_effect=ValueError('bad component')), \
                patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            nominal.main(['--saved-arm', str(self.saved), '--output', str(output)])
        self.assertIs(mujoco.mj_step, old_step); self.assertIs(CpuOracle.ik, old_ik)
        self.assertEqual(sys.meta_path, before); self.assertFalse(output.exists())

    def test_recorded_cli_selects_arm_and_blocks_genesis(self):
        before = list(sys.meta_path)
        def build(path, review_dir, **kwargs):
            self.assertTrue(kwargs['independent_arm']); self.assertEqual(kwargs['fps'], 15)
            with self.assertRaisesRegex(RuntimeError, 'must not import Genesis'):
                importlib.import_module('genesis')
            return {'total_display_frames': 0, 'scope': {'physical_steps': 0}}
        with patch.object(recorded, 'ROOT', self.root), patch.object(recorded, 'build_preview', side_effect=build), \
                patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(recorded.main(['--saved-arm', str(self.saved), '--review-dir', str(self.root),
                '--output', str(self.root/'output'/'recorded.json')]), 0)
        self.assertEqual(sys.meta_path, before)

    def test_both_saved_types_and_existing_output_are_rejected(self):
        for exporter in (nominal, recorded):
            out = self.root/'output'/exporter.__name__; out.parent.mkdir(exist_ok=True); out.write_text('keep')
            extra = [] if exporter is nominal else ['--review-dir', str(self.root)]
            with patch.object(exporter, 'ROOT', self.root), patch('sys.stderr', new=io.StringIO()):
                with self.assertRaises(SystemExit):
                    exporter.main(['--saved-arm', str(self.saved), '--saved-tier', 'other.json', '--output', str(out), *extra])
                with self.assertRaises(SystemExit):
                    exporter.main(['--saved-arm', str(self.saved), '--output', str(out), *extra])
            self.assertEqual(out.read_text(), 'keep')

    def test_sampled_indices_are_actual_integer_post_rows(self):
        for count in (1, 200, 799, 2537):
            for fps in (15, 30, 60):
                rows = recorded.sampled_indices(count, fps)
                self.assertEqual((rows[0], rows[-1]), (0, count-1))
                self.assertEqual(rows, sorted(set(rows)))
                self.assertTrue(all(type(i) is int and 0 <= i < count for i in rows))


class IndependentRecordedAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.saved = self.root/'source.json'; self.saved.write_text('original fixture')
        self.review = self.root/'practice'; self.review.mkdir()
        self.bound_path = self.review/'BOUND_ARM_PLAN.json'; self.bound_path.write_text('bound fixture')
        self.native_path = self.review/'arm_execution'/'ARM_PRACTICE_NATIVE_RESULT.json'
        self.native_path.parent.mkdir()
        identity = {'world': {'fixture': True}, 'scenario': {}, 'design': {}}
        from transport_challenge.contracts import digest
        self.original_record = {'stages': [{'fixture': 'two saved stages'}]}
        self.original_record['bundle_sha256'] = digest(self.original_record)
        self.saved.write_text(json.dumps(self.original_record))
        self.bound_record = copy.deepcopy(self.original_record)
        self.bound_record['component_origin'] = dict(
            schema_version='explicit-arm-plan-materialization-v1',
            producer_file_sha256=nominal.sha(self.saved),
            producer_bytes_base64=base64.b64encode(self.saved.read_bytes()).decode('ascii'),
            producer_bundle_sha256=self.original_record['bundle_sha256'])
        self.bound_record['bundle_sha256'] = digest({k:v for k,v in self.bound_record.items() if k != 'bundle_sha256'})
        self.bound_path.write_text(json.dumps(self.bound_record))
        self.original = SimpleNamespace(path=self.saved, file_sha256=nominal.sha(self.saved), sha256=self.original_record['bundle_sha256'],
            selected_arm_id='arm_2', record=lambda: copy.deepcopy(self.original_record), input_identity=lambda: copy.deepcopy(identity))
        self.bound = SimpleNamespace(path=self.bound_path, file_sha256=nominal.sha(self.bound_path), sha256=self.bound_record['bundle_sha256'],
            selected_arm_id='arm_2', record=lambda: copy.deepcopy(self.bound_record), input_identity=lambda: copy.deepcopy(identity))
        self.native = {'scope': 'arm_practice', 'contents': 'empty', 'practice_passed': True,
            'full_mission_passed': False, 'ranking_eligible': False,
            'saved_plan_file_sha256': self.bound.file_sha256, 'saved_bundle_sha256': self.bound.sha256,
            'initialization': {'fixed_scene': {'bead_identities': []}}, 'fixture_only_not_physical_evidence': True}
        self.result = dict(schema_version='student-arm-practice-v1', scope='arm_practice', status='ARM_PRACTICE_PASS',
            standalone_arm_plan=True, practice_passed=True, source_bindings_unchanged=True,
            full_mission_passed=False, ranking_eligible=False, arm_id='arm_2', difficulty='hard',
            input_arm_plan={'sha256': self.original.file_sha256, 'bundle_sha256': self.original.sha256},
            bound_arm_plan={'file': str(self.bound_path), 'sha256': self.bound.file_sha256, 'bundle_sha256': self.bound.sha256},
            worker={'native': copy.deepcopy(self.native)})
        def load(path):
            if Path(path) == self.saved: return self.original
            if Path(path) == self.bound_path: return self.bound
            raise AssertionError('Foreign file requested')
        self.loader = patch('physics.arm_mission_plan.ArmMissionPlan.from_file', side_effect=load)
        self.validator = patch('physics.arm_practice_validation.validate_arm_practice_native', return_value=True)
        self.loader.start(); self.addCleanup(self.loader.stop)
        self.validate = self.validator.start(); self.addCleanup(self.validator.stop)

    def run_admission(self):
        self.native_path.write_text(json.dumps(self.native))
        (self.review/'PRACTICE_RESULT.json').write_text(json.dumps(self.result))
        return recorded._independent_practice(self.saved, self.review, {'difficulty': 'hard'})

    def test_matching_local_fixture_delegates_to_native_validator(self):
        proof = self.run_admission()
        self.assertEqual(proof[2], self.native)
        self.assertEqual(proof[3], [])
        self.validate.assert_called_once_with(self.native, plan_record=self.bound.record())
        self.assertEqual(proof[1]['scope'], 'arm_practice')
        self.assertFalse(proof[1]['full_mission_passed'])

    def test_wrong_component_or_arm_is_rejected_before_native_validation(self):
        for field, wrong in (('sha256', 'foreign-file'), ('bundle_sha256', 'foreign-bundle')):
            with self.subTest(field=field):
                before = copy.deepcopy(self.result)
                self.result['input_arm_plan'][field] = wrong
                with self.assertRaisesRegex(ValueError, 'component differ'): self.run_admission()
                self.result = before
        self.result['arm_id'] = 'arm_0'
        with self.assertRaisesRegex(ValueError, 'component differ'): self.run_admission()
        self.validate.assert_not_called()

    def test_altered_bound_motion_or_layout_is_rejected(self):
        self.bound_record['stages'] = [{'foreign': 'path'}]
        with self.assertRaisesRegex(ValueError, 'binding or saved motion'): self.run_admission()
        self.bound_record['stages'] = copy.deepcopy(self.original_record['stages'])
        self.bound.input_identity = lambda: {'world': {'wrong': True}, 'scenario': {}, 'design': {}}
        with self.assertRaisesRegex(ValueError, 'binding or saved motion'): self.run_admission()
        self.validate.assert_not_called()

    def test_foreign_declared_bound_path_is_rejected(self):
        self.result['bound_arm_plan']['file'] = str(self.root/'foreign.json')
        with self.assertRaises(ValueError): self.run_admission()
        self.validate.assert_not_called()

    def test_foreign_bound_producer_is_rejected_even_with_same_motion(self):
        self.bound_record['component_origin']['producer_file_sha256'] = 'foreign'
        with self.assertRaises(ValueError): self.run_admission()
        self.validate.assert_not_called()

    def test_full_or_rank_claim_failed_or_changed_source_is_rejected(self):
        for field, wrong in (('full_mission_passed', True), ('ranking_eligible', True),
                             ('practice_passed', False), ('source_bindings_unchanged', False),
                             ('scope', 'full_mission')):
            with self.subTest(field=field):
                before = copy.deepcopy(self.result); self.result[field] = wrong
                with self.assertRaises(ValueError): self.run_admission()
                self.result = before
        self.validate.assert_not_called()

    def test_native_record_or_local_file_digest_change_is_rejected(self):
        self.native['changed'] = True
        with self.assertRaisesRegex(ValueError, 'native record differs'): self.run_admission()
        self.native.pop('changed'); self.bound_path.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'binding or saved motion'): self.run_admission()
        self.validate.assert_not_called()

    def test_native_validator_failure_is_not_reclassified_as_preview_pass(self):
        self.validate.side_effect = ValueError('Actual native proof rejected')
        with self.assertRaisesRegex(ValueError, 'Actual native proof rejected'): self.run_admission()


if __name__ == '__main__':
    unittest.main()
