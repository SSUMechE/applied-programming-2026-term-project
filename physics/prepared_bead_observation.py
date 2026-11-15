"""Observe disabled native beads without claiming physical retention or rest.

This collector reads the same real native state used by the viewer. The owned
gate separately proves that bead dynamics, integration and contacts were OFF
for every internal step. The ordinary carry audit remains unchanged.
"""
import copy
import hashlib
from pathlib import Path

from physics.deferred_gpu_beads import DeferredPayloadAudit, require


class PreparedPayloadAudit(DeferredPayloadAudit):
    def __init__(self, *args, native_gate, **kwargs):
        require(native_gate.mode == 'OFF', 'Prepared observation requires disabled bead physics')
        self.native_gate = native_gate
        self.off_start = native_gate.count_off
        super().__init__(*args, **kwargs)
        require(len(self.beads) == 3 and self.internal_spill,
                'Prepared observation retains the original three identities and every internal observation')
        path = str(Path(__file__).resolve())
        self.pins[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def finish(self, *, allow_prefix=False):
        record = super().finish(allow_prefix=allow_prefix)
        metadata = self.native_gate.metadata()
        count = metadata['off_native_substeps'] - self.off_start
        flags = metadata['current_guard_flags']
        require(metadata['mode'] == 'OFF' and count == self.captures * 16
                and len(flags) == 5 and not any(flags[1:]),
                'Disabled bead dynamics/contact guard or complete interval coverage failed')
        record.update(schema_version='provided-disabled-bead-observation-v1',
                      bead_dynamics_evaluated=False,
                      prepared_state_geometry_checked=True,
                      material_rest='NOT_APPLICABLE_DISABLED',
                      material_retention='NOT_APPLICABLE_DISABLED',
                      native_gate=dict(schema_version=metadata['schema_version'], mode='OFF',
                          off_internal_steps=count,
                          start_off_internal_step=self.off_start,
                          end_off_internal_step=metadata['off_native_substeps'],
                          guard_flags=copy.deepcopy(flags),
                          native_scene_identity=copy.deepcopy(metadata['native_scene_identity']),
                          bead_identities=copy.deepcopy(metadata['bead_identities']),
                          source_hashes=copy.deepcopy(metadata['source_hashes'])),
                      scope='Actual prepared-state observations only. Bead force/integration/contact were disabled. '
                            'No bead physical-retention or material-rest verdict is claimed.')
        return record
