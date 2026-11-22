"""Explain one failed CPU command from its fresh receipts. No student code is run."""
import json
import re


def _geometry_detail(message):
    """The oracle's last rejection is not a count or a diagnosis of every IK try."""
    _, sep, suffix = message.partition(": ")
    if sep and len(suffix) <= 8192:
        try:
            detail = json.loads(suffix)
        except (ValueError, RecursionError):
            return ""
        if isinstance(detail, dict):
            reason = detail.get("reason")
            if reason == "tool_workspace":
                return "the tool was outside its permitted workspace"
            if reason in ("full_model_collision", "tool_reference_obstacle", "unallowed_cpu_contact"):
                return "a collision or disallowed contact was detected"
            if reason == "joint_limit":
                return "a joint was outside its permitted range"
    return ""


def cpu_failure_help(action, summary, *, process=None):
    """Return (stage, reason, next check). The caller owns freshness and display."""
    summary = summary if isinstance(summary, dict) else {}
    process = process if isinstance(process, dict) else {}
    failure = summary.get("failure")
    if not isinstance(failure, dict):
        failure = process.get("failure")
    if not isinstance(failure, dict):
        failure = {}
    message = failure.get("message")
    msg = message if isinstance(message, str) else ""
    typ = failure.get("type")

    if typ == "CpuWorkerDidNotComplete":
        return ("CPU worker", "No usable completed worker result was available.",
                "Check the recorded worker exit code and the end of its log, if available. A missing result alone does not show why it stopped.")
    if typ == "KeyboardInterrupt":
        return ("CPU worker", "The CPU operation was interrupted.",
                "Keep the existing output. Use a fresh output folder if you run the command again.")
    if ("source changed during the operation" in msg or "source changed before public export" in msg
            or msg == "CPU planning source changed"):
        return ("CPU source verification", "The runner detected a changed input or source during this operation.",
                "Finish saving the files before starting again. Do not edit them while the CPU command runs.")
    if msg in ("Difficulty and map differ", "Tier difficulty does not match the selected provided map"):
        return ("CPU task inputs", "The selected difficulty and map do not match.",
                "Check --difficulty and --task against the same map in the README.")
    if msg.startswith("Edit the student design:"):
        return ("CPU design inputs", "Required values are missing from the selected design.",
                "Check the mount and base for each active arm and the required handoff entries in your design JSON.")
    if msg == "Arm placement leaves its selected installation rectangle":
        return ("CPU design inputs", "The chosen arm base is outside its installation rectangle.",
                "Compare the base x_m and y_m with the selected mount region's bounds.")
    if re.match(r"^Relay [XY] must be at (?:least|most) ", msg):
        axis = msg[6].lower()
        return ("CPU design inputs", f"The relay {axis}_m is outside its selected support.",
                f"Compare the handoff's {axis}_m with that support's allowed range in the map.")
    if msg == "Endpoint solve needs1..128attempts and a nonnegative31bit seed":
        return ("CPU endpoint settings", "The endpoint attempt count or seed is outside the supported range.",
                "Use 1 to 128 endpoint attempts and a nonnegative 31-bit stage seed. If unsure, use the README defaults.")

    if action == "merge-arms":
        reason = "The saved arm plans could not be assembled. Merge does not run plan_path or IK."
        check = "Check that the saved arm plans use the same difficulty, shared design and sources."
        if msg in ("Assembly requires each participating arm exactly once", "Do not supply the same arm component twice"):
            reason = "The list does not contain exactly one saved plan for each required arm."
            check = "List each required arm's saved plan exactly once after --arm-plans."
        elif msg.startswith("Arm producers disagree about"):
            reason = "The saved arm plans record different versions of a shared helper."
            check = "Compare the shared helper versions used to produce the arm plans."
        elif msg == "Arm plans use different shared designs, sources, peers or task boundaries":
            reason = "The arm plans were produced for different shared inputs."
        return ("CPU assembly of saved arm paths", reason, check)

    endpoint = re.fullmatch(r"CPU nominal endpoint not found for (arm_[012]__(?:approach|carry))(?:: .*)?", msg)
    if endpoint or msg == "CPU supplied source grasp endpoint not found":
        stage = "CPU endpoint search" + (f" ({endpoint.group(1)})" if endpoint else " (source grasp)")
        reason = "The supplied search found no valid goal pose before plan_path for this stage."
        detail = _geometry_detail(msg)
        if detail:
            reason += " Last recorded rejection: " + detail + "."
        return (stage, reason, "Check whether the arm base can reach the pickup or handoff without colliding at the goal pose.")
    if msg.startswith(("CPU stage start invalid: ", "Independent arm stage start is invalid: ")):
        detail = _geometry_detail(msg)
        return ("CPU stage start", "The start pose was rejected" + (": " + detail if detail else "") + ".",
                "Check the selected base placement and the arm's starting pose before changing the path search.")
    if msg == "Planner must return a nonempty bounded joint path":
        return ("CPU plan_path return check", "No usable path was returned. This message does not distinguish None from an invalid path container.",
                "If plan_path returned None, check where the search stopped. Otherwise return a list or tuple of 1 to 10,000 seven-angle states.")
    if (typ in ("PathNotFound", "NoPathFound")
            or msg in ("Joint planner exhausted its query budget without a returned path",
                       "Student planner found no path within its declared search and query budget")):
        reason = ("The planner exhausted its query budget without returning a path."
                  if msg.startswith("Joint planner exhausted") else "The planner reported that it found no path.")
        return ("CPU path search", reason,
                "Check the search between the supplied start and goal, including which candidate edges the oracle rejects.")
    path_checks = {
        "Joint path must preserve exact requested endpoints": "Use request.start and request.goal as the exact first and last states.",
        "Consecutive repeated configurations are not motion edges": "Check adjacent waypoints for duplicates before returning the path.",
        "Joint path contains an oracle-invalid state": "Check each returned state with oracle.state_valid.",
        "Joint path contains an oracle-invalid whole edge": "Check every consecutive pair with oracle.edge_valid.",
    }
    if msg in path_checks:
        return ("CPU plan_path return check", msg + ".", path_checks[msg])
    # This coordinate check also occurs outside path validation. Only identify
    # it as a returned waypoint when the supplied validator is in this traceback.
    trace = failure.get("traceback")
    if msg == "Expected 7 coordinates" and isinstance(trace, str) and re.search(r", in validate_joint_path\r?\n", trace):
        return ("CPU plan_path return check", "A returned waypoint did not have seven coordinates.",
                "Return one sequence of seven joint angles in radians for each waypoint.")
    if process.get("status") == "CPU_PUBLICATION_REJECTED" and not summary.get("failure"):
        return ("CPU result verification", "The candidate result did not pass the checks required to publish a saved plan.",
                "Check the failure in the CPU process result before using this run for review.")
    return ("CPU planning", "The CPU operation stopped. Its recorded failure does not establish a more specific cause here.",
            "Check the failure type and message in this run's CPU planning result.")
