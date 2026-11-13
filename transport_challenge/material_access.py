"""Published material availability, shared by CLI and recipe APIs."""
from .contracts import ContractError


def material_status():
    """Return a fresh availability record. No calendar-based unlocking occurs."""
    return {
        "beads": {"unlocked": True, "description": "Four rigid beads"},
        "process_liquid": {"unlocked": False, "description": "Planned for the next project disclosure"},
        "sand": {"unlocked": False, "description": "Planned for the next project disclosure"},
    }


def require_unlocked_material(material):
    """Accept only a material enabled in this published project revision."""
    statuses = material_status()
    if type(material) is not str or material not in statuses:
        raise ContractError("Unknown project material: " + str(material))
    if not statuses[material]["unlocked"]:
        raise ContractError(material + " is locked in this project revision. Use beads. A later disclosure will announce its availability.")
    return material
