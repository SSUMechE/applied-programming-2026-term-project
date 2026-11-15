"""Edit plan_path for arm_1. Runnable, deliberately limited example.
It tries one joint at a time and stops if blocked. It is not a solved map route.
Inputs, return values and test commands: README 4.2 and 5.
"""
import math
import random

from transport_challenge.contracts import PlanningBudgetExceeded
from transport_challenge.geometry import distance, steer
from transport_challenge.joint_planning import minimum_edge_duration_s


def plan_path(request, oracle, *, kinematics=None, context=None):
    # BEGIN REQUIRED STUDENT EDIT: replace this example with your planner.
    # Each state is (joint1, ..., joint7), in radians.
    try:
        if not oracle.state_valid(request.start):
            return None

        path = [request.start]
        for joint in range(7):
            candidate = list(path[-1])
            candidate[joint] = request.goal[joint]
            candidate = tuple(candidate)

            if candidate == path[-1]:
                continue
            if not oracle.state_valid(candidate):
                return None
            if not oracle.edge_valid(path[-1], candidate):
                return None
            path.append(candidate)

        return tuple(path)
    except PlanningBudgetExceeded:
        return None
    # END REQUIRED STUDENT EDIT


def choose_edge_durations_s(path, limits, *, context):
    """None uses joint_speed_scale from your design JSON. See README 4.3."""
    # BEGIN OPTIONAL STUDENT EDIT: return one duration in seconds per edge.
    return None
    # END OPTIONAL STUDENT EDIT
