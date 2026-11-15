"""CPU regressions for independent-arm scene admission and local-only results.

These fixtures test adapter ownership, not simulated physical success. Real
saved-arm schema/geometry validation is tested by the arm-plan implementation.
"""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from physics import review_arm_plan as review
from physics.saved_motion_planning import SavedMissionPlan
from transport_challenge.contracts import digest


class FixtureArmPlan(SavedMissionPlan):
    def __init__(self, path, arm='arm_2', record=None):
        self.path = Path(path).resolve()
        self.path.write_text('fixture independent arm', encoding='utf-8')
        self.file_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        initial = {f'arm_{i}': {'q_rad': [0.] * 7, 'finger_q_m': [.04, .04]} for i in range(3)}
        peers = {n: c for n, c in initial.items() if n != arm}
        body = {'frame': 'world', 'position_m': [2., 1., .6], 'quaternion_wxyz': [1., 0., 0., 0.]}
        stages = [dict(stage_id=arm+'__'+kind, kind=kind, active_arm_id=arm,
                       support_id='relay_2_support', destination_support_id='goal_support' if kind=='carry' else None,
                       expected_peer_commands_by_arm=copy.deepcopy(peers), nominal_body_world=body,
                       trajectory={'waypoints': [[0.] * 7, [0.] * 7]}) for kind in ('approach', 'carry')]
        self.value = record or dict(schema_version='saved-arm-mission-v1', scope='independent_arm',
            selected_arm_id=arm, initial_commands_by_arm=initial, stages=stages,
            task_contract={'difficulty': 'hard', 'required_arm_ids': ['arm_0', 'arm_1', 'arm_2'],
                           'support_route': ['source_support', 'relay_1_support', 'relay_2_support', 'goal_support']},
            physical_grasp_verified=False, physical_transport_verified=False,
            full_mission_passed=False, student_release_approved=False)
        self.sha256 = digest(self.value)
        self.stage_signatures = tuple((s['stage_id'], s['kind'], s['active_arm_id']) for s in self.value['stages'])
        self.post_place_policy = 'kinematic_return'

    @classmethod
    def from_file(cls, path):
        raise AssertionError('The fixture loader must not run outside the exact adapter')

    @property
    def required_arm_ids(self): return (self.value['selected_arm_id'],)
    @property
    def task_contract(self): return copy.deepcopy(self.value['task_contract'])
    @property
    def support_route(self): return tuple(self.task_contract['support_route'])
    def record(self): return copy.deepcopy(self.value)
    def stage(self, index): return copy.deepcopy(self.value['stages'][index])
    def verify_sources(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.file_sha256:
            raise ValueError('Fixture source changed')
        return True
    def materialize(self, destination): return FixtureArmPlan(destination, record=copy.deepcopy(self.value))


class IndependentArmPracticeTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.plan = FixtureArmPlan(self.directory / 'arm.json')
        module = ModuleType('physics.arm_mission_plan')
        module.ArmMissionPlan = FixtureArmPlan
        previous = sys.modules.get(module.__name__)
        sys.modules[module.__name__] = module
        def restore_module():
            if previous is None: sys.modules.pop(module.__name__, None)
            else: sys.modules[module.__name__] = previous
        self.addCleanup(restore_module)

    def test_parser_excludes_full_plan_and_online_arguments(self):
        args = ['--saved-arm', 'arm.json', '--difficulty', 'hard', '--task', 'task',
                '--design', 'design.json', '--arm', 'arm_2', '--contents', 'beads', '--output', 'run']
        self.assertEqual(review.parser().parse_args(args).saved_arm, Path('arm.json'))
        for extra in ('--saved-tier', '--rule', '--server-url', '--token-file'):
            with self.subTest(option=extra), patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
                review.parser().parse_args(args+[extra, 'forbidden'])

    def test_wrong_arm_or_full_success_claim_rejected_before_binding(self):
        with self.assertRaises(ValueError): review._identity(self.plan, 'arm_1')
        self.plan.value['full_mission_passed'] = True
        with self.assertRaises(ValueError): review._identity(self.plan, 'arm_2')

    def test_changed_peer_or_missing_robot_is_rejected(self):
        self.plan.value['stages'][1]['expected_peer_commands_by_arm']['arm_0']['q_rad'][0] = .1
        with self.assertRaises(ValueError): review._identity(self.plan, 'arm_2')
        self.plan.value['stages'][1]['expected_peer_commands_by_arm'] = copy.deepcopy(
            self.plan.value['stages'][0]['expected_peer_commands_by_arm'])
        for stage in self.plan.value['stages']: del stage['expected_peer_commands_by_arm']['arm_0']
        with self.assertRaises(ValueError): review._identity(self.plan, 'arm_2')

    def test_exact_loader_and_callback_keep_independent_schema(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        original_class, original_callback = tier.TierMissionPlan, frozen.execute_device_tier_mission
        original_loader = FixtureArmPlan.__dict__['from_file']
        callback = Mock(return_value='LOCAL')
        with review.local_scene_binding(self.plan, 'arm_2', callback) as receipt:
            loaded = tier.TierMissionPlan.from_file(self.plan.path)
            self.assertIs(loaded, self.plan)
            self.assertEqual(loaded.record()['scope'], 'independent_arm')
            result = frozen.execute_device_tier_mission(
                {'student_driver': SimpleNamespace(saved_plan=loaded)}, output=self.directory, wall_deadline=1.)
            self.assertEqual(result, 'LOCAL')
        self.assertIs(tier.TierMissionPlan, original_class)
        self.assertIs(frozen.execute_device_tier_mission, original_callback)
        self.assertIs(FixtureArmPlan.__dict__['from_file'], original_loader)
        self.assertEqual((receipt['loader_calls'], receipt['callback_calls'], receipt['restored']), (1, 1, True))
        with self.assertRaises(ValueError):
            original_class(self.plan.record(), path=self.plan.path)

    def test_inherited_loader_is_removed_after_adapter_exit(self):
        from physics import tier_mission_plan as tier
        original = FixtureArmPlan.__dict__['from_file']
        del FixtureArmPlan.from_file
        try:
            with review.local_scene_binding(self.plan, 'arm_2', Mock()):
                self.assertIs(tier.TierMissionPlan.from_file(self.plan.path), self.plan)
            self.assertNotIn('from_file', FixtureArmPlan.__dict__)
        finally:
            FixtureArmPlan.from_file = original

    def test_foreign_path_loader_failure_restores_every_name(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        cls, callback = tier.TierMissionPlan, frozen.execute_device_tier_mission
        other = self.directory / 'foreign.json'; other.write_text('{}')
        with self.assertRaises(ValueError):
            with review.local_scene_binding(self.plan, 'arm_2', Mock()) as receipt:
                tier.TierMissionPlan.from_file(other)
        self.assertIs(tier.TierMissionPlan, cls)
        self.assertIs(frozen.execute_device_tier_mission, callback)
        self.assertTrue(receipt['restored'])

    def test_callback_exception_restores_every_name(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        cls, original = tier.TierMissionPlan, frozen.execute_device_tier_mission
        with self.assertRaisesRegex(RuntimeError, 'owned failure'):
            with review.local_scene_binding(self.plan, 'arm_2', Mock(side_effect=RuntimeError('owned failure'))) as receipt:
                tier.TierMissionPlan.from_file(self.plan.path)
                frozen.execute_device_tier_mission({'student_driver': SimpleNamespace(saved_plan=self.plan)},
                                                   output=self.directory, wall_deadline=1.)
        self.assertIs(tier.TierMissionPlan, cls)
        self.assertIs(frozen.execute_device_tier_mission, original)
        self.assertTrue(receipt['restored'])

    def test_nested_or_duplicate_admission_is_rejected(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        with review.local_scene_binding(self.plan, 'arm_2', Mock()):
            with self.assertRaises(RuntimeError):
                with review.local_scene_binding(self.plan, 'arm_2', Mock()): pass
            tier.TierMissionPlan.from_file(self.plan.path)
            with self.assertRaises(ValueError): tier.TierMissionPlan.from_file(self.plan.path)
            context = {'student_driver': SimpleNamespace(saved_plan=self.plan)}
            frozen.execute_device_tier_mission(context, output=self.directory, wall_deadline=1.)
            with self.assertRaises(RuntimeError):
                frozen.execute_device_tier_mission(context, output=self.directory, wall_deadline=1.)

    def test_wrong_callback_plan_and_unadmitted_callback_are_rejected(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        callback = Mock()
        with review.local_scene_binding(self.plan, 'arm_2', callback):
            with self.assertRaises(RuntimeError):
                frozen.execute_device_tier_mission({'student_driver': SimpleNamespace(saved_plan=self.plan)},
                                                   output=self.directory, wall_deadline=1.)
            tier.TierMissionPlan.from_file(self.plan.path)
            with self.assertRaises(RuntimeError):
                frozen.execute_device_tier_mission({'student_driver': SimpleNamespace(saved_plan=object())},
                                                   output=self.directory, wall_deadline=1.)
        callback.assert_not_called()

    def test_source_mutation_between_admission_and_callback_is_rejected(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        with review.local_scene_binding(self.plan, 'arm_2', Mock()):
            tier.TierMissionPlan.from_file(self.plan.path)
            self.plan.path.write_text('changed')
            with self.assertRaises(ValueError):
                frozen.execute_device_tier_mission({'student_driver': SimpleNamespace(saved_plan=self.plan)},
                                                   output=self.directory, wall_deadline=1.)

    def test_real_frozen_driver_type_admission_only_inside_local_context(self):
        from physics.student_episode_driver import StudentEpisodeDriver
        case = json.loads((review.ROOT / 'cases/difficulty_20261106/hard_industrial_v1/world.json').read_bytes())['case_record']
        args = dict(strategy=None, case=case, speed_scale=1., mode='tier_mission', grasp_policy='automatic', saved_plan=self.plan)
        with self.assertRaises(ValueError): StudentEpisodeDriver(**args)
        with review.local_scene_binding(self.plan, 'arm_2', Mock()):
            driver = StudentEpisodeDriver(**args)
            self.assertIs(driver.saved_plan, self.plan)
            self.assertEqual(driver.saved_plan.required_arm_ids, ('arm_2',))
            self.assertEqual(len(driver.saved_plan.support_route), 4)
        with self.assertRaises(ValueError): StudentEpisodeDriver(**args)

    def test_manifest_source_changes_are_rejected(self):
        manifest = {}
        for name in review.PRACTICE_FILES:
            p = self.directory / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(name)
            manifest[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        (self.directory / 'course_release.json').write_text(json.dumps({'managed_files': manifest}))
        with patch.object(review, 'ROOT', self.directory):
            self.assertEqual(review.practice_source_snapshot(), manifest)
            (self.directory / 'physics/review_arm_plan.py').write_text('changed')
            with self.assertRaises(ValueError): review.practice_source_snapshot()

    def test_parent_rejects_foreign_local_file_native_hash_and_producer(self):
        output = self.directory / 'parent-run'; output.mkdir()
        bound = self.plan.materialize(output / 'BOUND_ARM_PLAN.json')
        worker = dict(bound_arm_plan=dict(file=str(bound.path), sha256=bound.file_sha256,
            bundle_sha256=bound.sha256, schema_version='saved-arm-mission-v1', scope='independent_arm'),
            native=dict(saved_bundle_sha256=bound.sha256, saved_plan_file_sha256=bound.file_sha256),
            input_arm_plan=dict(file=str(self.plan.path), sha256=self.plan.file_sha256,
                bundle_sha256=self.plan.sha256, schema_version='saved-arm-mission-v1',
                scope='independent_arm', selected_arm_id='arm_2'))
        with patch.object(FixtureArmPlan, 'from_file', return_value=bound):
            self.assertIs(review.verify_bound_worker_plan(worker, output, self.plan), bound)
            bad = copy.deepcopy(worker); bad['bound_arm_plan']['file'] = str(self.plan.path)
            with self.assertRaises(ValueError): review.verify_bound_worker_plan(bad, output, self.plan)
            bad = copy.deepcopy(worker); bad['native']['saved_bundle_sha256'] = 'wrong'
            with self.assertRaises(ValueError): review.verify_bound_worker_plan(bad, output, self.plan)
            bound.value['component_origin'] = dict(schema_version='explicit-arm-plan-materialization-v1',
                producer_file_sha256='wrong', producer_bundle_sha256=self.plan.sha256)
            with self.assertRaises(ValueError): review.verify_bound_worker_plan(worker, output, self.plan)

    def test_manual_upload_rejects_even_a_renamed_practice_receipt(self):
        from leaderboard import auto_record
        target = self.directory / 'upload'; target.mkdir()
        (target/'REVIEW_SUPERVISOR.json').write_text(json.dumps(dict(scope='arm_practice',
            contents='beads', difficulty='hard', full_mission_passed=False, ranking_eligible=False)))
        rule = dict(schema_version=auto_record.RULE_SCHEMA, difficulty='hard', rule_digest='rule')
        with patch.object(auto_record, 'load_rule', return_value=rule), \
                patch.object(auto_record, 'require_current_rule'), \
                patch.object(auto_record, 'validate_snapshot'), \
                patch.object(auto_record, 'source_snapshot') as source:
            with self.assertRaisesRegex(Exception, 'matching full bead review'):
                auto_record.auto_record_from_review('rule.json', target, project_root=review.ROOT,
                    student_snapshot={}, queue_dir=self.directory/'queue')
        source.assert_not_called()
        self.assertFalse((self.directory/'queue').exists())

    def test_worker_uses_local_sentinel_and_never_full_or_online_completion(self):
        from physics import tier_mission_plan as tier, device_tier_mission as frozen
        from physics import run_student_three_arm as entry
        output = self.directory / 'run'
        args = SimpleNamespace(output=output, arm='arm_2', difficulty='hard', task=self.directory,
                               design=self.directory / 'design.json', contents='beads', wall_limit_s=1800.)
        full = Mock(side_effect=AssertionError('Full engine cannot execute'))
        native = {'practice_passed': True, 'scope': 'arm_practice', 'full_mission_passed': False, 'ranking_eligible': False}
        def scene():
            self.assertEqual(sys.argv[sys.argv.index('--wall-limit-s')+1], '0')
            index = sys.argv.index('--saved-tier')
            loaded = tier.TierMissionPlan.from_file(sys.argv[index+1])
            self.assertEqual(loaded.path, output / 'BOUND_ARM_PLAN.json')
            self.assertEqual(loaded.record()['scope'], 'independent_arm')
            try:
                frozen.execute_device_tier_mission({'student_driver': SimpleNamespace(saved_plan=loaded)},
                                                   output=output/'scene_setup', wall_deadline=1.)
            except review.ArmPracticeComplete:
                target = output/'scene_setup'; target.mkdir()
                (target/'result.json').write_text(json.dumps({'failure': {'type': 'ArmPracticeComplete'},
                    'cleanup_errors': [], 'source_bindings_unchanged': True, 'full_mission_passed': False}))
        with patch.object(frozen, 'execute_device_tier_mission', full), patch.object(review, 'load_plan', return_value=self.plan), \
                patch.object(entry, 'main', side_effect=scene), \
                patch('physics.device_arm_practice.execute', return_value=native) as execute, \
                patch('physics.arm_practice_validation.validate_arm_practice_native', return_value=True) as validate:
            self.assertEqual(review.run_worker(args), 0)
            self.assertIs(frozen.execute_device_tier_mission, full)
        full.assert_not_called(); execute.assert_called_once(); validate.assert_called_once()
        self.assertIsNone(execute.call_args.kwargs['deadline'])
        result = json.loads((output/'PRACTICE_WORKER.json').read_bytes())
        self.assertTrue(result['standalone_arm_plan'])
        self.assertFalse(result['full_mission_passed']); self.assertFalse(result['ranking_eligible'])
        self.assertTrue(result['local_scene_binding']['restored'])
        self.assertEqual(result['blocked_online_calls'], {'plan_student_motion': 0, 'plan_joint_motion': 0, 'ik': 0})
        self.assertFalse((output/'REVIEW_SUPERVISOR.json').exists())
        self.assertFalse((output/'LEADERBOARD_STATUS.json').exists())


if __name__ == '__main__':
    unittest.main()
