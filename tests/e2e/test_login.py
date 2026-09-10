"""Login + account contract: every approved school logs in as the parent
account with no 2FA pending."""

import pytest

import edupage_mcp as m

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("sub", ["zssturovamalacky", "iprskola"])
def test_login_contract(sub, sessions):
    from conftest import in_ci

    client = sessions[sub]
    uid = client.get_user_id()
    assert uid and "Rodic" in uid, (
        "expected parent (Rodic) user id" if in_ci() else f"expected parent (Rodic) user id, got {uid!r}"
    )
    role = m._roles[sub]
    assert role == "parent", f"role {role!r} for {sub}"


def test_all_e2e_subdomains_approved(subdomains, sessions):
    assert set(subdomains) <= {"zssturovamalacky", "iprskola"}
    assert set(sessions) == set(subdomains)
    # No cvcmalacky anywhere: it is outside the test scope on purpose.
    assert "cvcmalacky" not in subdomains