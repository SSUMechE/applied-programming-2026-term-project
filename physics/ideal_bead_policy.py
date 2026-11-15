"""Pure contracts for ideal bead handling between fully physical carries.

These validators do not operate Genesis, repair a failed carry, or authenticate
an arbitrary caller. The producer source, native artifacts and source bindings
remain authenticated by the existing full/practice review gates.
"""
from __future__ import annotations

import copy
from functools import wraps
import math
import re

from transport_challenge.contracts import ContractError, digest

SCHEMA = 'provided-carry-only-bead-lifecycle-v1'
DT = .0025
SUBSTEPS = 16


def require(condition, message):
    if not condition:
        raise ContractError(message)


def policy_contract():
    from leaderboard.v3 import ideal_bead_policy_contract
    return ideal_bead_policy_contract()


def _sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _vector(value, size):
    return type(value) is list and len(value) == size and all(
        type(v) in (int, float) and math.isfinite(v) for v in value)


def _identities(identities):
    require(type(identities) is list and len(identities) == 3, 'Exactly three original bead identities required')
    require(all(type(v) is dict and type(v.get('id')) is str and v['id']
                and type(v.get('entity_idx')) is int and v['entity_idx'] >= 0
                and type(v.get('mass_kg')) in (int, float) and math.isfinite(v['mass_kg']) and v['mass_kg'] > 0
                and type(v.get('radius_m')) in (int, float) and math.isfinite(v['radius_m']) and v['radius_m'] > 0
                for v in identities), 'Invalid original bead identity, mass or radius')
    require(len({v['id'] for v in identities}) == len({v['entity_idx'] for v in identities}) == 3,
            'Bead logical/native identities must be distinct')


def _positions(value):
    require(type(value) is list and len(value) == 3 and all(_vector(v, 3) for v in value),
            'The fixed three-bead local rest table is required')


def canonical_prepared_positions(world, identities):
    """Read the original ordered body-frame recipe without a new placement rule."""
    _identities(identities)
    contents = world.get('initial_contents') if type(world) is dict else None
    require(type(contents) is list and len(contents) == 3, 'Canonical three-bead recipe missing')
    for item, identity in zip(contents, identities):
        require(all(item.get(k) == identity[k] for k in ('id', 'mass_kg', 'radius_m'))
                and item.get('shape') == 'sphere' and item.get('frame') == 'body'
                and item.get('position_units') == 'm' and item.get('mass_units') == 'kg',
                'Prepared recipe differs from original ordered bead supply')
    positions = [copy.deepcopy(item.get('center_body_m')) for item in contents]
    _positions(positions)
    return positions


def _clock(value):
    require(_vector(value, 4) and type(value[0]) is int and type(value[2]) is int and type(value[3]) is int
            and value[0] >= 0 and value[2] == value[0] * SUBSTEPS and value[3] == 0
            and abs(value[1] - value[0] * DT) <= 1e-9,
            'Bead transition must occur at a complete native outer-step boundary')


def validate_native_transition(receipt, *, identities, expected_event=None):
    """Check the native gate's zero-time, bead-only transition receipt."""
    _identities(identities)
    require(type(receipt) is dict and receipt.get('event') in ('pause', 'reset', 'resume'), 'Unknown bead transition')
    kind = receipt['event']
    require(expected_event is None or kind == expected_event, 'Wrong native bead transition')
    require(type(receipt.get('physical_steps')) is int and receipt['physical_steps'] == 0,
            'Bead handling must consume zero physical steps')
    before, after = receipt.get('before'), receipt.get('after')
    require(type(before) is dict and type(after) is dict, 'Missing actual before/after bead state')
    _clock(receipt.get('native_clock'))
    for state in (before, after):
        require(state.get('native_clock') == receipt['native_clock'], 'Transition changed native clock')
        require(type(state.get('scene_identity')) is list and len(state['scene_identity']) == 5
                and all(type(v) is int and v > 0 for v in state['scene_identity']), 'Missing persistent native scene')
        require(state.get('bead_identities') == identities, 'Transition replaced original bead identities')
        require(type(state.get('bead_qpos')) is list and len(state['bead_qpos']) == 3
                and all(_vector(v, 7) and abs(sum(x*x for x in v[3:]) - 1) <= 1e-5 for v in state['bead_qpos'])
                and type(state.get('bead_qvel')) is list and len(state['bead_qvel']) == 3
                and all(_vector(v, 6) for v in state['bead_qvel']), 'Invalid actual bead q7/qd6')
        for key in ('non_bead_qpos_sha256', 'non_bead_qvel_sha256', 'non_bead_constraint_cache_sha256',
                    'collision_pair_sha256', 'non_bead_collision_pair_sha256'):
            require(_sha(state.get(key)), 'Missing actual native state hash: ' + key)
    for key in ('scene_identity', 'native_clock', 'non_bead_qpos_sha256', 'non_bead_qvel_sha256',
                'non_bead_constraint_cache_sha256', 'non_bead_collision_pair_sha256', 'bead_identities'):
        require(before[key] == after[key], 'Bead transition changed cup, robots, scene, clock or other collisions: ' + key)
    modes = {'reset': ('OFF', 'OFF'), 'resume': ('OFF', 'CARRY'), 'pause': ('CARRY', 'OFF')}
    require((before.get('mode'), after.get('mode')) == modes[kind], 'Illegal bead dynamics transition')
    pins = receipt.get('source_hashes')
    require(type(pins) is dict and pins and all(type(k) is str and k and _sha(v) for k, v in pins.items()),
            'Missing native bead gate source bindings')
    if kind == 'reset':
        _positions(receipt.get('requested_body_local_positions_m'))
        _positions(receipt.get('requested_world_positions_m'))
        require(receipt.get('zero_velocity') is True and all(all(v == 0 for v in row) for row in after['bead_qvel']),
                'Bead reset did not zero every linear/angular velocity')
        require([q[:3] for q in after['bead_qpos']] == receipt['requested_world_positions_m'],
                'Reset native bead positions differ from requested positions')
        require(before['collision_pair_sha256'] == after['collision_pair_sha256'], 'Reset changed collision configuration')
    else:
        require(before['bead_qpos'] == after['bead_qpos'] and before['bead_qvel'] == after['bead_qvel'],
                'Pause/resume changed physical bead coordinates or velocities')
    return receipt


def _validate_endpoint_readbacks(readbacks, *, phase, endpoint, identities):
    """Bind each same-clock readback in its original numeric representation."""
    require(type(readbacks) is dict and set(readbacks) == {
        'schema_version', 'native_scene_step', 'raw_history', 'raw_post_ordinal',
        'raw_post_payload', 'native_snapshot'}
        and readbacks.get('schema_version') == 'same-clock-carry-endpoint-readbacks-v1',
        'Missing exact same-clock carry endpoint readbacks')
    require(type(readbacks['native_scene_step']) is int
            and readbacks['native_scene_step'] == phase['end_native_step'] == endpoint['native_scene_step']
            and type(readbacks['raw_post_ordinal']) is int
            and readbacks['raw_post_ordinal'] == phase['actual_intervals'] - 1,
            'Carry readbacks do not identify the final actual POST')
    artifact = readbacks['raw_history']
    require(type(artifact) is dict and set(artifact) == {'file', 'sha256'}
            and type(artifact['file']) is str and artifact['file'] and _sha(artifact['sha256'])
            and artifact == phase.get('raw_history'), 'Carry readbacks use a different raw history artifact')
    raw = readbacks['raw_post_payload']
    require(_vector(raw, 38), 'Carry endpoint needs the complete finite native payload row')
    native = readbacks['native_snapshot']
    require(type(native) is dict and native.get('mode') == 'CARRY', 'Carry endpoint native snapshot is not active')
    _clock(native.get('native_clock'))
    require(native['native_clock'][0] == readbacks['native_scene_step']
            and native['native_clock'][1] == endpoint['simulation_s'], 'Carry endpoint native clock differs')
    scene = native.get('scene_identity')
    owner = phase.get('native_scene_identity')
    require(type(scene) is list and len(scene) == 5 and all(type(v) is int and v > 0 for v in scene)
            and type(owner) is list and len(owner) == 4 and all(type(v) is int and v > 0 for v in owner)
            and scene[:3] == [owner[i] for i in (0, 2, 3)], 'Carry readbacks belong to a different native scene')
    require(native.get('bead_identities') == identities, 'Carry native snapshot replaced original bead identities')
    require(type(native.get('bead_qpos')) is list and len(native['bead_qpos']) == 3
            and all(_vector(v, 7) and abs(sum(x*x for x in v[3:]) - 1) <= 1e-5 for v in native['bead_qpos'])
            and type(native.get('bead_qvel')) is list and len(native['bead_qvel']) == 3
            and all(_vector(v, 6) for v in native['bead_qvel']), 'Invalid carry native bead q7/qd6')
    for key in ('non_bead_qpos_sha256', 'non_bead_qvel_sha256', 'non_bead_constraint_cache_sha256',
                'collision_pair_sha256', 'non_bead_collision_pair_sha256'):
        require(_sha(native.get(key)), 'Missing carry native state hash: ' + key)
    masses = phase['payload'].get('expected_native_masses_kg')
    require(_vector(masses, 3) and all(v > 0 for v in masses), 'Carry native mass binding is missing')
    for i, (material, qpos, mass) in enumerate(zip(endpoint['materials'], native['bead_qpos'], masses)):
        sample = raw[14 + 8*i:22 + 8*i]
        require(material['position_world_m'] == sample[:3] == qpos[:3]
                and sample[6] == 1 and sample[7] == mass,
                'Original bead position, retention or native mass differs at the carry endpoint')
    # Public link-origin, f64 collector and free-joint velocities are distinct
    # readbacks. Each remains finite and is bound to its own original receipt.
    # The owning result validator compares raw_post_payload to the actual NPZ.
    return readbacks


def seal_carry(*, arm_id, stage_id, phases, endpoint, endpoint_readbacks, identities, failures=()):
    """Seal successful full-rate carry evidence before any bead reset."""
    _identities(identities)
    require(not failures, 'A failed carry cannot be sealed or repaired by reset')
    require(arm_id in ('arm_0', 'arm_1', 'arm_2') and stage_id == arm_id + '__carry', 'Wrong carry stage identity')
    require(type(phases) is list and phases, 'Missing complete physical carry phases')
    cursor = phases[0]['start_native_step']
    begin = cursor
    identity = phases[0]['native_scene_identity']
    for phase in phases:
        count = phase.get('actual_intervals')
        require(type(count) is int and count > 0 and phase.get('phase') == 'carry'
                and phase.get('stage_id') == stage_id and phase.get('active_arm_id') == arm_id
                and phase.get('actual_holder_arm_id') == arm_id and phase.get('native_scene_identity') == identity
                and phase.get('start_native_step') == cursor and phase.get('end_native_step') == cursor + count,
                'Incomplete or noncontiguous actual carry phase')
        require(all(type(phase.get(k)) is dict and phase[k].get('passed') is True
                    for k in ('control', 'payload', 'contacts', 'support', 'handling_checkpoint')),
                'Carry has failed control, payload, contact, support or handling checks')
        payload = phase['payload']
        require(payload.get('schema_version') == 'deferred-native-payload-post-audit-v1'
                and payload.get('original_bead_ids') == identities and payload.get('bead_count') == 3
                and payload.get('actual_POST_samples') == payload.get('compact_host_polls') == count
                and payload.get('actual_internal_POST_samples') == count * SUBSTEPS
                and payload.get('requested_POST_samples') == count and payload.get('observed_prefix_only') is False
                and payload.get('immediate_failure_halt_preserved') is True
                and payload.get('source_bindings_unchanged') is True
                and payload.get('first_escape') is None and payload.get('escape_witnesses') == []
                and payload.get('first_failed_step') is None and payload.get('first_failed_global_substep') is None
                and type(payload.get('flags')) is dict and payload['flags']
                and all(v is False for v in payload['flags'].values()), 'Carry payload coverage or sticky failure evidence invalid')
        require(phase['contacts'].get('PRE_samples') == phase['contacts'].get('POST_samples') == count * SUBSTEPS
                and phase['contacts'].get('category_counts', {}).get('UNALLOWED_CONTACT') == 0,
                'Carry contact coverage or collision evidence invalid')
        require(phase['control'].get('arm_order') == ['arm_0', 'arm_1', 'arm_2']
                and phase['control'].get('controlled_dof_count') == 27,
                'Carry removed a robot or peer controls')
        cursor += count
    require(type(endpoint) is dict and endpoint.get('native_scene_step') == cursor
            and abs(endpoint.get('simulation_s', -1) - cursor * DT) <= 1e-9,
            'Carry endpoint is stale or not the final actual POST')
    materials = endpoint.get('materials')
    require(type(materials) is list and len(materials) == 3, 'Final carry did not retain all original beads')
    for actual, expected in zip(materials, identities):
        require(all(actual.get(k) == expected[k] for k in ('id', 'entity_idx', 'mass_kg'))
                and actual.get('inside_carrier') is True, 'Original bead identity/mass or pre-reset retention failed')
        for key in ('position_world_m', 'velocity_world_m_s'):
            require(_vector(actual.get(key), 3), 'Missing/nonfinite carry endpoint material')
    _validate_endpoint_readbacks(endpoint_readbacks, phase=phases[-1], endpoint=endpoint, identities=identities)
    result = dict(schema_version='provided-physical-bead-carry-seal-v1', passed=True, arm_id=arm_id,
                  stage_id=stage_id, start_native_step=begin, end_native_step=cursor,
                  native_scene_identity=copy.deepcopy(identity), original_bead_ids=copy.deepcopy(identities),
                  phases_sha256=digest(phases), endpoint=copy.deepcopy(endpoint),
                  endpoint_sha256=digest(endpoint), endpoint_readbacks=copy.deepcopy(endpoint_readbacks),
                  endpoint_readbacks_sha256=digest(endpoint_readbacks), original_three_retained_before_reset=True,
                  full_internal_coverage=True, failure_latch_clear=True)
    result['seal_sha256'] = digest(result)
    return result


def _fail_closed(method):
    @wraps(method)
    def checked(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            if self.state != 'FAILED':
                self.note_failure(method.__name__ + ': ' + str(exc))
            raise ContractError(str(exc)) from exc
    return checked


class BeadLifecycle:
    """A fail-closed producer ledger with replayable, source-bound inputs."""

    def __init__(self, identities, body_local_positions, required_arm_ids, *, support_route, scope='full_mission'):
        _identities(identities)
        _positions(body_local_positions)
        require(scope in ('full_mission', 'arm_practice'), 'Unknown bead lifecycle scope')
        require(type(required_arm_ids) is list and required_arm_ids and len(set(required_arm_ids)) == len(required_arm_ids)
                and all(a in ('arm_0', 'arm_1', 'arm_2') for a in required_arm_ids), 'Invalid participating arms')
        require(scope != 'arm_practice' or len(required_arm_ids) == 1, 'Practice is exactly one local leg')
        require(scope != 'full_mission' or required_arm_ids == ['arm_' + str(i) for i in range(len(required_arm_ids))],
                'Full mission must retain the tier arm order')
        require(type(support_route) is list and len(support_route) == len(required_arm_ids) + 1
                and all(type(v) is str and v for v in support_route) and len(set(support_route)) == len(support_route),
                'Immutable source/destination support route required')
        self.identities = copy.deepcopy(identities)
        self.positions = copy.deepcopy(body_local_positions)
        self.arms = list(required_arm_ids)
        self.route = list(support_route)
        self.scope = scope
        self.state = 'NEW'
        self.cursor = 0
        self.events = []
        self.failures = []
        self.seals = []
        self._last_native = None
        self._carry_pairs = None
        self._off_pairs = None
        self._native_sources = None

    def _clear(self):
        require(not self.failures and self.state != 'FAILED', 'Bead lifecycle failure is sticky')

    def _append(self, operation, inputs, output=None):
        event = dict(ordinal=len(self.events), previous_event_sha256=digest(self.events[-1]) if self.events else None,
                     operation=operation, inputs=copy.deepcopy(inputs), output=copy.deepcopy(output))
        self.events.append(event)
        return copy.deepcopy(event)

    def note_failure(self, reason):
        require(type(reason) is str and reason, 'Failure reason required')
        self.failures.append(reason)
        self.state = 'FAILED'
        return self._append('failure', dict(reason=reason))

    def _native(self, transition, expected):
        validate_native_transition(transition, identities=self.identities, expected_event=expected)
        before, after = transition['before'], transition['after']
        if self._last_native is not None:
            previous = self._last_native
            require(before['scene_identity'] == previous['scene_identity']
                    and before['native_clock'][0] >= previous['native_clock'][0], 'Native bead scene/clock lineage changed')
            if previous['mode'] == 'OFF':
                require(before['mode'] == 'OFF' and before['bead_qpos'] == previous['bead_qpos']
                        and before['bead_qvel'] == previous['bead_qvel'], 'Disabled beads moved outside an authorized reset')
            require(transition['source_hashes'] == self._native_sources, 'Native bead gate source bindings changed')
        for state in (before, after):
            expected_pairs = self._off_pairs if state['mode'] == 'OFF' else self._carry_pairs
            require(expected_pairs is None or state['collision_pair_sha256'] == expected_pairs,
                    'Native bead collision matrix was not restored exactly')
        self._last_native = copy.deepcopy(after)
        self._native_sources = copy.deepcopy(transition['source_hashes'])

    @_fail_closed
    def check_reset_allowed(self, reason, *, arm_id, support_id, stage_id=None):
        """Call before native mutation. record_reset repeats this authorization."""
        self._clear()
        require(reason in ('INITIAL', 'PRE_PICK', 'PLACE'), 'Unknown authorized reset boundary')
        require(self.cursor < len(self.arms) and arm_id == self.arms[self.cursor], 'Reset arm order differs')
        expected_support = self.route[self.cursor + (reason == 'PLACE')]
        require(support_id == expected_support, 'Reset support differs from the immutable leg route')
        if reason == 'INITIAL':
            require(self.state == 'NEW' and stage_id is None, 'INITIAL reset is fresh clock-zero only')
        elif reason == 'PRE_PICK':
            require(self.state == 'OFF' and stage_id == arm_id + '__approach', 'PRE_PICK reset is not a supported next pickup')
        else:
            require(self.state == 'PAUSED' and stage_id == arm_id + '__carry', 'PLACE reset requires a sealed and paused carry')
        return True

    @_fail_closed
    def record_reset(self, reason, native_transition, *, arm_id, support_id, support_evidence, stage_id=None, initial_pause=None):
        self.check_reset_allowed(reason, arm_id=arm_id, support_id=support_id, stage_id=stage_id)
        require(type(support_evidence) is dict
                and support_evidence.get('passed') is True and support_evidence.get('support_id') == support_id,
                'Reset lacks actual named support evidence')
        require(support_evidence.get('native_scene_step')==native_transition['native_clock'][0]
                and support_evidence.get('simulation_s')==native_transition['native_clock'][1]
                and support_evidence.get('material_rest_assessed') is False
                and support_evidence.get('evidence_kind')==('fresh_source_geometry' if reason=='INITIAL' else 'native_endpoint_support')
                and support_evidence.get('support_contact_evaluated') is (reason!='INITIAL'),
                'Reset support proof is stale or claims nonexistent material physics')
        if reason == 'INITIAL':
            validate_native_transition(initial_pause, identities=self.identities, expected_event='pause')
            require(initial_pause['native_clock'] == [0, 0., 0, 0]
                    and initial_pause['after'] == native_transition['before'], 'Missing original initial bead pause lineage')
            self._carry_pairs = initial_pause['before']['collision_pair_sha256']
            self._off_pairs = initial_pause['after']['collision_pair_sha256']
            self._native(initial_pause, 'pause')
        else:
            require(initial_pause is None, 'Only INITIAL may establish the native bead gate')
        self._native(native_transition, 'reset')
        require(native_transition['requested_body_local_positions_m'] == self.positions,
                'Reset changed the fixed local three-bead recipe')
        if reason == 'INITIAL':
            require(native_transition['native_clock'] == [0, 0., 0, 0], 'INITIAL reset is fresh clock-zero only')
            self.state = 'OFF'
        elif reason == 'PRE_PICK':
            self.state = 'PREPARED'
        else:
            require(native_transition['native_clock'][0] == self.seals[-1]['end_native_step'],
                    'PLACE reset must immediately follow a sealed carry at its native endpoint')
            self.cursor += 1
            self.state = 'COMPLETE' if self.cursor == len(self.arms) else 'OFF'
        return self._append('reset', dict(reason=reason, native_transition=native_transition, arm_id=arm_id,
                            support_id=support_id, support_evidence=support_evidence, stage_id=stage_id, initial_pause=initial_pause))

    @_fail_closed
    def begin_carry(self, arm_id, stage_id, native_resume):
        self._clear()
        require(self.state == 'PREPARED' and arm_id == self.arms[self.cursor] and stage_id == arm_id + '__carry',
                'Carry must follow its supported pickup preparation')
        self._native(native_resume, 'resume')
        require(native_resume['native_clock'] == self.events[-1]['inputs']['native_transition']['native_clock'],
                'A physical step occurred between bead preparation and carry activation')
        self.state = 'CARRY'
        return self._append('begin_carry', dict(arm_id=arm_id, stage_id=stage_id, native_resume=native_resume))

    @_fail_closed
    def finish_carry(self, phases, endpoint, endpoint_readbacks):
        self._clear()
        require(self.state == 'CARRY', 'No active carry to seal')
        begin = self.events[-1]['inputs']
        try:
            seal = seal_carry(arm_id=begin['arm_id'], stage_id=begin['stage_id'], phases=phases,
                              endpoint=endpoint, endpoint_readbacks=endpoint_readbacks,
                              identities=self.identities, failures=self.failures)
            require(seal['start_native_step'] == begin['native_resume']['native_clock'][0], 'Carry began at the wrong native clock')
            native_identity = begin['native_resume']['after']['scene_identity']
            require([seal['native_scene_identity'][i] for i in (0, 2, 3)] == native_identity[:3]
                    and endpoint_readbacks['native_snapshot']['scene_identity'] == native_identity,
                    'Carry phase and bead gate belong to different native scenes')
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            self.note_failure('Carry could not be sealed: ' + str(exc))
            raise ContractError(self.failures[-1]) from exc
        self.seals.append(seal)
        self.state = 'SEALED'
        self._append('finish_carry', dict(phases=phases, endpoint=endpoint, endpoint_readbacks=endpoint_readbacks), seal)
        return copy.deepcopy(seal)

    @_fail_closed
    def pause(self, native_pause):
        self._clear()
        require(self.state == 'SEALED', 'Beads can be paused only after a successful carry seal')
        self._native(native_pause, 'pause')
        require(native_pause['native_clock'][0] == self.seals[-1]['end_native_step'], 'Pause changed the carry endpoint')
        require(native_pause['before'] == self.seals[-1]['endpoint_readbacks']['native_snapshot'],
                'Native pause no longer observes the exact sealed native state and constraint cache')
        self.state = 'PAUSED'
        return self._append('pause', dict(native_pause=native_pause))

    def record(self):
        return copy.deepcopy(dict(schema_version=SCHEMA, policy=policy_contract(), scope=self.scope,
                                  identities=self.identities, body_local_positions_m=self.positions,
                                  required_arm_ids=self.arms, support_route=self.route, state=self.state, completed_carries=self.cursor,
                                  failures=self.failures, events=self.events, carry_seals=self.seals,
                                  full_mission_claimed=False, ranking_eligible=False))


def validate_lifecycle(record, *, identities, body_local_positions, required_arm_ids,
                       support_route, scope='full_mission', require_complete=True, actual_phases=None):
    """Recompute all transitions and carry seals without trusting summaries."""
    require(type(record) is dict and record.get('schema_version') == SCHEMA, 'Wrong bead lifecycle schema')
    ledger = BeadLifecycle(identities, body_local_positions, required_arm_ids, support_route=support_route, scope=scope)
    require(type(record.get('events')) is list, 'Missing bead lifecycle events')
    methods = {'reset': ledger.record_reset, 'begin_carry': ledger.begin_carry,
               'finish_carry': ledger.finish_carry, 'pause': ledger.pause, 'failure': ledger.note_failure}
    try:
        for event in record['events']:
            require(type(event) is dict and event.get('operation') in methods and type(event.get('inputs')) is dict,
                    'Unknown bead lifecycle event')
            methods[event['operation']](**event['inputs'])
            require(ledger.events[-1] == event, 'Bead lifecycle receipt chain or derived carry seal differs')
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError('Malformed bead lifecycle event') from exc
    require(ledger.record() == record, 'Bead lifecycle summary differs from its authenticated transitions')
    if require_complete:
        require(ledger.state == 'COMPLETE' and not ledger.failures and ledger.cursor == len(required_arm_ids),
                'Bead lifecycle is incomplete or failed')
        require(type(actual_phases) is list and actual_phases, 'Completion requires the owning mission actual phase records')
    if actual_phases is not None:
        require(type(actual_phases) is list, 'Actual mission phase records must be a list')
        carried = [p for p in actual_phases if p.get('phase') == 'carry']
        sealed = [p for e in ledger.events if e['operation'] == 'finish_carry' for p in e['inputs']['phases']]
        if require_complete:
            require(carried == sealed, 'Bead seals do not bind the exact owning mission carry phases')
        else:
            require(carried[:len(sealed)] == sealed, 'Sealed carry prefix differs from actual mission phases')
    return record
