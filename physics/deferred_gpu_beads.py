"""Deferred device POST audit of the original four beads and vessel tilt.

The supplied body-only variant checks tilt for native-particle runs. This new
audit records intermediate failures even when the final state looks valid.
"""
import copy
import hashlib
import importlib
import math
from pathlib import Path
import threading
import numpy as np
from transport_challenge.contracts import ContractError

_KERNELS={}


def require(ok,message):
    if not ok:raise ContractError(message)


def reference_inside(local,geometry,radii):
    """Reference for open-top loss, not for final bulk-rest cavity membership."""
    from physics.native_transport_metrics import whole_bead_escaped
    p=np.asarray(local,float);r=np.asarray(radii,float)
    require(p.shape==(len(r),3) and np.isfinite(p).all(),'All original finite bead centers required')
    return np.asarray([not whole_bead_escaped(p[i],radius,geometry) for i,radius in enumerate(r)],bool)


def bind_native_masses(logical_masses,native_values,native_dtype):
    """Preserve logical supply metadata and bind its one declared native cast."""
    dtype=np.dtype(native_dtype)
    require(dtype in (np.dtype('float32'),np.dtype('float64')),
            'Use the actual declared native floating precision')
    logical=np.asarray(logical_masses)
    actual=np.asarray(native_values)
    require(logical.ndim==1 and logical.dtype.kind in 'if' and np.isfinite(logical).all()
        and (logical>0).all() and actual.shape==logical.shape and actual.dtype==dtype
        and np.isfinite(actual).all(),'Original positive logical masses and actual native mass bytes required')
    expected=np.ascontiguousarray(logical.astype(dtype))
    require(np.array_equal(actual.view(np.uint8),expected.view(np.uint8)),
            'Actual initial native bead mass differs from the exact declared cast')
    return expected.astype(np.float64)


def _kernel(qd,arrays):
    key=(id(qd),id(arrays))
    if key in _KERNELS:return _KERNELS[key]
    @qd.func
    def finite(value):
        return (qd.bit_cast(qd.cast(value,qd.f64),qd.u64)&qd.u64(0x7ff0000000000000))!=qd.u64(0x7ff0000000000000)
    @qd.kernel
    def audit(ds:arrays.DynState,di:arrays.DynInfo,config:qd.template(),
              ids:qd.types.ndarray(),expected:qd.types.ndarray(),centers:qd.types.ndarray(),
              halves:qd.types.ndarray(),output:qd.types.ndarray(),history:qd.types.ndarray(),
              cup_link:qd.i32,n:qd.i32,index:qd.i32,step:qd.i32,tilt_limit:qd.f64,
              catchment:qd.types.ndarray(),internal:qd.i32,escape_steps:qd.types.ndarray(),escape_points:qd.types.ndarray()):
        for only in range(1):
            bad=0
            p=qd.cast(ds.links.pos[cup_link,0],qd.f64)
            quaternion=qd.cast(ds.links.quat[cup_link,0],qd.f64)
            norm=qd.sqrt(quaternion.dot(quaternion))
            if not finite(norm) or norm<=0 or qd.abs(norm-1)>qd.f64(1e-4):
                bad|=16
                norm=1.
            q=quaternion/norm
            w,x,y,z=q[0],q[1],q[2],q[3]
            R=qd.Matrix([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]],dt=qd.f64)
            tilt=qd.acos(qd.max(-1.,qd.min(1.,R[2,2])))*(qd.f64(180.)/qd.f64(math.pi))
            if not finite(tilt):bad|=1
            # Tilt is diagnostic. Actual whole-bead escape decides failure.
            if not internal:history[index,13]=tilt
            omega=qd.cast(ds.links.cd_ang[cup_link,0],qd.f64)
            linear=qd.cast(ds.links.cd_vel[cup_link,0],qd.f64)+omega.cross(
                p-qd.cast(ds.links.root_COM[cup_link,0],qd.f64))
            for a in qd.static(range(3)):
                if not internal:history[index,a],history[index,7+a],history[index,10+a]=p[a],linear[a],omega[a]
                if not finite(p[a]) or not finite(linear[a]) or not finite(omega[a]):bad|=1
            for a in qd.static(range(4)):
                if not internal:history[index,3+a]=quaternion[a]
                if not finite(quaternion[a]):bad|=1
            for i in range(n):
                link=ids[i]
                I=[link,0] if qd.static(config.batch_links_info) else link
                mass=qd.cast(di.links.inertial_mass[I],qd.f64)
                xyz=qd.cast(ds.links.pos[link,0],qd.f64)
                angular=qd.cast(ds.links.cd_ang[link,0],qd.f64)
                vel=qd.cast(ds.links.cd_vel[link,0],qd.f64)+angular.cross(
                    xyz-qd.cast(ds.links.root_COM[link,0],qd.f64))
                local=R.transpose()@(xyz-p)
                # Open top: a bead may bounce vertically and return. Loss is
                # sticky once its entire sphere leaves the outer catchment or
                # passes below the actual basin bottom (1 mm numeric tolerance).
                radius=expected[i,1]
                inside=1
                if qd.abs(local[0]-catchment[0])-radius>catchment[3]+qd.f64(.001):inside=0
                if qd.abs(local[1]-catchment[1])-radius>catchment[4]+qd.f64(.001):inside=0
                if local[2]+radius<catchment[2]-qd.f64(.001):inside=0
                if not finite(mass) or mass!=expected[i,0]:bad|=2
                if not inside:
                    bad|=4
                    if step<escape_steps[i]:
                        escape_steps[i]=step
                        for a in qd.static(range(3)):
                            escape_points[i,a]=xyz[a]
                            escape_points[i,3+a]=local[a]
                base=14+8*i
                for a in qd.static(range(3)):
                    if not internal:history[index,base+a],history[index,base+3+a]=xyz[a],vel[a]
                    if not finite(xyz[a]) or not finite(vel[a]):bad|=1
                if not internal:history[index,base+6],history[index,base+7]=inside,mass
            if bad:
                qd.atomic_or(output[0],bad)
                qd.atomic_min(output[1],qd.cast(step,qd.i64))
            if internal:output[3]+=1
            else:output[2]+=1
    _KERNELS[key]=audit
    return audit


class DeferredPayloadAudit:
    def __init__(self,scene,beads,tray,model,ids,*,tilt_limit_deg,capacity_steps,internal_spill=False):
        import genesis as gs
        import quadrants as qd
        from genesis.utils import array_class as arrays
        from physics.deferred_gpu_control import source_pins
        require(type(capacity_steps) is int and 0<capacity_steps<=20000,
                'Bound the complete actual POST window')
        require(gs.backend==gs.cuda and gs.use_zerocopy and scene.n_envs==0
                and scene.rigid_solver._B==1,'Deferred payload requires the original unbatched CUDA scene')
        require(len(beads)==len(ids) and len(beads) in (0,4),'Use original four beads or explicit particle body-only scope')
        require(type(tilt_limit_deg) in (int,float) and math.isfinite(tilt_limit_deg)
                and 0<=tilt_limit_deg<=180,'Pass the actual existing driver body tilt bound')
        self.scene,self.solver,self.tray=scene,scene.rigid_solver,tray
        self.beads=tuple(beads);self.identities=copy.deepcopy(ids)
        self.model=model;self.geometry=copy.deepcopy(model.geometry_record())
        self.tilt=float(tilt_limit_deg);self.capacity=capacity_steps
        self.qd=qd;self.start=int(scene.t);self.captures=0;self.closed=False
        self.internal_spill=bool(internal_spill);self.internal_captures=0;self.polls=0
        self.thread=threading.get_ident();self.native_state=self.solver.dyn_state;self.native_info=self.solver.dyn_info
        self.pins=source_pins(Path(gs.__file__).resolve().parent)
        for p in [Path(__file__),Path(__file__).with_name('scene_assets.py')]:
            self.pins[str(p.resolve())]=hashlib.sha256(p.read_bytes()).hexdigest()
        require(tray.scene is scene and tray._solver is self.solver,'Actual native cup owner differs')
        for bead,record in zip(self.beads,ids):
            require(bead.scene is scene and bead._solver is self.solver and bead.n_links==1
                and bead.name==record['entity'] and bead.idx==record['entity_idx']
                and record['radius_m']==.012 and record['mass_kg']>0,'Native bead identity/radius/mass contract differs')
        require(len({r['id'] for r in ids})==len(ids) and len({r['entity_idx'] for r in ids})==len(ids),
                'Do not drop/duplicate original bead identities')
        self.signature=self._signature()
        # One setup read authenticates native storage, without changing the
        # logical supply masses retained in self.identities.
        native_mass_field=np.asarray(self.native_info.links.inertial_mass.to_numpy())
        batch=self.solver.rigid_config.batch_links_info
        native_masses=np.asarray([native_mass_field[b.base_link_idx,0] if batch
            else native_mass_field[b.base_link_idx] for b in beads],dtype=native_mass_field.dtype)
        self.native_mass_dtype=np.dtype(gs.np_float)
        self.native_masses=bind_native_masses([r['mass_kg'] for r in ids],native_masses,self.native_mass_dtype)
        self.initial_native_mass_sha256=hashlib.sha256(np.ascontiguousarray(native_masses).tobytes()).hexdigest()
        def upload(value,integer=False):
            a=np.ascontiguousarray(value,dtype=np.int64 if integer else np.float64)
            result=qd.ndarray(dtype=qd.i64 if integer else qd.f64,shape=a.shape);result.from_numpy(a);return result
        # At least one inert row is allocated for the explicit body-only mode.
        self.link_ids=upload([b.base_link_idx for b in beads] or [-1],True)
        self.expected=upload([[float(mass),r['radius_m']] for mass,r in zip(self.native_masses,ids)] or [[0.,0.]])
        regions=self.geometry.get('interior_regions') or [dict(center_local_m=self.geometry['interior_center_local_m'],half_extents_m=self.geometry['interior_half_extents_m'])]
        self.centers=upload([r['center_local_m'] for r in regions]);self.halves=upload([r['half_extents_m'] for r in regions])
        from physics.native_transport_metrics import catchment
        from physics import native_transport_metrics
        p=Path(native_transport_metrics.__file__).resolve()
        self.pins[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        c=catchment(self.geometry)
        self.catchment_record=dict(center_xy_m=c[:2].tolist(),half_xy_m=c[3:].tolist(),bottom_m=float(c[2]),tolerance_m=.001)
        self.catchment=upload(c)
        self.output=upload([0,(self.start+capacity_steps+1)*16,0,0],True)
        self.escape_steps=upload([(self.start+capacity_steps+1)*16]*max(1,len(beads)),True)
        self.escape_points=upload(np.zeros((max(1,len(beads)),6)))
        self.history=qd.ndarray(dtype=qd.f64,shape=(capacity_steps,14+8*len(beads)))
        self.collect=_kernel(qd,arrays)

    def _signature(self):
        return (id(self.scene),id(self.solver),id(self.solver.dyn_state),id(self.solver.dyn_info),
            id(self.tray),self.tray.base_link_idx,tuple((id(b),b.idx,b.name,b.base_link_idx) for b in self.beads))

    def capture_post(self,index):
        require(not self.closed and threading.get_ident()==self.thread and index==self.captures
            and type(index) is int and index<self.capacity and int(self.scene.t)==self.start+index+1
            and self._signature()==self.signature,'Deferred payload POST owner/clock/sequence changed')
        self.collect(self.native_state,self.native_info,self.solver.rigid_config,self.link_ids,self.expected,
            self.centers,self.halves,self.output,self.history,self.tray.base_link_idx,len(self.beads),
            index,int(self.scene.t)*16,self.tilt,self.catchment,0,self.escape_steps,self.escape_points)
        self.captures+=1

    def capture_internal(self, ordinal):
        require(self.internal_spill and not self.closed and type(ordinal) is int
            and ordinal==self.start*16+self.internal_captures
            and self._signature()==self.signature,'Internal spill observer sequence changed')
        self.collect(self.native_state,self.native_info,self.solver.rigid_config,self.link_ids,self.expected,
            self.centers,self.halves,self.output,self.history,self.tray.base_link_idx,len(self.beads),
            0,ordinal+1,self.tilt,self.catchment,1,self.escape_steps,self.escape_points)
        self.internal_captures+=1

    def poll_failure(self):
        require(self.internal_spill and not self.closed and self.internal_captures==self.captures*16,
                'Poll only after all original internal substeps and outer POST')
        self.polls+=1
        return bool(self.output.to_numpy()[0])

    def finish(self, *, allow_prefix=False):
        require(not self.closed and 0<self.captures<=self.capacity
                and (allow_prefix or self.captures==self.capacity),'All observed payload POST samples are required')
        output=self.output.to_numpy().copy();history=self.history.to_numpy()[:self.captures].copy()
        escape_steps=self.escape_steps.to_numpy()[:len(self.beads)].copy()
        escape_points=self.escape_points.to_numpy()[:len(self.beads)].copy()
        escaped=[i for i in range(len(self.beads)) if escape_steps[i]<=(self.start+self.captures)*16]
        witnesses=[dict(bead_id=self.identities[i]['id'],global_substep=int(escape_steps[i]),
            simulation_s=int(escape_steps[i])*.0025/16,position_world_m=escape_points[i,:3].tolist(),
            position_body_m=escape_points[i,3:].tolist()) for i in escaped]
        witnesses.sort(key=lambda row:(row['global_substep'],next(i for i,r in enumerate(self.identities) if r['id']==row['bead_id'])))
        self.closed=True
        unchanged=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in self.pins.items())
        require(int(output[2])==self.captures,'GPU payload POST count differs from actual native intervals')
        require(not self.internal_spill or int(output[3])==self.internal_captures==self.captures*16, 'Internal spill coverage differs')
        flags=dict(nonfinite=bool(output[0]&1),native_mass_changed=bool(output[0]&2),
            bead_outside=bool(output[0]&4),body_tilt=bool(output[0]&8),quaternion_invalid=bool(output[0]&16))
        return dict(schema_version='deferred-native-payload-post-audit-v1',passed=not any(flags.values()) and unchanged,
            flags=flags,first_failed_step=math.ceil(int(output[1])/16) if output[0] else None,first_failed_global_substep=int(output[1]) if output[0] else None,actual_POST_samples=self.captures,
            original_bead_ids=self.identities,first_escape=witnesses[0] if witnesses else None,escape_witnesses=witnesses,
            escape_steps=escape_steps.tolist(),escape_points=escape_points.tolist(),bead_count=len(self.beads),body_tilt_limit_deg=None,body_tilt_is_diagnostic=True,
            actual_internal_POST_samples=self.internal_captures,compact_host_polls=self.polls,
            spill_policy='open-top-whole-sphere-outer-catchment-v1',catchment=self.catchment_record,
            observed_prefix_only=self.captures!=self.capacity,requested_POST_samples=self.capacity,
            native_mass_dtype=self.native_mass_dtype.str,expected_native_masses_kg=self.native_masses.tolist(),
            initial_native_mass_sha256=self.initial_native_mass_sha256,
            native_mass_binding='Exact declared-dtype cast verified against initial native storage. Original logical supply metadata is preserved.',
            history=history,history_layout='body pos3/quat4/vel3/ang3/tilt1, then each bead pos3/vel3/inside1/mass1',
            source_bindings_unchanged=unchanged,source_hashes=copy.deepcopy(self.pins),final_bulk_readbacks=4,
            carry_loop_host_state_reads=0,carry_loop_explicit_syncs=0,immediate_failure_halt_preserved=self.internal_spill,maximum_halt_latency_s=.0025 if self.internal_spill else None,
            full_transport_passed=False,scope='Every actual outer POST, with whole-sphere open-top loss detection.  '
                'Particle IDs and all internal contact/handling predicates are supplied by separate audits.')


DeferredBeadAudit=DeferredPayloadAudit
