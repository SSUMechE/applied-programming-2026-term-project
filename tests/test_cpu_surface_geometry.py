"""Actual CPU geometry, without Genesis initialization or physics integration."""
from dataclasses import FrozenInstanceError
import sys
import unittest
from unittest.mock import patch

import mujoco
import numpy as np

from tests.test_tier_route_contract import selected
from supplied_support.cpu_geometry import CpuGeometry
from transport_challenge.contracts import ContractError
from transport_challenge.joint_kinematics import BoundedKinematics, KinematicsBudgetExceeded


class CpuSurfaceGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _,bound=selected('easy')
        cls.engine=CpuGeometry(bound['world'],bound['scenario'])

    def configure(self, carrying=False):
        e=self.engine
        oracle=e.configure('arm_0',e.initial_q_by_arm,e.world['source_body_world'],
            holder_arm_id='arm_0' if carrying else None,
            hand_to_body={'translation_hand_m':[.1,0.,0.], 'quaternion_hand_body_wxyz':[1.,0.,0.,0.]} if carrying else None)
        facade=BoundedKinematics(oracle,e.bounds,e.case['tool_workspace_m'],max_fk_calls=4,max_ik_calls=0)
        return oracle,facade,list(e.initial_q_by_arm['arm_0'][:7])

    def test_surfaces_are_detached_nominal_geometry_with_no_step_or_collision(self):
        oracle,facade,q=self.configure()
        e=self.engine; initial=e.data.qpos.copy(); q[0]+=.05
        with patch('mujoco.mj_step',side_effect=AssertionError('geometry must not integrate')):
            surfaces=facade.geometry(q)
        np.testing.assert_array_equal(e.data.qpos,initial)
        self.assertEqual(oracle.collision_queries,0)
        self.assertEqual(facade.record()['backend_calls']['geometry'],1)
        self.assertEqual(facade.remaining_fk_calls,3)
        self.assertEqual(len(surfaces),e.model.ngeom)
        self.assertTrue(any(s.shape=='mesh' and len(s.vertices_m)>8 and s.triangles for s in surfaces))
        self.assertTrue(any(s.owner_kind=='tray' for s in surfaces))
        self.assertEqual(len([s for s in surfaces if s.owner_kind=='bead']),4)
        with self.assertRaises(FrozenInstanceError):surfaces[0].center_m=(0.,0.,0.)
        before=surfaces
        q[0]+=.1;facade.geometry(q)
        self.assertEqual(surfaces,before)
        self.assertFalse(any(n=='genesis' or n.startswith('genesis.') for n in sys.modules))

    def test_mesh_vertices_and_map_margins_have_distinct_meanings(self):
        oracle,facade,q=self.configure()
        e=self.engine
        surfaces=facade.geometry(q)
        mesh=next(s for s in surfaces if s.shape=='mesh')
        geom=e.model.geom(mesh.geometry_id).id; index=int(e.model.geom_dataid[geom])
        start,count=int(e.model.mesh_vertadr[index]),int(e.model.mesh_vertnum[index])
        expected=e.model.mesh_vert[start:start+count]@e.data.geom_xmat[geom].reshape(3,3).T+e.data.geom_xpos[geom]
        np.testing.assert_allclose(mesh.vertices_m,expected,atol=1e-9)
        surface=next(s for s in surfaces if s.geometry_id in e.obstacle_specs)
        spec=e.obstacle_specs[surface.geometry_id]
        original=next(o for o in e.case['obstacles'] if o['id']==spec['id'])
        np.testing.assert_allclose(np.min(surface.vertices_m,axis=0),original['lower_m'])
        np.testing.assert_allclose(np.max(surface.vertices_m,axis=0),original['upper_m'])
        self.assertTrue(np.all(np.asarray(surface.query_aabb_m[0])<=original['lower_m']))
        self.assertTrue(np.all(np.asarray(surface.query_aabb_m[1])>=original['upper_m']))

    def test_carrying_cup_follows_q_peers_and_snapshot_do_not_change(self):
        oracle,facade,q=self.configure(carrying=True)
        e=self.engine; original=e.data.qpos.copy(); pose=e.body_pose
        first={s.geometry_id:s for s in facade.geometry(q)}
        q[0]+=.15
        second={s.geometry_id:s for s in facade.geometry(q)}
        cup=[name for name,s in first.items() if s.owner_kind=='tray']
        self.assertTrue(any(first[name].center_m!=second[name].center_m for name in cup))
        peer=[name for name,s in first.items() if s.owner_kind=='arm' and s.entity_id!='arm_0']
        self.assertTrue(peer)
        self.assertTrue(all(first[name]==second[name] for name in peer))
        np.testing.assert_array_equal(e.data.qpos,original)
        self.assertEqual(e.body_pose,pose)

    def test_invalid_and_exhausted_calls_preserve_scene(self):
        _,facade,q=self.configure(); initial=self.engine.data.qpos.copy()
        invalid=list(q);invalid[0]=1e8
        with self.assertRaises(ContractError):facade.geometry(invalid)
        for _ in range(3):facade.geometry(q)
        with self.assertRaises(KinematicsBudgetExceeded):facade.geometry(q)
        np.testing.assert_array_equal(self.engine.data.qpos,initial)


if __name__=='__main__':unittest.main()
