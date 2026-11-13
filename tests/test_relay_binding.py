import copy
import json
from pathlib import Path
import unittest

from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design, design_space


ROOT = Path(__file__).resolve().parents[1]
MAPS = {'easy': 'easy_open_v1', 'medium': 'medium_staggered_v1', 'hard': 'hard_industrial_v1'}


def inputs(tier):
    case = ROOT / 'cases/difficulty_20261106' / MAPS[tier]
    world, scenario = (json.loads(path.read_bytes()) for path in (case/'world.json', case/'initialization.json'))
    space = design_space(world)
    # Structural test inputs, not a solved layout or a collision-free route.
    record = {'schema_version': 'three-arm-design-v1', 'joint_speed_scale': .5,
        'arms': [{'id': 'arm_'+str(i), 'mount_region': mount['id'],
            'base_placement': {'x_m': sum(mount['x_m'])/2, 'y_m': sum(mount['y_m'])/2,
                               'yaw_rad': sum(mount['yaw_rad'])/2}}
            for i, mount in enumerate(space['mount_regions'][:3])],
        'relays': [{'support_id': support['id'],
            'x_m': world['support_targets_by_id'][support['id']]['body_world']['position_m'][0],
            'y_m': world['support_targets_by_id'][support['id']]['body_world']['position_m'][1]}
            for support in world['relay_supports']]}
    return world, scenario, record


class RelayBindingTests(unittest.TestCase):
    def test_unchanged_relay_coordinates_keep_original_supported_poses(self):
        for tier in MAPS:
            with self.subTest(tier=tier):
                world, scenario, record = inputs(tier)
                bound = bind_three_arm_design(ThreeArmDesign.from_record(record, world), world, scenario)
                self.assertEqual(bound['scenario']['supported_body_poses'], scenario['supported_body_poses'])

    def test_shifted_relay_pose_reaches_both_recipes(self):
        world, scenario, record = inputs('medium')
        original_world, original_scenario = copy.deepcopy((world, scenario))
        relay = 'relay_1_support'
        record['relays'][0].update(x_m=1.05, y_m=1.05)
        bound = bind_three_arm_design(ThreeArmDesign.from_record(record, world), world, scenario)

        expected = copy.deepcopy(scenario['supported_body_poses'])
        expected[relay]['position_m'][:2] = [1.05, 1.05]
        self.assertEqual(bound['world']['support_targets_by_id'][relay]['body_world'], expected[relay])
        self.assertEqual(bound['scenario']['supported_body_poses'], expected)
        self.assertEqual(bound['world']['support_stations'], world['support_stations'])
        self.assertEqual(bound['scenario']['support_stations'], scenario['support_stations'])
        self.assertEqual((world, scenario), (original_world, original_scenario))


if __name__ == '__main__':
    unittest.main()
