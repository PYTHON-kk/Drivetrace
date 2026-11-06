"""Privacy policy for everything the assistant returns (NFR-08).

The policy is data minimisation: no role needs location or the identity of a driver to answer the supported
questions, so the assistant never serves them. Signals tagged `privacy:` in schema/vss_mapping.yaml are
refused by the data APIs, and identity keys are removed from event attributes before they leave the process.
"""

from schema.canonical import load_catalogue

DENIED_TAGS = {"location", "identity"}
IDENTITY_ATTR_KEYS = {"driver", "driver_id", "user", "user_id", "vin", "account", "name", "email", "phone"}
ROLES = ("driver", "service", "fleet")

_catalogue = load_catalogue()["signals"]
DENIED_SIGNALS = {p for p, spec in _catalogue.items() if spec.get("privacy") in DENIED_TAGS}


def signal_allowed(path: str) -> bool:
    return path in _catalogue and path not in DENIED_SIGNALS


def scrub_attrs(attrs: dict) -> dict:
    return {k: v for k, v in attrs.items() if k.lower() not in IDENTITY_ATTR_KEYS}


def check_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    return role
