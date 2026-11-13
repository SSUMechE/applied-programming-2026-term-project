"""Source-bound native measurement policies. No planning or simulation setters."""
import math
import numpy as np
from transport_challenge.contracts import ContractError

DISTANCE_SCHEMA = 'ap-carried-cup-center-distance-v1'

def cavity_reference(geometry):
    point=np.asarray(geometry['interior_center_local_m'],float)
    if point.shape!=(3,) or not np.isfinite(point).all():raise ContractError('Missing finite cavity center')
    return point

def catchment(geometry):
    size,center_floor=geometry['component_boxes'][0]
    center=np.asarray(geometry['interior_center_local_m'],float)
    size=np.asarray(size,float)
    bottom=float(center_floor[2])-float(size[2])/2
    return np.asarray([*center[:2],bottom,*list(size[:2]/2)],float)

def whole_bead_escaped(local,radius,geometry,tolerance_m=.001):
    p=np.asarray(local,float);c=catchment(geometry)
    if p.shape!=(3,) or not np.isfinite(p).all() or not math.isfinite(radius) or radius<=0:
        raise ContractError('Finite bead position and positive radius required')
    return bool(abs(p[0]-c[0])-radius>c[3]+tolerance_m
        or abs(p[1]-c[1])-radius>c[4]+tolerance_m or p[2]+radius<c[2]-tolerance_m)

def world_center(pose,reference):
    p=np.asarray(pose['position_m'],float);q=np.asarray(pose['quaternion_wxyz'],float)
    if p.shape!=(3,) or q.shape!=(4,) or not np.isfinite(p).all() or not np.isfinite(q).all() or abs(np.linalg.norm(q)-1)>1e-4:
        raise ContractError('Finite actual body pose required for cup-center distance')
    q=q/np.linalg.norm(q);w,x,y,z=q
    r=np.asarray([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
        [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
        [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    return p+r@np.asarray(reference,float)

class CarriedDistance:
    def __init__(self,geometry):
        self.reference=cavity_reference(geometry);self.segments=[]
    def append(self,arm,start_step,initial_pose,payload_history):
        h=np.asarray(payload_history,float)
        if h.ndim!=2 or len(h)<1 or h.shape[1]<7:raise ContractError('Actual outer POST history required')
        points=[world_center(initial_pose,self.reference)]
        points.extend(world_center(dict(position_m=row[:3],quaternion_wxyz=row[3:7]),self.reference) for row in h)
        length=math.fsum(float(np.linalg.norm(b-a)) for a,b in zip(points,points[1:]))
        self.segments.append(dict(arm_id=arm,from_native_step=int(start_step),to_native_step=int(start_step)+len(h),path_length_m=length))
    def record(self):
        length=math.fsum(s['path_length_m'] for s in self.segments)
        return dict(schema_version=DISTANCE_SCHEMA,sample_dt_s=.0025,reference_point_body_m=self.reference.tolist(),
            path_length_m=length,distance_mm=int(math.floor(length*1000+.5)),
            counted_intervals=sum(s['to_native_step']-s['from_native_step'] for s in self.segments),
            segments=list(self.segments),rounding='nearest_mm_half_up_once_after_total',
            excludes=['unheld_approach','donor_return','released_settle'])
