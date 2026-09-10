"""One-way fingerprinting of the parent's linked children.

Fingerprints let the suite detect drift in EduPage child parsing on a public CI
runner WITHOUT committing or printing any personal details. Only a sha256 hex
digest of `person_id|name` lives in the repo; the raw identities live only in
the gitignored tests/e2e/.local.e2e.json on the owner's machine.

A fingerprint change means either EduPage changed how children are rendered
(handled = layout drift) or the account's children genuinely changed. In both
cases the test fails with a message that reveals nothing personal.
"""

import hashlib
import json
import os

FINGERPRINT_PATH = os.path.join(os.path.dirname(__file__), "expected_children.fingerprint.json")


def children_fingerprint(children):
    """sha256 over the canonical per-child lines `person_id|name`, sorted."""
    lines = sorted(f"{c.person_id}|{c.name}" for c in children)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def expected_for(subdomain):
    """Fingerprint expected for a subdomain, or None if unrecorded."""
    with open(FINGERPRINT_PATH, encoding="utf-8") as f:
        return json.load(f).get("children", {}).get(subdomain)


def save_expected(mapping):
    """Write a new fingerprint file (owner regenerates after an intentional
    change; never runs in CI)."""
    data = {"version": 1, "algo": "sha256", "children": {k: v for k, v in sorted(mapping.items())}}
    with open(FINGERPRINT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")