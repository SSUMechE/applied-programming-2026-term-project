"""Small algorithm-interface checks, without Genesis or physical integration.

These simple joint-space inputs exercise the editable algorithm and optional
timing hook. They do not certify any project map or physical transport.
"""
import math
import unittest

from student import joint_strategy
from transport_challenge.contracts import PlanningRequest
from transport_challenge.endpoint_planning import QueryBudgetOracle
from transport_challenge.joint_kinematics import BoundedKinematics, ToolPose, WorldGeometry
from transport_challenge.joint_context import JointBasePlacement, JointTaskContext
from transport_challenge.joint_planning import (
    JointLimits, parameterize_joint_path, validate_joint_path,
)


class _OpenJointSpace:
    """Supplied toy geometry: every bounded joint configuration is free."""
    def state_valid(self, q):
        return True

    def edge_valid(self, a, b):
        return True

    def fk(self, q):
        return ToolPose(tuple(q[:3]), (1.0, 0.0, 0.0, 0.0))

    def ik(self, position_m, quaternion_wxyz, seed_q):
        return tuple(position_m) + tuple(seed_q[3:])

    def surface_geometry(self, q):
        """Toy world surfaces support optional student clearance queries too."""
        def box(name, kind, entity, link, center, half):
            vertices=tuple(tuple(c+s*h for c,s,h in zip(center,signs,half))
                for signs in ((-1,-1,-1),(-1,-1,1),(-1,1,-1),(-1,1,1),
                              (1,-1,-1),(1,-1,1),(1,1,-1),(1,1,1)))
            faces=((0,1,3),(0,3,2),(4,6,7),(4,7,5),(0,4,5),(0,5,1),
                   (2,3,7),(2,7,6),(0,2,6),(0,6,4),(1,5,7),(1,7,3))
            return WorldGeometry(name,kind,entity,link,'box',tuple(center),(1.,0.,0.,0.),
                tuple(half),None,vertices,faces,vertices)
        return (box('toy_hand','arm','arm_0','hand',q[:3],(.01,.01,.01)),
                box('outside_workspace_box','obstacle','outside_workspace_box','box',
                    (3.5,3.5,3.5),(.5,.5,.5)))


class StudentStrategyChecks(unittest.TestCase):
    def setUp(self):
        self.limits = JointLimits(((-2.0, 2.0),) * 7, (1.0,) * 7, (2.0,) * 7)
        self.geometry = _OpenJointSpace()

    def context(self, start, goal):
        case = {
            "schema_version": "joint-map-v1", "case_id": "supplied_open_joint_space_check",
            "robot_model": "provided_seven_joint_toy_check", "tool_link": "hand",
            "arm_joint_names": ["joint" + str(i) for i in range(1, 8)], "base_z_m": 0.0,
            "installation_zones": [{"x_m": [-.2, .2], "y_m": [-.2, .2], "yaw_rad": [-math.pi, math.pi]}],
            "tool_workspace_m": [[-2., 2.]] * 3,
            "obstacles": [{"id": "outside_workspace_box", "lower_m": [3., 3., 3.], "upper_m": [4., 4., 4.]}],
            "start_tool_pose": {"position_m": list(start[:3]), "quaternion_wxyz": [1., 0., 0., 0.]},
            "goal_tool_pose": {"position_m": list(goal[:3]), "quaternion_wxyz": [1., 0., 0., 0.]},
            "motion_limits": {"velocity_rad_s": [1.] * 7, "acceleration_rad_s2": [2.] * 7,
                              "source": "Provided open toy check limits, not hardware specifications"},
            "planning": {"tracking_clearance_m": 0., "edge_max_joint_delta_rad": .005},
        }
        return JointTaskContext.from_case(case, limits=self.limits,
            base_placement=JointBasePlacement(x_m=0., y_m=0., yaw_rad=0.),
            active_arm_id='arm_0', motion_kind='carry', source_support_id='source_support',
            destination_support_id='goal_support')

    def call_planner(self, start, goal):
        request = PlanningRequest(start, goal, self.limits.position_rad, 2000, 71)
        kinematics = BoundedKinematics(self.geometry, request.bounds, ((-2.0, 2.0),) * 3,
                                      max_fk_calls=5000, max_ik_calls=1000)
        context = self.context(start, goal)
        returned = joint_strategy.plan_path(request, QueryBudgetOracle(self.geometry, request.max_queries),
                                            kinematics=kinematics, context=context)
        self.assertIsNotNone(returned, "The open joint-space check has a valid connection.")
        return validate_joint_path(returned, request, self.limits, self.geometry), context

    def test_path_preserves_seven_joint_endpoints(self):
        """The implemented search returns valid exact endpoints in seven dimensions."""
        path, _ = self.call_planner((-0.3,) * 7, (0.3,) * 7)
        self.assertEqual(path[0], (-0.3,) * 7)
        self.assertEqual(path[-1], (0.3,) * 7)

    def test_equal_endpoints_need_no_motion_edge(self):
        """A valid start equal to goal is one state, without repeated adjacent points."""
        start = (0.0,) * 7
        path, _ = self.call_planner(start, start)
        self.assertEqual(path, (start,))

    def test_optional_edge_times_obey_command_limits(self):
        """None is the supplied default. Explicit seconds must obey the same limits."""
        path = ((-0.3,) * 7, (0.3,) * 7)
        context = self.context(path[0], path[-1])
        times = joint_strategy.choose_edge_durations_s(path, self.limits, context=context)
        trajectory = parameterize_joint_path(path, self.limits, desired_edge_durations_s=times)
        self.assertGreater(trajectory.duration_s, 0.0)
        self.assertTrue(math.isfinite(trajectory.duration_s))


if __name__ == "__main__":
    unittest.main()
