from app.services.enterprise.auth import enterprise_auth

def test_enterprise_auth():
    assert enterprise_auth.check_permissions("admin", "manage_users") is True
    assert enterprise_auth.check_permissions("viewer", "write") is False
