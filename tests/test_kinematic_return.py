import unittest

from physics.saved_motion_planning import iter_kinematic_return_samples, kinematic_return_schedule
from transport_challenge.joint_planning import JointLimits, JointTrajectory


class KinematicReturnTests(unittest.TestCase):
    def test_cumulative_endpoint_rounds_past_duration(self):
        limits = JointLimits(((-1.0, 1.0),), (100.0,), (100.0,))
        approach = JointTrajectory(((-0.01,), (0.0,)), (0.25,), limits)
        carry = JointTrajectory(((0.0,), (0.01,), (0.02,), (0.03,)), (0.1, 0.2, 0.3), limits)
        self.assertGreater(carry.waypoint_times_s[-1], carry.duration_s)

        stages = [
            {'stage_id': 'arm_0__approach', 'trajectory': approach.to_record()},
            {'stage_id': 'arm_0__carry', 'trajectory': carry.to_record()},
        ]
        schedule, = kinematic_return_schedule(stages, {'arm_0': {'q_rad': [-0.01]}})
        samples = list(iter_kinematic_return_samples(schedule))

        self.assertEqual(samples[0]['source_time_s'], carry.duration_s)
        self.assertEqual(samples[0]['q_rad'], [0.03])
        self.assertEqual(samples[-1]['display_s'], schedule['display_duration_s'])
        self.assertEqual(samples[-1]['q_rad'], [-0.01])
        self.assertTrue(all(a['display_s'] < b['display_s'] for a, b in zip(samples, samples[1:])))
