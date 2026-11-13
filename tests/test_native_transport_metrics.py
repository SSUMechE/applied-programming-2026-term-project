import copy
import math
import unittest
import numpy as np
from transport_challenge.vessel import build_container_geometry
from physics.native_transport_metrics import whole_bead_escaped,CarriedDistance,world_center
from physics.device_tier_mission import partial_progress
from transport_challenge.contracts import ContractError

class NativeMetricsTest(unittest.TestCase):
    def setUp(self):
        self.g=build_container_geometry(dict(outer_size_m=[.12,.10,.06],wall_m=.006,open_top=True,partitions=0,handle_width_m=.04,mass_limit_kg=.5,geometry_model='centered_overhead_handle_v3'))
        self.p=dict(position_m=[0,0,0],quaternion_wxyz=[1,0,0,0])
    def test_open_top_is_not_a_lid(self):
        self.assertFalse(whole_bead_escaped([0,0,.5],.012,self.g))
        self.assertFalse(whole_bead_escaped([.071,0,.5],.012,self.g))
        self.assertTrue(whole_bead_escaped([.074,0,.5],.012,self.g))
        self.assertTrue(whole_bead_escaped([0,0,-.114],.012,self.g))
    def test_tilt_changes_center_even_with_fixed_root(self):
        metric=CarriedDistance(self.g)
        p=copy.deepcopy(self.p);p['quaternion_wxyz']=[math.sqrt(.5),math.sqrt(.5),0,0]
        h=np.zeros((1,14));h[0,3:7]=p['quaternion_wxyz']
        metric.append('arm_0',7,self.p,h)
        result=metric.record()
        self.assertAlmostEqual(result['path_length_m'],math.sqrt(2)*.067)
        self.assertEqual(result['reference_point_body_m'],[0,0,-.067])
        self.assertEqual(result['counted_intervals'],1)
        self.assertEqual(result['distance_mm'],95)
    def test_round_once_and_no_fake_return_length(self):
        metric=CarriedDistance(self.g)
        h=np.zeros((1,14));h[0,0]=.0003;h[0,3]=1
        metric.append('arm_0',10,self.p,h)
        metric.append('arm_1',100,self.p,h)
        self.assertEqual(metric.record()['distance_mm'],1)
        self.assertEqual(metric.record()['counted_intervals'],2)
    def test_partial_score_requires_ordered_completed_carries(self):
        record=dict(task_contract=dict(required_arm_ids=['arm_0','arm_1','arm_2']),completed_stage_ids=['arm_0__approach','arm_0__carry','arm_2__carry'])
        self.assertEqual(partial_progress(record),dict(valid_carry_count=1,required_carry_count=3,valid_carry_stage_ids=['arm_0__carry']))
    def test_nonfinite_pose_cannot_score(self):
        with self.assertRaises(ContractError):world_center(dict(position_m=[float('nan'),0,0],quaternion_wxyz=[1,0,0,0]),[0,0,-.067])

if __name__=='__main__':unittest.main()

