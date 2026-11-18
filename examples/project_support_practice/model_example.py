"""Hand-specified classroom model. Not trained or calibrated to the project."""
import json
import numpy as np

# BEGIN load
with open("model_example.json", encoding="utf-8") as stream:
    model = json.load(stream)

if model["schema_version"] != 1:
    raise ValueError("unsupported schema version")
if model["feature_names"] != ["joint_length_rad", "waypoint_count"]:
    raise ValueError("unexpected feature order")
if model["feature_units"] != ["rad", "count"]:
    raise ValueError("unexpected feature units")
weights = np.array(model["weights"], dtype=float)
offset_s = float(model["offset_s"])
if weights.shape != (2,) or not np.isfinite(weights).all():
    raise ValueError("expected two finite weights")
if not np.isfinite(offset_s):
    raise ValueError("expected a finite offset")
# END load

# BEGIN predict
features = np.array([[1.5, 4.0], [1.0, 4.0]], dtype=float)
predicted_time_s = (features * weights).sum(axis=1) + offset_s
print(features.shape)
print(predicted_time_s.shape)
print(predicted_time_s)
# END predict

# BEGIN comparison
# Illustrative reference values, not measured project times.
reference_time_s = np.array([6.5, 8.0], dtype=float)
absolute_error_s = np.abs(predicted_time_s - reference_time_s)
print("reference (s):", reference_time_s)
print("absolute error (s):", absolute_error_s)
# END comparison
