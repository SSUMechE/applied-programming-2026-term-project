"""Optional, metered FK/IK primitives for student-generated sampling targets.

No engine or path planner is imported here. IK solves only the supplied target
pose from the supplied joint seed. A returned configuration is not a collision
or path-validity claim. The unchanged joint oracle must check candidate edges.
"""
from dataclasses import dataclass
from collections.abc import Mapping
import math

from .contracts import ContractError, PlanningBudgetExceeded, number, point


class KinematicsBudgetExceeded(PlanningBudgetExceeded):
    """A separate public FK/IK call cap is exhausted."""


def _quaternion(value):
    value = point(value, 4)
    norm = math.hypot(*value)
    if abs(norm-1.) > 1e-5:
        raise ContractError("Use a unit world quaternion in w,x,y,z order")
    return value


@dataclass(frozen=True)
class ToolPose:
    position_m: tuple
    quaternion_wxyz: tuple

    def __post_init__(self):
        object.__setattr__(self, "position_m", point(self.position_m, 3))
        object.__setattr__(self, "quaternion_wxyz", _quaternion(self.quaternion_wxyz))


@dataclass(frozen=True)
class WorldGeometry:
    """Detached collision surface in world metres, never a path or validity result.

    Mesh/box vertices and triangle indices describe their nominal surfaces.
    A sphere uses its exact radius and centre. Its six surface points are only
    samples, not a polygonal substitute. A plane is infinite with local +Z normal.
    query_aabb_m separately describes the oracle's padded map box where present.
    """
    geometry_id: str
    owner_kind: str
    entity_id: str
    link_id: str
    shape: str
    center_m: tuple
    quaternion_wxyz: tuple
    half_extents_m: tuple | None = None
    radius_m: float | None = None
    vertices_m: tuple = ()
    triangles: tuple = ()
    surface_points_m: tuple = ()
    query_aabb_m: tuple | None = None

    def __post_init__(self):
        if self.shape not in ('mesh', 'box', 'sphere', 'plane'):
            raise ContractError('Unsupported published collision surface type')
        for name in ('geometry_id', 'owner_kind', 'entity_id', 'link_id'):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ContractError('Collision geometry needs named ownership')
        object.__setattr__(self, 'center_m', point(self.center_m, 3))
        object.__setattr__(self, 'quaternion_wxyz', _quaternion(self.quaternion_wxyz))
        for name in ('vertices_m', 'surface_points_m'):
            object.__setattr__(self, name, tuple(point(p, 3) for p in getattr(self, name)))
        faces = tuple(tuple(face) for face in self.triangles)
        if any(len(face) != 3 or any(type(i) is not int or not 0 <= i < len(self.vertices_m) for i in face) for face in faces):
            raise ContractError('Collision triangles must index the published vertices')
        object.__setattr__(self, 'triangles', faces)
        if self.half_extents_m is not None:
            half = point(self.half_extents_m, 3)
            if any(v <= 0 for v in half):
                raise ContractError('Box half extents must be positive')
            object.__setattr__(self, 'half_extents_m', half)
        if self.radius_m is not None:
            radius = number(self.radius_m, 'sphere radius', minimum=0.)
            if radius == 0:
                raise ContractError('Sphere radius must be positive')
            object.__setattr__(self, 'radius_m', radius)
        if self.query_aabb_m is not None:
            if len(self.query_aabb_m) != 2:
                raise ContractError('Query AABB needs lower and upper XYZ')
            object.__setattr__(self, 'query_aabb_m', tuple(point(p, 3) for p in self.query_aabb_m))


class BoundedKinematics:
    """Per-planning-call facade with immutable native/workspace bounds.

    adapter.fk(q) returns ToolPose or its two-field mapping. adapter.ik(xyz,
    quaternion,seed_q) returns q or None. The adapter must perform one bounded
    IK solve without hidden multistart, intermediate targets or route creation.
    Internal FK verification is included in one IK call, not recharged against
    public FK calls. It is counted separately in backend telemetry.
    """
    def __init__(self, adapter, joint_bounds_rad, workspace_bounds_m, *, max_fk_calls,
                 max_ik_calls, wall_check=None, position_tolerance_m=.003,
                 orientation_tolerance_rad=.02):
        if not callable(getattr(adapter, "fk", None)) or not callable(getattr(adapter, "ik", None)):
            raise ContractError("Kinematics adapter must provide callable fk and ik")
        joint_bounds = tuple(point(pair, 2) for pair in joint_bounds_rad)
        workspace = tuple(point(pair, 2) for pair in workspace_bounds_m)
        if not joint_bounds or len(workspace) != 3 or any(lo >= hi for lo, hi in joint_bounds+workspace):
            raise ContractError("Use ordered native joint bounds and three workspace intervals")
        if any(not math.isfinite(hi-lo) for lo, hi in joint_bounds+workspace):
            raise ContractError("Kinematics bounds must have finite spans")
        if any(type(cap) is not int or cap < 0 for cap in (max_fk_calls, max_ik_calls)):
            raise ContractError("FK and IK caps must be nonnegative integers")
        if wall_check is not None and not callable(wall_check):
            raise ContractError("wall_check must be callable or None")
        self._adapter, self._bounds, self._workspace = adapter, joint_bounds, workspace
        self._caps = {"fk": max_fk_calls, "ik": max_ik_calls}
        self._calls = {"fk": 0, "ik": 0}
        self._backend = {"fk": 0, "ik": 0, "ik_verification_fk": 0, "geometry": 0}
        self._exhausted, self._converged, self._misses = 0, 0, 0
        self._wall_check = wall_check
        self._position_tolerance = number(position_tolerance_m, "position tolerance", minimum=0)
        self._orientation_tolerance = number(orientation_tolerance_rad, "orientation tolerance", minimum=0)
        if self._position_tolerance == 0 or self._orientation_tolerance == 0:
            raise ContractError("IK residual tolerances must be positive")

    @property
    def joint_bounds_rad(self): return self._bounds

    @property
    def workspace_bounds_m(self): return self._workspace

    @property
    def remaining_fk_calls(self): return self._caps["fk"]-self._calls["fk"]

    @property
    def remaining_ik_calls(self): return self._caps["ik"]-self._calls["ik"]

    def _clock(self):
        if self._wall_check is not None:
            self._wall_check()

    def _charge(self, kind):
        if self._calls[kind] >= self._caps[kind]:
            self._exhausted += 1
            raise KinematicsBudgetExceeded(f"Public {kind.upper()} call cap exhausted")
        self._calls[kind] += 1
        self._clock()

    def _q(self, value):
        value = point(value, len(self._bounds))
        if any(not lo <= q <= hi for q, (lo, hi) in zip(value, self._bounds)):
            raise ContractError("Kinematics configuration leaves native joint limits")
        return value

    def _forward(self, q, *, verification=False):
        self._clock()
        self._backend["ik_verification_fk" if verification else "fk"] += 1
        result = self._adapter.fk(q)
        self._clock()
        if isinstance(result, Mapping):
            result = ToolPose(result["position_m"], result["quaternion_wxyz"])
        if not isinstance(result, ToolPose):
            raise ContractError("FK adapter must return ToolPose or its named mapping")
        return result

    def fk(self, q):
        """Return a finite world tool pose. This does not test collision validity."""
        self._charge("fk")
        return self._forward(self._q(q))

    def geometry(self, q):
        """Return immutable world collision surfaces for one nominal configuration.

        One call shares the FK budget. Peers retain the stage's current poses.
        The cup follows the provided grasp transform during a carrying stage.
        No collision check or physical integration is performed by this query.
        """
        self._charge('fk')
        query = getattr(self._adapter, 'surface_geometry', None)
        if not callable(query):
            raise ContractError('This kinematics adapter does not supply collision surfaces')
        self._backend['geometry'] += 1
        surfaces = tuple(query(self._q(q)))
        self._clock()
        if not surfaces or any(not isinstance(s, WorldGeometry) for s in surfaces):
            raise ContractError('Geometry query must return named immutable WorldGeometry surfaces')
        return surfaces

    def ik(self, position_m, quaternion_wxyz, seed_q):
        """Return all native q or None after fixed residual checks.

        A failed or malformed public call still consumes one IK call. Only the
        student/reference generates the target and seed. Collision checks are
        deliberately separate and remain mandatory before accepting an edge.
        """
        self._charge("ik")
        target = ToolPose(position_m, quaternion_wxyz)
        seed_q = self._q(seed_q)
        if any(not lo <= x <= hi for x, (lo, hi) in zip(target.position_m, self._workspace)):
            raise ContractError("IK target lies outside the supplied workspace bounds")
        self._backend["ik"] += 1
        result = self._adapter.ik(target.position_m, target.quaternion_wxyz, seed_q)
        self._clock()
        if result is None:
            self._misses += 1
            return None
        q = self._q(result)
        actual = self._forward(q, verification=True)
        position_error = math.dist(actual.position_m, target.position_m)
        dot = math.fsum(a*b for a, b in zip(actual.quaternion_wxyz, target.quaternion_wxyz))
        cosine = abs(dot)/(math.hypot(*actual.quaternion_wxyz)*math.hypot(*target.quaternion_wxyz))
        orientation_error = 2*math.acos(min(1., cosine))
        if position_error > self._position_tolerance or orientation_error > self._orientation_tolerance:
            self._misses += 1
            return None
        self._converged += 1
        return q

    def record(self):
        return {"schema_version": "bounded-joint-kinematics-v1",
                "joint_bounds_rad": [list(pair) for pair in self._bounds],
                "workspace_bounds_m": [list(pair) for pair in self._workspace],
                "max_fk_calls": self._caps["fk"], "max_ik_calls": self._caps["ik"],
                "fk_calls": self._calls["fk"], "ik_calls": self._calls["ik"],
                "backend_calls": dict(self._backend), "ik_converged": self._converged,
                "ik_no_solution_or_residual_failure": self._misses,
                "exhausted_call_attempts": self._exhausted,
                "position_tolerance_m": self._position_tolerance,
                "orientation_tolerance_rad": self._orientation_tolerance,
                "ik_result_is_collision_or_path_certificate": False}
