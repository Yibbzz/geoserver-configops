"""What the security kinds do, against the real GeoServer Cloud REST service: people log in from a
JDBC user database and from LDAP, failed logins are slowed down, another role service decides who
is an administrator, and store passwords are encrypted as set. GeoServer's security files are put
back at the end.

LDAP needs the directory tests/e2e/up.sh starts (E2E_LDAP_URL, as GeoServer reaches it); the
JDBC test makes its tables in the e2e database (E2E_DB_HOST_IN_GEOSERVER, as GeoServer reaches it).

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_LDAP_URL=ldap://ldap:389 pytest -m e2e
"""

import os
import time

import pytest
import requests
import yaml

from configops.auth import SECURITY_FILE, reload_security
from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
LDAP = os.environ.get("E2E_LDAP_URL")
DB_IN_GEOSERVER = os.environ.get("E2E_DB_HOST_IN_GEOSERVER", "db")
DB_PASSWORD = os.environ.get("E2E_DB_PASSWORD", "postgres")
MADE = ["usergroup/e2e-jdbc", "auth/e2e-jdbc", "auth/e2e-ldap", "role/e2e-roles", "role/e2e-ldap-roles", "role/e2e-jdbc-roles"]


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    saved = gs.request("GET", SECURITY_FILE).text
    yield gs
    gs.request("PUT", SECURITY_FILE, saved, content_type="application/xml")
    reload_security(gs)
    for folder in MADE:
        gs.request("DELETE", f"resource/security/{folder}", ok=(200, 404))


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": os.environ.get("E2E_DB_HOST", "localhost"),
                       "E2EDB_PORT": os.environ.get("E2E_DB_PORT", "55433"), "E2EDB_DATABASE": "postgres",
                       "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": DB_PASSWORD}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)

    def write(*docs) -> str:
        head = {"kind": "Project", "metadata": {"name": "e2e-logins"}, "spec": {
            "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
            "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": "configops_logins"}}
        (tmp_path / "project.yaml").write_text(yaml.safe_dump_all(
            [{"apiVersion": "configops/v1alpha1", **d} for d in (head, *docs)], sort_keys=False))
        return str(tmp_path)
    return write


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def apply_clean(capsys, folder: str) -> None:
    code, out = configops(capsys, "apply", "-C", folder)
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", folder)
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


def login(user: str, password: str) -> int:
    """GeoServer's answer to a REST call as this person: 401 for a wrong login, 403 or 200 for a right one."""
    return requests.get(f"{URL}/about/version.json", auth=(user, password), timeout=30).status_code


def settle(user: str, password: str, wanted: set[int]) -> int:
    end = time.time() + 30
    while True:
        code = login(user, password)
        if code in wanted or time.time() > end:
            return code
        time.sleep(2)


def security(**fields) -> dict:
    return {"kind": "Settings", "metadata": {"name": "global"}, "spec": {"global": {"security": fields}}}


def test_people_log_in_from_a_jdbc_database(gs, project, capsys):
    service = {"kind": "UserGroupService", "metadata": {"name": "e2e-jdbc"}, "spec": {
        "class": "org.geoserver.security.jdbc.config.JDBCUserGroupServiceConfig",
        "className": "org.geoserver.security.jdbc.JDBCUserGroupService",
        "config": {"driverClassName": "org.postgresql.Driver", "connectURL": f"jdbc:postgresql://{DB_IN_GEOSERVER}:5432/postgres",
                   "userName": "postgres", "password": DB_PASSWORD, "creatingTables": True, "jndi": False,
                   "propertyFileNameDDL": "usersddl.xml", "propertyFileNameDML": "usersdml.xml",
                   "passwordEncoderName": "pbePasswordEncoder", "passwordPolicyName": "default"}}}
    provider = {"kind": "AuthProvider", "metadata": {"name": "e2e-jdbc"}, "spec": {
        "class": "org.geoserver.security.config.UsernamePasswordAuthenticationProviderConfig",
        "className": "org.geoserver.security.auth.UsernamePasswordAuthenticationProvider",
        "config": {"userGroupServiceName": "e2e-jdbc"}}}
    apply_clean(capsys, project(service, provider, security(authProviders=["default", "e2e-jdbc"])))
    gs.request("DELETE", "security/usergroup/service/e2e-jdbc/user/e2e-jdbc-bob", ok=(200, 400, 404))  # from an earlier run
    gs.request("POST", "security/usergroup/service/e2e-jdbc/users",
               {"user": {"userName": "e2e-jdbc-bob", "password": "bob-Jdbc-12", "enabled": True}})
    assert settle("e2e-jdbc-bob", "bob-Jdbc-12", {200, 403}) in (200, 403)  # known, through the JDBC provider
    assert login("e2e-jdbc-bob", "wrong-Password-1") == 401


@pytest.mark.skipif(not LDAP, reason="E2E_LDAP_URL not set: no LDAP directory")
def test_people_log_in_from_ldap(gs, project, capsys):
    provider = {"kind": "AuthProvider", "metadata": {"name": "e2e-ldap"}, "spec": {
        "class": "org.geoserver.security.ldap.LDAPSecurityServiceConfig",
        "className": "org.geoserver.security.ldap.LDAPAuthenticationProvider",
        "config": {"serverURL": f"{LDAP}/dc=example,dc=org", "userDnPattern": "uid={0},ou=people", "useTLS": False,
                   "groupSearchBase": "ou=groups", "groupSearchFilter": "member={0}", "bindBeforeGroupSearch": True}}}
    apply_clean(capsys, project(provider, security(authProviders=["e2e-ldap", "default"])))
    assert settle("e2e-ldap-alice", "alice-Ldap-1", {200, 403}) in (200, 403)
    assert login("e2e-ldap-alice", "wrong-Password-1") == 401
    assert login("admin", PASSWORD) == 200  # GeoServer's own users still log in after LDAP


def test_failed_logins_are_slowed_down(gs, project, capsys):
    # GeoServer lets 127.0.0.1 off by default, which is where a request through a port-forward comes from
    apply_clean(capsys, project(security(bruteForcePrevention={"enabled": True, "minDelaySeconds": 3, "maxDelaySeconds": 3,
                                                               "whitelistedMasks": ["192.0.2.1"]})))
    login("e2e-nobody", "wrong-Password-1")  # the first failure starts the delay for this user
    start = time.time()
    assert login("e2e-nobody", "wrong-Password-1") == 401
    assert time.time() - start >= 2.5


def test_the_active_role_service_decides_who_is_an_administrator(gs, project, capsys):
    """A second XML role service whose roles file makes admin an administrator, and adds a role of its own."""
    roles = ('<?xml version="1.0" encoding="UTF-8"?><roleRegistry version="1.0" xmlns="http://www.geoserver.org/security/roles">'
             '<roleList><role id="ADMIN"/><role id="E2E_ONLY_HERE"/></roleList>'
             '<userList><userRoles username="admin"><roleRef roleID="ADMIN"/></userRoles></userList><groupList/></roleRegistry>')
    gs.request("PUT", "resource/security/role/e2e-roles/roles.xml", roles, content_type="application/xml")
    service = {"kind": "RoleService", "metadata": {"name": "e2e-roles"}, "spec": {
        "class": "org.geoserver.security.xml.XMLRoleServiceConfig", "className": "org.geoserver.security.xml.XMLRoleService",
        "config": {"fileName": "roles.xml", "checkInterval": 10000, "validating": True,
                   "adminRoleName": "ADMIN", "groupAdminRoleName": "ADMIN"}}}
    try:
        apply_clean(capsys, project(service, security(roleService="e2e-roles")))
        roles_now = gs.get("security/roles") or {}
        assert "E2E_ONLY_HERE" in str(roles_now), roles_now
        assert login("admin", PASSWORD) == 200
    finally:
        apply_clean(capsys, project(security(roleService="default")))
    assert "E2E_ONLY_HERE" not in str(gs.get("security/roles"))


def test_store_passwords_are_encrypted_as_set(gs, project, capsys):
    """The setting decides how store passwords are kept: plain text shows as it was given."""
    ws = "configops_passwords"  # not the project's workspace, so the store isn't the files' to manage
    gs.delete_workspace(ws)
    gs.upsert_workspace(ws)
    try:
        for encoder, check in (("plainTextPasswordEncoder", lambda p: p in ("s3cret-Pass", "plain:s3cret-Pass")),
                               ("pbePasswordEncoder", lambda p: p.startswith("crypt1:"))):
            apply_clean(capsys, project(security(passwordEncryption=encoder)))
            gs.request("DELETE", f"workspaces/{ws}/datastores/pw", params={"recurse": "true"}, ok=(200, 404))
            gs.request("POST", f"workspaces/{ws}/datastores", {"dataStore": {"name": "pw", "connectionParameters": {"entry": [
                {"@key": "dbtype", "$": "postgis"}, {"@key": "host", "$": "nowhere"}, {"@key": "port", "$": "5432"}, {"@key": "database", "$": "x"},
                {"@key": "user", "$": "u"}, {"@key": "passwd", "$": "s3cret-Pass"}]}}})
            stored = gs.request("GET", f"resource/workspaces/{ws}/pw/datastore.xml", ok=(200, 404))
            if stored.status_code != 200:  # the catalog is in a database: read the parameter back through REST
                params = gs.get(f"workspaces/{ws}/datastores/pw")["dataStore"]["connectionParameters"]["entry"]
                passwd = next(e["$"] for e in params if e["@key"] == "passwd")
            else:
                passwd = stored.text.split("passwd\">", 1)[1].split("<", 1)[0]
            assert check(passwd), (encoder, passwd)
    finally:
        gs.delete_workspace(ws)


@pytest.mark.skipif(not LDAP, reason="E2E_LDAP_URL not set: no LDAP directory")
def test_roles_from_ldap_groups(gs, project, capsys):
    """An LDAP role service: the directory's groups are its roles. (Asking it for one person's roles through
    REST gave none with every filter tried; GeoServer's own lookup at login isn't reachable from a test.)"""
    service = {"kind": "RoleService", "metadata": {"name": "e2e-ldap-roles"}, "spec": {
        "class": "org.geoserver.security.ldap.LDAPRoleServiceConfig", "className": "org.geoserver.security.ldap.LDAPRoleService",
        "config": {"serverURL": f"{LDAP}/dc=example,dc=org", "groupSearchBase": "ou=groups", "groupSearchFilter": "member={1}",
                   "allGroupsSearchFilter": "cn=*", "userFilter": "uid={0}", "useTLS": False, "bindBeforeGroupSearch": True,
                   "user": "cn=admin,dc=example,dc=org", "password": "admin"}}}
    apply_clean(capsys, project(service))
    roles = gs.get("security/roles/service/e2e-ldap-roles")
    assert "ROLE_MAPPERS" in str(roles), roles


def test_roles_kept_in_a_jdbc_database(gs, project, capsys):
    service = {"kind": "RoleService", "metadata": {"name": "e2e-jdbc-roles"}, "spec": {
        "class": "org.geoserver.security.jdbc.config.JDBCRoleServiceConfig", "className": "org.geoserver.security.jdbc.JDBCRoleService",
        "config": {"driverClassName": "org.postgresql.Driver", "connectURL": f"jdbc:postgresql://{DB_IN_GEOSERVER}:5432/postgres",
                   "userName": "postgres", "password": DB_PASSWORD, "creatingTables": True, "jndi": False,
                   "propertyFileNameDDL": "rolesddl.xml", "propertyFileNameDML": "rolesdml.xml",
                   "adminRoleName": "ADMIN", "groupAdminRoleName": "GROUP_ADMIN"}}}
    apply_clean(capsys, project(service))
    gs.request("POST", "security/roles/service/e2e-jdbc-roles/role/E2E_JDBC_ROLE", ok=(200, 201, 400, 500))  # 4xx/5xx: there from before
    assert "E2E_JDBC_ROLE" in str(gs.get("security/roles/service/e2e-jdbc-roles"))
