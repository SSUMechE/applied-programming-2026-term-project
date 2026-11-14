/* Read-only validation and geometry transforms for the optional student preview. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AP_MOTION_PREVIEW = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function requireValue(ok, message) { if (!ok) throw new Error(message); }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function vector(value, length, label) {
    requireValue(Array.isArray(value) && value.length === length && value.every(finite), label + ' must contain ' + length + ' finite numbers.');
    return value;
  }
  function validateDesign(design, map) {
    requireValue(design && design.schema_version === 'three-arm-design-v1' && Array.isArray(design.arms) && design.arms.length === 3 && Array.isArray(design.relays) && design.relays.length === 2,
      'Use a completed design JSON with three arm entries and two support entries.');
    const bases = design.arms.map((arm, i) => {
      const mount = map.objects.find(o => o.mount && o.mount.id === arm.mount_region)?.mount;
      const point = arm.base_placement;
      requireValue(arm.id === 'arm_' + i && mount && point, 'Choose a valid mount for each arm.');
      for (const key of ['x_m', 'y_m', 'yaw_rad']) {
        requireValue(finite(point[key]) && point[key] >= mount[key][0] && point[key] <= mount[key][1], arm.id + ' ' + key + ' is outside its mounting region.');
      }
      return {id: arm.id, position_world_m: [point.x_m, point.y_m, mount.base_z_m], yaw_rad: point.yaw_rad};
    });
    const allRelays = design.relays.map(relay => {
      const support = map.relay_regions.find(s => s.support_id === relay.support_id);
      requireValue(support && ['x_m', 'y_m'].every(k => finite(relay[k]) && relay[k] >= support[k][0] && relay[k] <= support[k][1]), 'Choose allowed support IDs and XY coordinates.');
      return {support_id: relay.support_id, position_world_m: [relay.x_m, relay.y_m, support.support_top_z_m], body_root_z_m: support.body_root_z_m};
    });
    const handoffs = allRelays.slice(0, map.handoff_count);
    requireValue(handoffs.every(h => h.support_id !== map.terminal_support_id) && new Set(handoffs.map(h => h.support_id)).size === handoffs.length,
      'Internal handoff surfaces must be distinct and cannot be the fixed finish.');
    if (map.difficulty === 'easy' || map.difficulty === 'medium') {
      const fixed = allRelays[map.handoff_count];
      requireValue(fixed.support_id === map.terminal_support_id, 'Keep the fixed finish support unchanged.');
      requireValue(map.fixed_finish && fixed.position_world_m[0] === map.fixed_finish.position_world_m[0]
        && fixed.position_world_m[1] === map.fixed_finish.position_world_m[1], 'Keep the fixed finish XY coordinates unchanged.');
    }
    return {bases, handoffs};
  }
  function transformPoint(point, position, quaternion) {
    const [w, x, y, z] = quaternion, [vx, vy, vz] = point;
    const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
    return [position[0] + vx + w * tx + y * tz - z * ty,
      position[1] + vy + w * ty + z * tx - x * tz,
      position[2] + vz + w * tz + x * ty - y * tx];
  }
  function validatePreview(data, map) {
    requireValue(data && ['ap-student-motion-preview-v1', 'ap-student-physical-preview-v1'].includes(data.schema_version), 'Use a JSON produced by a supplied preview exporter.');
    const physical = data.schema_version === 'ap-student-physical-preview-v1';
    if (physical) {
      requireValue(Array.isArray(data.bead_ids) && data.bead_ids.length <= 100 && new Set(data.bead_ids).size === data.bead_ids.length
        && data.bead_ids.every(id => typeof id === 'string' && id.length > 0 && id.length <= 128), 'Invalid recorded bead IDs.');
      requireValue(finite(data.bead_radius_m) && data.bead_radius_m > 0 && data.bead_radius_m < 1, 'Invalid recorded bead radius.');
      requireValue(typeof data.result_sha256 === 'string' && /^[0-9a-f]{64}$/.test(data.result_sha256), 'Missing physical result hash.');
    }
    requireValue(data.difficulty === map.difficulty && data.map_id === map.map_id, 'This preview belongs to a different map. Select its complexity first.');
    const expectedKeys = Object.keys(map.source_files_sha256).sort();
    requireValue(data.source_files_sha256 && JSON.stringify(Object.keys(data.source_files_sha256).sort()) === JSON.stringify(expectedKeys)
      && expectedKeys.every(k => data.source_files_sha256[k] === map.source_files_sha256[k]), 'The preview map/source versions do not match this viewer.');
    for (const key of ['saved_plan_sha256', 'bundle_sha256', 'design_sha256'])
      requireValue(typeof data[key] === 'string' && /^[0-9a-f]{64}$/.test(data[key]), 'Missing or invalid ' + key + '.');
    const layout = validateDesign(data.design, map);
    const closeVector = (a, b) => Array.isArray(a) && a.length === b.length && a.every((v, i) => finite(v) && Math.abs(v - b[i]) <= 1e-8);
    requireValue(Array.isArray(data.bases) && data.bases.length === 3 && data.bases.every((b, i) => b.id === layout.bases[i].id
      && closeVector(b.position_m, layout.bases[i].position_world_m) && finite(b.yaw_rad) && Math.abs(b.yaw_rad - layout.bases[i].yaw_rad) <= 1e-8), 'Preview bases do not match its bound design.');
    requireValue(Array.isArray(data.handoffs) && data.handoffs.length === layout.handoffs.length && data.handoffs.every((h, i) => h.support_id === layout.handoffs[i].support_id
      && closeVector(h.position_m, layout.handoffs[i].position_world_m)), 'Preview handoff points do not match its bound design.');
    requireValue(Array.isArray(data.geometry) && data.geometry.length > 0 && data.geometry.length <= 256, 'Invalid preview geometry count.');
    const meshes = new Map(), radii = new Map(); let vertices = 0, triangles = 0;
    for (const mesh of data.geometry) {
      requireValue(typeof mesh.geometry_id === 'string' && mesh.geometry_id.length <= 256 && !meshes.has(mesh.geometry_id), 'Invalid or repeated geometry ID.');
      requireValue(['arm', 'tray'].includes(mesh.owner_kind) && ['mesh', 'box'].includes(mesh.shape), 'Unsupported preview geometry owner or shape.');
      requireValue(Array.isArray(mesh.vertices_local_m) && mesh.vertices_local_m.length >= 3 && Array.isArray(mesh.triangles) && mesh.triangles.length > 0, 'Missing collision mesh.');
      vertices += mesh.vertices_local_m.length; triangles += mesh.triangles.length;
      requireValue(vertices <= 300000 && triangles <= 500000, 'Preview mesh exceeds the viewer size limit.');
      mesh.vertices_local_m.forEach(v => { vector(v, 3, 'Mesh vertex'); requireValue(v.every(x => Math.abs(x) < 10000), 'Mesh coordinate exceeds the viewer range.'); });
      mesh.triangles.forEach(t => requireValue(Array.isArray(t) && t.length === 3 && t.every(i => Number.isInteger(i) && i >= 0 && i < mesh.vertices_local_m.length), 'Invalid mesh triangle index.'));
      meshes.set(mesh.geometry_id, mesh);
      let radius = 0;
      for (const vertex of mesh.vertices_local_m) radius = Math.max(radius, Math.hypot(...vertex));
      radii.set(mesh.geometry_id, radius);
    }
    requireValue(Array.isArray(data.stages) && data.stages.length > 0 && data.stages.length <= (physical ? 100 : 30), 'Invalid preview stage count.');
    const stageIDs = new Set(); let frameCount = 0;
    const boundsLower = [Infinity, Infinity, Infinity], boundsUpper = [-Infinity, -Infinity, -Infinity];
    for (const stage of data.stages) {
      requireValue(typeof stage.stage_id === 'string' && stage.stage_id.length <= 256 && !stageIDs.has(stage.stage_id), 'Invalid or repeated stage ID.');
      stageIDs.add(stage.stage_id);
      requireValue((physical ? stage.kind === 'physical' : ['approach', 'carry', 'kinematic_return'].includes(stage.kind)) && /^arm_[012]$/.test(stage.active_arm_id), 'Invalid stage arm or motion kind.');
      requireValue(finite(stage.duration_s) && stage.duration_s >= 0 && stage.duration_s <= 100000, 'Invalid stage duration.');
      const source = stage.source_trajectory;
      if (!physical) {
      requireValue(source && Array.isArray(source.waypoints) && source.waypoints.length > 0 && source.waypoints.length <= 10000
        && Array.isArray(source.edge_durations_s) && source.edge_durations_s.length === source.waypoints.length - 1, 'Invalid source trajectory.');
      source.waypoints.forEach(q => vector(q, 7, 'Source joint angles'));
      requireValue(source.edge_durations_s.every(x => finite(x) && x > 0), 'Invalid source edge time.');
      requireValue(Math.abs(source.edge_durations_s.reduce((a, b) => a + b, 0) - stage.duration_s) <= 1e-6, 'Stage duration differs from the source trajectory.');
      }
      requireValue(Array.isArray(stage.frames) && stage.frames.length > 0 && stage.frames.length <= 20000, 'Invalid preview frame count.');
      frameCount += stage.frames.length; requireValue(frameCount <= 30000, 'Preview exceeds the frame limit.');
      let previous = -1;
      for (const frame of stage.frames) {
        requireValue(finite(frame.time_s) && frame.time_s >= 0 && frame.time_s <= stage.duration_s + 1e-7 && frame.time_s > previous, 'Frame times must increase within the source stage.');
        previous = frame.time_s; vector(frame.q_rad, 7, 'Frame joint angles'); vector(frame.cup_position_m, 3, 'Cup position');
        if (physical) {
          requireValue(finite(frame.global_sim_time_s) && frame.global_sim_time_s >= 0, 'Invalid recorded simulation time.');
          requireValue(Array.isArray(frame.bead_positions_m) && frame.bead_positions_m.length === data.bead_ids.length, 'Every recorded bead must remain present in every frame.');
          frame.bead_positions_m.forEach(p => {
            vector(p, 3, 'Recorded bead position');
            requireValue(p.every(v => Math.abs(v) < 10000), 'Recorded bead is outside the viewer range.');
            for (let i = 0; i < 3; i++) {
              boundsLower[i] = Math.min(boundsLower[i], p[i] - data.bead_radius_m);
              boundsUpper[i] = Math.max(boundsUpper[i], p[i] + data.bead_radius_m);
            }
          });
        }
        requireValue(Array.isArray(frame.poses) && frame.poses.length === meshes.size, 'Each frame must locate every collision geometry.');
        const ids = new Set();
        for (const pose of frame.poses) {
          requireValue(meshes.has(pose.geometry_id) && !ids.has(pose.geometry_id), 'Unknown or repeated frame geometry.'); ids.add(pose.geometry_id);
          vector(pose.position_m, 3, 'Geometry position'); vector(pose.quaternion_wxyz, 4, 'Geometry quaternion');
          requireValue(pose.position_m.every(x => Math.abs(x) < 10000) && Math.abs(Math.hypot(...pose.quaternion_wxyz) - 1) < 1e-5, 'Geometry pose is out of range or quaternion is not unit length.');
          const radius = radii.get(pose.geometry_id);
          for (let i = 0; i < 3; i++) {
            boundsLower[i] = Math.min(boundsLower[i], pose.position_m[i] - radius);
            boundsUpper[i] = Math.max(boundsUpper[i], pose.position_m[i] + radius);
          }
        }
      }
      requireValue(Math.abs(stage.frames[0].time_s) <= 1e-8 && Math.abs(stage.frames.at(-1).time_s - stage.duration_s) <= 1e-6, 'Preview stage time endpoints differ.');
      if (!physical) requireValue(closeVector(stage.frames[0].q_rad, source.waypoints[0])
        && closeVector(stage.frames.at(-1).q_rad, source.waypoints.at(-1)), 'Preview stage endpoints differ from the saved trajectory.');
    }
    // One cached envelope covers every imported pose. Scrubbing never changes Fit.
    return {data, layout, meshes, frameCount, bounds_m: [boundsLower, boundsUpper]};
  }
  return {finite, vector, validateDesign, validatePreview, transformPoint, requireValue};
});
