"""Provided carry-only rigid-bead dynamics with explicit nonphysical handling.

The installed SDK is untouched. CARRY calls the original bound substep with its
original configuration. The caller owns any temporary observer wrapper. OFF uses native awake-list branches only for dynamics/integration, with
private lists excluding beads, and omits native automatic island sleep/wakeup.
This is a provided extension, not a public Genesis activation API.
"""
import copy,dataclasses,hashlib,json,math
from pathlib import Path
import numpy as np

SCHEMA='provided-ideal-bead-handling-native-v1'
SDK_PINS={'engine/solvers/base_solver.py':'849a7dd38a36da96e68aad5d777bd29a200479946962424ca90dbc8299f9cbe6','engine/solvers/rigid/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'engine/solvers/rigid/abd/__init__.py': '54a64f24959ab148714ebde751d600e976ace56efc8dd2770209d6889db9727e', 'engine/solvers/rigid/abd/accessor.py': '64250f83e393119cdec38f6bfdac243a68fc8701e3a6109e1775c78c3513c54d', 'engine/solvers/rigid/abd/diff.py': 'fd009c89f977985228d09429daf059ed9e1a797f2545ad5b79f9681e3bdb61c5', 'engine/solvers/rigid/abd/forward_dynamics.py': '464f6deae2e8c61bc43d7ed483e07dd4bd71fc6cdbec150a2af7461ba92a5710', 'engine/solvers/rigid/abd/forward_kinematics.py': 'c78116d05f8d7d2eb69d4b728f4369527f0530e1b61f6f367564262fd59e4497', 'engine/solvers/rigid/abd/inverse_kinematics.py': 'ddc847e984c1368ed9c410e19bcd53772c8bad38c2d148d3ff3baaf73a9ae1e4', 'engine/solvers/rigid/abd/manual_bw.py': 'e1c1274d0a3f7e4dc7ef3b8d1798a0521a2f1f223929371bb1cf6cea8217510c', 'engine/solvers/rigid/abd/misc.py': 'bb3d2c80cf68c3fe89536b1b798c5913f62390ef42f58747021b63cdbf9f8ca2', 'engine/solvers/rigid/collider/__init__.py': 'e24b0419713c0c56afad32f6bb4ea2347fb655bdba832762c3f17ea456d6a75d', 'engine/solvers/rigid/collider/box_contact.py': '69ad3a8401f58bb993a394d83af4b1071d779f2c082827f02a768bee4d6b3ab1', 'engine/solvers/rigid/collider/broadphase.py': '923e6ebdd3612233ea751d74c2f532ecdbe305c392a97782a13b9b66af8c739f', 'engine/solvers/rigid/collider/capsule_contact.py': '59dda84cd9eaff5cbf81e13f0cde7bbb79e8921465ac3d4245bb7983f17f54ea', 'engine/solvers/rigid/collider/collider.py': '496e2f56acdfb7bd20822c993ba12d275824335759b49cbc965347a64c833cc0', 'engine/solvers/rigid/collider/constants.py': '81654d0296a3de62094afea3aec5412b864734762996c3a65c520902a61d5c4a', 'engine/solvers/rigid/collider/contact.py': 'cd11feed70d9badfa9e56327e71cd49bcf9fe280bd8ed774b890ef6a1226023c', 'engine/solvers/rigid/collider/diff_gjk.py': '14274de44e9afae1689b37ba3fb7df987808d7093605ebab29f6e49fd60c91db', 'engine/solvers/rigid/collider/epa.py': '62fb3cab6a2f167d0de9509971fca6e489fab3dba6a21c515d1d2447fcb1c6c9', 'engine/solvers/rigid/collider/gjk.py': '186a773fd6871af401d2a3bf927c51cdf12f7582189c20ae9470c19ac6114914', 'engine/solvers/rigid/collider/gjk_support.py': '58d9a3785d55db6468dfa2638828763db3007a071b78635d000d9ca0e97b79f8', 'engine/solvers/rigid/collider/gjk_utils.py': '10de98b82b30ba74714d7ec2901962c5d1a057a4cb675b4e08f0051ba8559269', 'engine/solvers/rigid/collider/mpr.py': '20674c3cf5379a823e3083dd69afa76f4c6d89e5fbd48feb668a9758466cbc8b', 'engine/solvers/rigid/collider/multi_contact.py': 'eebf3851a7a6d065655db2a0b7f3bc15f08acd182ebbd4ec90d5c674e3bfe6b0', 'engine/solvers/rigid/collider/narrowphase.py': '9e74a0b4c57657aab82b36438745055e2c5d136ac860ce8d598ed1213c41b2d6', 'engine/solvers/rigid/collider/support_field.py': 'fd79d8139cb3a517467fd5fd45e89c29c9f54be9b592a8b115329bc27b22d6da', 'engine/solvers/rigid/collider/utils.py': 'b6f4f7f17b78f1553d75b762708dfdccd4ee1503ad62a7f466dbe0645f59de74', 'engine/solvers/rigid/constraint/__init__.py': 'e63a2f381b17324bef72497f0ea835e62d8500254c474a9d7577e4d7d8a863f8', 'engine/solvers/rigid/constraint/backward.py': '492a6e6f945841be875608148f0585e0f81bbfde8d565be8317dc59e2ed8fbfc', 'engine/solvers/rigid/constraint/island.py': 'c86af8c959de3f8a8eccac82d0f71764918a98a6ff9431d5cd8417969214846b', 'engine/solvers/rigid/constraint/noslip.py': 'a3e39bf974d84f5bbfacb96511eed75766a3606302e3f011f4df8a166ed71ef2', 'engine/solvers/rigid/constraint/solver.py': 'f0753ff78bfe93b42d82ecb5c81e440cf0c521a3588efa3a4663a7c82e3b4d0f', 'engine/solvers/rigid/constraint/solver_breakdown.py': '268432a7953ea4650103bd69b7ebc17c9858ae72c8fff6a5fc891a2699405ce0', 'engine/solvers/rigid/rigid_solver.py': 'e076854af17262323041544ff692d366402a700357053618c8d89fed1e86b19f', 'utils/array_class.py': '459ff6c67a3080d5ebf71e746d59f6746e6e76f11a1d534dfbfc7a78dd049d34', 'utils/geom.py': '57affcc68874030ba0f0562516137d1a5c6373ae606037b2de34d4349a1499e3', 'engine/entities/rigid_entity/rigid_entity.py': 'b0f86f9c9ebdf62b9df2e5aadc0719c28e06cd52e0ce1bef452b1a269d9b2092'}
def _sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def _digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def _array_sha(a):return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()
def _need(value,message):
 if not value:raise RuntimeError(message)
def _host(v):return np.asarray(v.detach().cpu().numpy() if hasattr(v,'detach') else v).copy()
def _clock(scene):return [int(scene.t),float(scene.cur_t),int(scene.sim.cur_substep_global),int(scene.sim.cur_substep_local)]

def _kernels(qd,gs,arrays):
 from genesis.engine.solvers.rigid.abd.forward_dynamics import func_update_acc,func_implicit_damping,func_integrate,func_compute_mass_matrix,func_factor_mass,func_update_force,func_bias_force,func_compute_qacc
 from genesis.engine.solvers.rigid.abd.misc import func_check_index_range,func_add_safe_backward
 import genesis.utils.geom as gu
 from genesis.engine.solvers.rigid.abd.diff import func_copy_next_to_curr
 from genesis.engine.solvers.rigid.abd.forward_kinematics import func_update_cartesian_space,func_forward_velocity

 @qd.func
 def func_off_torque_and_passive_force(dyn_state: arrays.DynState, constraint_state: arrays.ConstraintState, dyn_info: arrays.DynInfo, rigid_info: arrays.RigidInfo, rigid_config: qd.template(), is_backward: qd.template()):
     BW = qd.static(is_backward)
     qd.loop_config(serialize=rigid_config.para_level < gs.PARA_LEVEL.PARTIAL)
     for i_slot, i_b in qd.ndrange(dyn_info.entities.n_links.shape[0], dyn_state.dofs.ctrl_mode.shape[1]):
         if i_slot < rigid_info.n_awake_entities[i_b]:
             i_e = rigid_info.awake_entities[i_slot, i_b]
             EPS = rigid_info.EPS[None]
             wakeup = False
             for i_l in range(dyn_info.entities.link_start[i_e], dyn_info.entities.link_end[i_e]):
                 I_l = [i_l, i_b] if qd.static(rigid_config.batch_links_info) else i_l
                 if dyn_info.links.n_dofs[I_l] > 0:
                     i_j = dyn_info.links.joint_start[I_l]
                     I_j = [i_j, i_b] if qd.static(rigid_config.batch_joints_info) else i_j
                     joint_type = dyn_info.joints.type[I_j]
                     for i_d in range(dyn_info.links.dof_start[I_l], dyn_info.links.dof_end[I_l]):
                         I_d = [i_d, i_b] if qd.static(rigid_config.batch_dofs_info) else i_d
                         force = gs.qd_float(0.0)
                         if dyn_state.dofs.ctrl_mode[i_d, i_b] == gs.CTRL_MODE.FORCE:
                             force = dyn_state.dofs.ctrl_force[i_d, i_b]
                         elif dyn_state.dofs.ctrl_mode[i_d, i_b] == gs.CTRL_MODE.VELOCITY:
                             force = -dyn_info.dofs.act_bias[I_d][2] * (dyn_state.dofs.ctrl_vel[i_d, i_b] - dyn_state.dofs.vel[i_d, i_b])
                         elif dyn_state.dofs.ctrl_mode[i_d, i_b] == gs.CTRL_MODE.POSITION and (not (joint_type == gs.JOINT_TYPE.FREE and i_d >= dyn_info.links.dof_start[I_l] + 3)):
                             force = dyn_info.dofs.act_gain[I_d] * (dyn_state.dofs.ctrl_pos[i_d, i_b] - dyn_state.dofs.pos[i_d, i_b]) + dyn_info.dofs.act_bias[I_d][0] + (dyn_info.dofs.act_gain[I_d] + dyn_info.dofs.act_bias[I_d][1]) * dyn_state.dofs.pos[i_d, i_b] + dyn_info.dofs.act_bias[I_d][2] * (dyn_state.dofs.vel[i_d, i_b] - dyn_state.dofs.ctrl_vel[i_d, i_b])
                         dyn_state.dofs.qf_applied[i_d, i_b] = qd.math.clamp(force, dyn_info.dofs.force_range[I_d][0], dyn_info.dofs.force_range[I_d][1])
                         if qd.abs(force) > EPS:
                             wakeup = True
                     dof_start = dyn_info.links.dof_start[I_l]
                     if joint_type == gs.JOINT_TYPE.FREE and (dyn_state.dofs.ctrl_mode[dof_start + 3, i_b] == gs.CTRL_MODE.POSITION or dyn_state.dofs.ctrl_mode[dof_start + 4, i_b] == gs.CTRL_MODE.POSITION or dyn_state.dofs.ctrl_mode[dof_start + 5, i_b] == gs.CTRL_MODE.POSITION):
                         xyz = qd.Vector([dyn_state.dofs.pos[0 + 3 + dof_start, i_b], dyn_state.dofs.pos[1 + 3 + dof_start, i_b], dyn_state.dofs.pos[2 + 3 + dof_start, i_b]], dt=gs.qd_float)
                         ctrl_xyz = qd.Vector([dyn_state.dofs.ctrl_pos[0 + 3 + dof_start, i_b], dyn_state.dofs.ctrl_pos[1 + 3 + dof_start, i_b], dyn_state.dofs.ctrl_pos[2 + 3 + dof_start, i_b]], dt=gs.qd_float)
                         quat = gu.qd_xyz_to_quat(xyz)
                         ctrl_quat = gu.qd_xyz_to_quat(ctrl_xyz)
                         q_diff = gu.qd_transform_quat_by_quat(ctrl_quat, gu.qd_inv_quat(quat))
                         rotvec = gu.qd_quat_to_rotvec(q_diff, EPS)
                         for j in qd.static(range(3)):
                             i_d = dof_start + 3 + j
                             I_d = [i_d, i_b] if qd.static(rigid_config.batch_dofs_info) else i_d
                             force = dyn_info.dofs.act_gain[I_d] * rotvec[j] + dyn_info.dofs.act_bias[I_d][0] + (dyn_info.dofs.act_gain[I_d] + dyn_info.dofs.act_bias[I_d][1]) * dyn_state.dofs.pos[i_d, i_b] + dyn_info.dofs.act_bias[I_d][2] * (dyn_state.dofs.vel[i_d, i_b] - dyn_state.dofs.ctrl_vel[i_d, i_b])
                             dyn_state.dofs.qf_applied[i_d, i_b] = qd.math.clamp(force, dyn_info.dofs.force_range[I_d][0], dyn_info.dofs.force_range[I_d][1])
                             if qd.abs(force) > EPS:
                                 wakeup = True
     qd.loop_config(serialize=rigid_config.para_level < gs.PARA_LEVEL.ALL)
     for i_0, i_b in qd.ndrange(1, dyn_state.dofs.ctrl_mode.shape[1]) if qd.static(rigid_config.use_hibernation) else qd.ndrange(dyn_state.dofs.ctrl_mode.shape[0], dyn_state.dofs.ctrl_mode.shape[1]):
         for i_1 in range(rigid_info.n_awake_dofs[i_b]) if qd.static(rigid_config.use_hibernation) else qd.static(range(1)):
             if func_check_index_range(i_1, 0, rigid_info.n_awake_dofs[i_b], rigid_config.use_hibernation):
                 i_d = rigid_info.awake_dofs[i_1, i_b] if qd.static(rigid_config.use_hibernation) else i_0
                 I_d = [i_d, i_b] if qd.static(rigid_config.batch_dofs_info) else i_d
                 dyn_state.dofs.qf_passive[i_d, i_b] = -dyn_info.dofs.damping[I_d] * dyn_state.dofs.vel[i_d, i_b]
     qd.loop_config(serialize=rigid_config.para_level < gs.PARA_LEVEL.ALL)
     for i_0, i_b in qd.ndrange(1, dyn_state.dofs.ctrl_mode.shape[1]) if qd.static(rigid_config.use_hibernation) else qd.ndrange(dyn_info.links.root_idx.shape[0], dyn_state.dofs.ctrl_mode.shape[1]):
         for i_1 in range(rigid_info.n_awake_links[i_b]) if qd.static(rigid_config.use_hibernation) else qd.static(range(1)):
             if func_check_index_range(i_1, 0, rigid_info.n_awake_links[i_b], rigid_config.use_hibernation):
                 i_l = rigid_info.awake_links[i_1, i_b] if qd.static(rigid_config.use_hibernation) else i_0
                 I_l = [i_l, i_b] if qd.static(rigid_config.batch_links_info) else i_l
                 if dyn_info.links.n_dofs[I_l] > 0:
                     i_j = dyn_info.links.joint_start[I_l]
                     I_j = [i_j, i_b] if qd.static(rigid_config.batch_joints_info) else i_j
                     joint_type = dyn_info.joints.type[I_j]
                     if joint_type != gs.JOINT_TYPE.FREE and joint_type != gs.JOINT_TYPE.FIXED:
                         dof_start = dyn_info.links.dof_start[I_l]
                         dof_end = dyn_info.links.dof_end[I_l]
                         for j_d in range(dof_end - dof_start):
                             I_d = [dof_start + j_d, i_b] if qd.static(rigid_config.batch_dofs_info) else dof_start + j_d
                             func_add_safe_backward([dof_start + j_d, i_b], -dyn_state.dofs.pos[dof_start + j_d, i_b] * dyn_info.dofs.stiffness[I_d], dyn_state.dofs.qf_passive, BW)

 @qd.kernel
 def reset_state(qids:qd.Tensor,dids:qd.Tensor,target:qd.Tensor,state:arrays.DynState,info:arrays.RigidInfo,constraints:arrays.ConstraintState,dyn:arrays.DynInfo,cfg:qd.template()):
  for i in range(qids.shape[0]):
   q=qids[i];info.qpos[q,0]=target[i];info.qpos_next[q,0]=target[i]
  for i in range(dids.shape[0]):
   d=dids[i]
   state.dofs.vel[d,0]=0.;state.dofs.vel_prev[d,0]=0.;state.dofs.vel_next[d,0]=0.
   state.dofs.acc[d,0]=0.;state.dofs.acc_smooth[d,0]=0.;state.dofs.force[d,0]=0.
   state.dofs.qf_bias[d,0]=0.;state.dofs.qf_passive[d,0]=0.;state.dofs.qf_actuator[d,0]=0.
   state.dofs.qf_applied[d,0]=0.;state.dofs.qf_smooth[d,0]=0.;state.dofs.qf_constraint[d,0]=0.
   state.dofs.ctrl_force[d,0]=0.;state.dofs.ctrl_vel[d,0]=0.;state.dofs.ctrl_mode[d,0]=gs.CTRL_MODE.FORCE
   constraints.qacc[d,0]=0.;constraints.qacc_ws[d,0]=0.;constraints.qfrc_constraint[d,0]=0.
  func_update_cartesian_space(state,dyn,info,cfg,force_update_fixed_geoms=False,is_backward=False)
  func_forward_velocity(state,dyn,info,cfg,is_backward=False)
 @qd.kernel
 def prepare(entity_mask:qd.Tensor,info:arrays.RigidInfo):
  for e in range(entity_mask.shape[0]):info.mass_mat_mask[e,0]=entity_mask[e]==0
 @qd.kernel
 def start(state:arrays.DynState,constraints:arrays.ConstraintState,dyn:arrays.DynInfo,info:arrays.RigidInfo,cfg:qd.template(),pos_updated:qd.template(),vel_updated:qd.template()):
  if qd.static(not pos_updated):func_update_cartesian_space(state,dyn,info,cfg,force_update_fixed_geoms=False,is_backward=False)
  if qd.static(not vel_updated):func_forward_velocity(state,dyn,info,cfg,is_backward=False)
  func_compute_mass_matrix(state,dyn,info,cfg,qd.static(cfg.integrator==gs.integrator.approximate_implicitfast),False)
  func_factor_mass(state,dyn,info,cfg,implicit_damping=False)
  func_off_torque_and_passive_force(state,constraints,dyn,info,cfg,False)
  func_update_acc(state,dyn,info,cfg,update_cacc=False,is_backward=False)
  func_update_force(state,dyn,info,cfg,False)
  func_bias_force(state,dyn,info,cfg,False)
  func_compute_qacc(state,dyn,info,cfg)
 @qd.kernel
 def finish(state:arrays.DynState,dyn:arrays.DynInfo,info:arrays.RigidInfo,cfg:qd.template(),errno:qd.Tensor):
  func_update_acc(state,dyn,info,cfg,update_cacc=True,is_backward=False)
  if qd.static(cfg.integrator!=gs.integrator.approximate_implicitfast):func_implicit_damping(state,dyn,info,cfg)
  func_integrate(state,dyn,info,cfg,is_backward=False)
  # Deliberately no automatic island sleep or awake-list aggregation here.
  func_copy_next_to_curr(state,info,cfg,errno)
  if qd.static(not cfg.enable_mujoco_compatibility):
   func_update_cartesian_space(state,dyn,info,cfg,force_update_fixed_geoms=False,is_backward=False)
   func_forward_velocity(state,dyn,info,cfg,is_backward=False)
 @qd.kernel
 def guard(qids:qd.Tensor,dids:qd.Tensor,qexpected:qd.Tensor,geom_mask:qd.Tensor,state:arrays.DynState,info:arrays.RigidInfo,collider:arrays.ColliderState,flags:qd.Tensor):
  for i in range(qids.shape[0]):
   if info.qpos[qids[i],0]!=qexpected[i]:qd.atomic_max(flags[1],1)
  for i in range(dids.shape[0]):
   d=dids[i]
   if state.dofs.vel[d,0]!=0.:qd.atomic_max(flags[2],1)
   if state.dofs.ctrl_mode[d,0]!=gs.CTRL_MODE.FORCE or state.dofs.ctrl_force[d,0]!=0.:qd.atomic_max(flags[3],1)
  for i in range(collider.n_contacts[0]):
   if geom_mask[collider.contact_data.geom_a[i,0]] or geom_mask[collider.contact_data.geom_b[i,0]]:qd.atomic_max(flags[4],1)
  flags[0]+=1
 return reset_state,prepare,start,finish,guard

class IdealBeadHandling:
 def __init__(self,scene,*,cup,beads,identities):
  import genesis as gs
  import quadrants as qd
  import genesis.utils.array_class as arrays
  self.scene=scene;self.solver=scene.rigid_solver;self.cup=cup;self.beads=tuple(beads);self.ids=copy.deepcopy(identities)
  r=self.solver
  _need(len(beads)==len(identities)==3 and scene.is_built and scene.n_envs==0,'Exactly three built unbatched native beads required')
  _need(not r._use_hibernation and not r.rigid_config.requires_grad and not r._is_backward,'Original forward nonhibernating solver required')
  _need(all(b.solver is r and b.n_dofs==6 and b.n_qs==7 and b.n_links==1 for b in beads),'Original free rigid spheres required')
  _need(all(b.idx==i['entity_idx'] and float(_host(b.get_mass()).reshape(-1)[0])>0 for b,i in zip(beads,identities)),'Native bead identities differ')
  _need(len(scene.sim.active_solvers)==1,'Prototype excludes external solver coupling')
  self.original_config=r.rigid_config;self.original_info=r.rigid_info;self.original_substep=r.substep
  self.native_identity=(id(scene),id(r),id(r.dyn_state),id(r.dyn_info),id(r.rigid_info))
  self.gs,self.qd,self.arrays=gs,qd,arrays
  self.kernel_reset,self.kernel_prepare,self.kernel_start,self.kernel_finish,self.kernel_guard=_kernels(qd,gs,arrays)
  self.mode='CARRY';self.events=[];self.count_off=0;self.count_carry=0;self.last_reset_clock=None;self.ready=False
  self.qids=np.asarray([q for b in beads for q in range(b.q_start,b.q_start+7)],dtype=np.int32)
  self.dids=np.asarray([d for b in beads for d in range(b.dof_start,b.dof_start+6)],dtype=np.int32)
  self.entities={b.idx for b in beads};self.links={b.link_start for b in beads};self.geoms={g.idx for b in beads for g in b.geoms}
  self.entity_mask=np.asarray([int(e in self.entities) for e in range(r.n_entities)],dtype=np.int32)
  self.geom_mask=np.asarray([int(g in self.geoms) for g in range(r.n_geoms)],dtype=np.int32)
  self.qkeep=np.asarray([q for q in range(r.n_qs) if q not in set(self.qids)],dtype=np.int32)
  self.dkeep=np.asarray([d for d in range(r.n_dofs) if d not in set(self.dids)],dtype=np.int32)
  values={k:getattr(r.rigid_config,k) for k in type(r.rigid_config).__annotations__};values['use_hibernation']=True
  self.off_config=type(r.rigid_config)(**values)
  changes={}
  for singular,total,excluded in [('dofs',r.n_dofs,set(self.dids)),('links',r.n_links,self.links),('entities',r.n_entities,self.entities)]:
   active=[i for i in range(total) if i not in excluded]
   count=arrays.V(dtype=gs.qd_int,shape=(1,));count.from_numpy(np.asarray([len(active)],dtype=gs.np_int))
   indices=arrays.V(dtype=gs.qd_int,shape=(total,1));a=np.zeros((total,1),dtype=gs.np_int);a[:len(active),0]=active;indices.from_numpy(a)
   changes['n_awake_'+singular]=count;changes['awake_'+singular]=indices
  mask=arrays.V(dtype=gs.qd_bool,shape=r.rigid_info.mass_mat_mask.shape);mask.from_numpy(r.rigid_info.mass_mat_mask.to_numpy())
  changes['mass_mat_mask']=mask;self.off_info=dataclasses.replace(r.rigid_info,**changes)
  self.original_pairs=r.collider._collider_info.collision_pair_idx.to_numpy().copy()
  self.off_pairs=self.original_pairs.copy();self.off_pairs[list(self.geoms),:]=-1;self.off_pairs[:,list(self.geoms)]=-1
  self.nonbead_pair_mask=np.ones(self.original_pairs.shape,dtype=bool);self.nonbead_pair_mask[list(self.geoms),:]=False;self.nonbead_pair_mask[:,list(self.geoms)]=False
  self.cache_ids=np.unique(self.original_pairs[~self.nonbead_pair_mask]);self.cache_ids=self.cache_ids[self.cache_ids>=0]
  def device(values,dtype):
   out=arrays.V(dtype=dtype,shape=values.shape);out.from_numpy(values);return out
  self.device_qids=device(self.qids,gs.qd_int);self.device_dids=device(self.dids,gs.qd_int)
  self.device_entities=device(self.entity_mask,gs.qd_int);self.device_geoms=device(self.geom_mask,gs.qd_int)
  self.device_expected=device(np.zeros(21,dtype=gs.np_float),gs.qd_float)
  self.device_flags=device(np.zeros(5,dtype=gs.np_int),gs.qd_int)
  self.device_target=device(np.zeros(21,dtype=gs.np_float),gs.qd_float)
  from genesis.engine.solvers.base_solver import mutates,StateChange
  @mutates(StateChange.GEOMETRY,StateChange.DYNAMICS,links='links_idx')
  def apply_reset(solver,gate,links_idx=None,envs_idx=None):
   gate.kernel_reset(gate.device_qids,gate.device_dids,gate.device_target,solver.dyn_state,
                     solver.rigid_info,solver.constraint_solver.constraint_state,solver.dyn_info,solver.rigid_config)
   solver._is_forward_pos_updated=True;solver._is_forward_vel_updated=True
  self._apply_reset=apply_reset;self.last_reset_off_count=0
  self.source_hashes=self._sources()
 def _sources(self):
  import genesis
  base=Path(genesis.__file__).resolve().parent
  pins={str(base/name):expected for name,expected in SDK_PINS.items()}
  _need(bool(pins) and all(Path(p).is_file() and _sha(p)==h for p,h in pins.items()),'Installed native source closure changed')
  pins[str(Path(__file__).resolve())]=_sha(__file__);return pins
 def _check(self):
  r=self.solver
  _need((id(self.scene),id(r),id(r.dyn_state),id(r.dyn_info),id(r.rigid_info))==self.native_identity,'Native scene/state replaced')
  _need(r.rigid_config is self.original_config and r._use_hibernation is False,'Original solver configuration changed')
  _need(all(_sha(p)==h for p,h in self.source_hashes.items()),'Phase-gate sources changed')
 def _constraint_cache_hash(self):
  r=self.solver;cs=r.constraint_solver.constraint_state;cache=r.collider._collider_state.contact_cache
  h=hashlib.sha256()
  for field in [cs.qacc,cs.qacc_ws,cs.qfrc_constraint]:h.update(np.ascontiguousarray(field.to_numpy()[self.dkeep]).tobytes())
  keep=np.ones(cache.penetration.shape[0],dtype=bool);keep[self.cache_ids]=False
  for field in [cache.normal,cache.penetration]:h.update(np.ascontiguousarray(field.to_numpy()[keep]).tobytes())
  return h.hexdigest()
 def _snapshot(self):
  r=self.solver;q=r.rigid_info.qpos.to_numpy()[:,0];v=r.dyn_state.dofs.vel.to_numpy()[:,0];pairs=r.collider._collider_info.collision_pair_idx.to_numpy()
  return {'non_bead_constraint_cache_sha256':self._constraint_cache_hash(),'scene_identity':list(self.native_identity),'native_clock':_clock(self.scene),'non_bead_qpos_sha256':_array_sha(q[self.qkeep]),'non_bead_qvel_sha256':_array_sha(v[self.dkeep]),'bead_identities':copy.deepcopy(self.ids),'bead_qpos':q[self.qids].reshape(3,7).tolist(),'bead_qvel':v[self.dids].reshape(3,6).tolist(),'mode':self.mode,'collision_pair_sha256':_array_sha(pairs),'non_bead_collision_pair_sha256':_array_sha(pairs[self.nonbead_pair_mask])}
 def _record(self,event,before,**extra):
  after=self._snapshot()
  for key in ['native_clock','scene_identity','non_bead_qpos_sha256','non_bead_qvel_sha256','non_bead_constraint_cache_sha256','bead_identities','non_bead_collision_pair_sha256']:_need(before[key]==after[key],'Bead operation changed protected state: '+key)
  row={'event':event,'native_clock':after['native_clock'],'before':before,'after':after,'physical_steps':0,'source_hashes':copy.deepcopy(self.source_hashes),**extra}
  self.events.append(row);return copy.deepcopy(row)
 def _clear_bead_cache(self):
  cache=self.solver.collider._collider_state.contact_cache
  for field in [cache.normal,cache.penetration]:
   a=field.to_numpy();a[self.cache_ids]=0;field.from_numpy(a)
  self.solver.collider._contact_data_cache.clear()
 def pause(self):
  self._check();_need(self.mode=='CARRY','Pause requires CARRY state');before=self._snapshot()
  self.solver.collider._collider_info.collision_pair_idx.from_numpy(self.off_pairs)
  self._clear_bead_cache();self.mode='OFF';self.ready=False
  return self._record('pause',before)
 def reset(self,body_local_positions):
  self._check();_need(self.mode=='OFF','Reset is permitted only with dynamics and contacts OFF')
  local=np.asarray(body_local_positions,dtype=float);_need(local.shape==(3,3) and np.isfinite(local).all(),'Three finite body-local positions required')
  if self.ready:self.assert_paused()
  before=self._snapshot()
  from physics.carried_body import WorldPose
  body=WorldPose(_host(self.cup.get_pos(relative=False)).tolist(),_host(self.cup.get_quat(relative=False)).tolist())
  world=[];geometric_world=[];target=[]
  for bead,position in zip(self.beads,local):
   geometric=list(body.transform_point(position.tolist()));geometric_world.append(geometric)
   center=np.asarray(geometric,dtype=self.gs.np_float).tolist();world.append(center)
   target.extend(center+np.asarray(body.quaternion_wxyz,dtype=self.gs.np_float).tolist())
  self.device_target.from_numpy(np.asarray(target,dtype=self.gs.np_float))
  self._apply_reset(self.solver,self,links_idx=np.asarray(sorted(self.links),dtype=np.int32))
  self.device_expected.from_numpy(self.solver.rigid_info.qpos.to_numpy()[self.qids,0].astype(self.gs.np_float))
  self.device_flags.fill(0);self.ready=True;self.last_reset_clock=_clock(self.scene);self.last_reset_off_count=self.count_off
  self._clear_bead_cache()
  return self._record('reset',before,requested_body_local_positions_m=local.tolist(),requested_world_positions_m=world,geometric_world_positions_m=geometric_world,zero_velocity=True)
 def _off_substep(self,f):
  _need(self.mode=='OFF' and self.ready,'OFF requires an authenticated boundary reset before stepping')
  r=self.solver
  self.kernel_prepare(self.device_entities,self.off_info)
  self.kernel_start(r.dyn_state,r.constraint_solver.constraint_state,r.dyn_info,self.off_info,self.off_config,r._is_forward_pos_updated,r._is_forward_vel_updated)
  r._func_constraint_force() # Original global config, original robot/cup contact and constraint solver.
  self.kernel_finish(r.dyn_state,r.dyn_info,self.off_info,self.off_config,r._errno)
  r._is_forward_pos_updated=not self.original_config.enable_mujoco_compatibility
  r._is_forward_vel_updated=not self.original_config.enable_mujoco_compatibility
  self.kernel_guard(self.device_qids,self.device_dids,self.device_expected,self.device_geoms,r.dyn_state,r.rigid_info,r.collider._collider_state,self.device_flags)
  self.count_off+=1
 def assert_paused(self):
  _need(self.mode=='OFF' and self.ready,'Not in a ready OFF phase')
  flags=self.flags
  _need(not flags[1:].any(),'Inactive bead integration, velocity, actuator or collision detected: '+str(flags.tolist()))
  _need(int(flags[0])==self.count_off-self.last_reset_off_count,'Inactive bead guard coverage is incomplete')
  _need(np.array_equal(self.solver.collider._collider_info.collision_pair_idx.to_numpy(),self.off_pairs),'OFF collision matrix changed')
  return True
 def resume(self):
  self._check();self.assert_paused();_need(self.last_reset_clock==_clock(self.scene),'Resume requires a fresh same-clock reset')
  before=self._snapshot();self.solver.collider._collider_info.collision_pair_idx.from_numpy(self.original_pairs);self._clear_bead_cache()
  self.mode='CARRY';self.ready=False
  return self._record('resume',before,original_substep_restored=True,original_config_restored=self.solver.rigid_config is self.original_config)
 @property
 def flags(self):
  return self.device_flags.to_numpy().copy()
 def snapshot(self):
  self._check();return self._snapshot()
 def step(self,original_substep,*args,**kwargs):
  # The caller owns the temporary contact/control observer wrapper.
  # CARRY dispatches the exact original native bound method without alterations.
  _need(original_substep==self.original_substep,'Phase gate requires the original native substep')
  if self.mode=='CARRY':
   result=original_substep(*args,**kwargs);self.count_carry+=1;return result
  return self._off_substep(*args,**kwargs)
 def counter_snapshot(self):
  return {'mode':self.mode,'native_clock':_clock(self.scene),'scene_identity':list(self.native_identity),
          'off_native_substeps':self.count_off,'carry_native_substeps':self.count_carry}
 def checkpoint(self,start):
  self._check();self.assert_paused();end=self.counter_snapshot()
  _need(type(start) is dict and start.get('mode')=='OFF' and start.get('scene_identity')==end['scene_identity'],'Wrong OFF phase start')
  count=end['off_native_substeps']-start['off_native_substeps']
  _need(count>=0 and end['carry_native_substeps']==start['carry_native_substeps']
        and end['native_clock'][2]-start['native_clock'][2]==count,'Incomplete OFF internal coverage')
  return {'schema_version':'provided-inactive-bead-phase-check-v1','passed':True,
          'mode':'OFF','bead_dynamics_evaluated':False,'bead_contacts_enabled':False,
          'off_internal_POST_samples':count,'off_native_substeps':count,
          'guard_flags':self.flags.tolist(),'bead_qpos_unchanged':True,'bead_qvel_zero':True,
          'no_bead_contact_observed':True,'start':copy.deepcopy(start),'end':end,
          'actual_native_snapshot':self._snapshot(),'source_hashes':copy.deepcopy(self.source_hashes)}
 def metadata(self):
  self._check()
  return {'schema_version':SCHEMA,'implementation':'provided-native-carry-only-phase-gate','mode':self.mode,'native_scene_identity':list(self.native_identity),'bead_identities':copy.deepcopy(self.ids),'off_native_substeps':self.count_off,'current_guard_flags':self.flags.tolist(),'events':copy.deepcopy(self.events),'source_hashes':copy.deepcopy(self.source_hashes),'carry_original_solver_path':self.mode=='CARRY','carry_native_substeps':self.count_carry,'dispatch':'caller_owned_observer_wrapper','full_mission_passed':False}

def bind(scene,*,cup,beads,identities):return IdealBeadHandling(scene,cup=cup,beads=beads,identities=identities)
