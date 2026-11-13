"""Every kind of authentication filter, provider and user/group and role service configops can
write, each field set to something other than GeoServer's default: apply, then plan has
nothing to do and GeoServer's files hold each value. None is put in a filter chain or made
active, so logins aren't touched (test_e2e_logins.py checks the ones that log people in).

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest pytest -m e2e
"""

import os
import xml.etree.ElementTree as ET

import pytest
import yaml

from configops.auth import SECURITY_FILE, reload_security
from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")

C = "org.geoserver.security.config."
F = "org.geoserver.security.filter."
FILTERS = {
    "e2e-basic": (C + "BasicAuthenticationFilterConfig", F + "GeoServerBasicAuthenticationFilter",
                  {"useRememberMe": True}),
    "e2e-digest": (C + "DigestAuthenticationFilterConfig", F + "GeoServerDigestAuthenticationFilter",
                   {"userGroupServiceName": "default", "nonceValiditySeconds": 123}),
    "e2e-form": (C + "UsernamePasswordAuthenticationFilterConfig", F + "GeoServerUserNamePasswordAuthenticationFilter",
                 {"usernameParameterName": "e2e_user", "passwordParameterName": "e2e_password"}),
    "e2e-header-roles": (C + "RequestHeaderAuthenticationFilterConfig", F + "GeoServerRequestHeaderAuthenticationFilter", {
        "principalHeaderAttribute": "X-E2E-Principal", "rolesHeaderAttribute": "X-E2E-Roles", "roleConverterName": "e2e-converter",
        "roleServiceName": "default", "userGroupServiceName": "default",
        "roleSource": {"class": C + "PreAuthenticatedUserNameFilterConfig$PreAuthenticatedUserNameRoleSource", "value": "Header"}}),
    "e2e-credentials": (C + "CredentialsFromRequestHeaderFilterConfig", F + "GeoServerCredentialsFromRequestHeaderFilter", {
        "userNameHeaderName": "X-E2E-Name", "passwordHeaderName": "X-E2E-Secret", "userNameRegex": "name=(.*)",
        "passwordRegex": "secret=(.*)", "parseAsUriComponents": True}),
    "e2e-roles": (C + "RoleFilterConfig", F + "GeoServerRoleFilter",
                  {"httpResponseHeaderAttrForIncludedRoles": "X-E2E-Included-Roles", "roleConverterName": "e2e-converter"}),
    "e2e-ssl": (C + "SSLFilterConfig", F + "GeoServerSSLFilter", {"sslPort": 8443}),
    "e2e-exception": (C + "ExceptionTranslationFilterConfig", F + "GeoServerExceptionTranslationFilter",
                      {"authenticationFilterName": "e2e-basic", "accessDeniedErrorPage": "/e2e-denied.html"}),
    "e2e-interceptor": (C + "SecurityInterceptorFilterConfig", F + "GeoServerSecurityInterceptorFilter",
                        {"allowIfAllAbstainDecisions": True, "securityMetadataSource": "restFilterDefinitionMap"}),
    "e2e-context": (C + "SecurityContextPersistenceFilterConfig", F + "GeoServerSecurityContextPersistenceFilter",
                    {"allowSessionCreation": True}),
    "e2e-logout": (C + "LogoutFilterConfig", F + "GeoServerLogoutFilter",
                   {"redirectURL": "/e2e-goodbye", "formLogoutChain": "/j_spring_security_logout,/e2e_logout"}),
    "e2e-authkey": ("org.geoserver.security.AuthenticationKeyFilterConfig", "org.geoserver.security.GeoServerAuthenticationKeyFilter", {
        "authKeyMapperName": "propertyMapper", "authKeyParamName": "e2e_key", "userGroupServiceName": "default",
        "mapperParameters": {"entry": ["e2e", "value"]}, "allowMapperKeysAutoSync": True,
        "allowChallengeAnonymousSessions": True}),
}
LDAP = "org.geoserver.security.ldap."
LDAP_SEARCH = {"serverURL": "ldap://e2e-ldap:389/dc=example,dc=org", "groupSearchBase": "ou=e2e-groups",
               "groupFilter": "(member={0})", "groupNameAttribute": "e2eName", "groupMembershipAttribute": "e2eMember",
               "userSearchBase": "ou=e2e-people", "userNameAttribute": "e2eUid", "allUsersSearchFilter": "(objectClass=e2ePerson)",
               "groupMember2UserName": True, "useNestedParentGroups": True, "maxGroupSearchLevel": 4,
               "nestedGroupSearchFilter": "(e2eNested={0})", "adminGroup": "E2E_ADMINS", "groupAdminGroup": "E2E_GROUP_ADMINS",
               "rolePrefix": "E2E_", "convertToUpperCase": False}
OTHERS = [
    ("AuthProvider", "e2e-ldap-provider", LDAP + "LDAPSecurityServiceConfig", LDAP + "LDAPAuthenticationProvider",
     {**LDAP_SEARCH, "userDnPattern": "uid={0},ou=e2e-people", "userGroupServiceName": "default", "userFormat": "uid={0}"}),
    ("AuthProvider", "e2e-jdbc-provider", "org.geoserver.security.jdbc.config.JDBCConnectAuthProviderConfig",
     "org.geoserver.security.jdbc.JDBCConnectAuthProvider",
     {"driverClassName": "org.postgresql.Driver", "connectURL": "jdbc:postgresql://e2e-db:5432/e2e", "userGroupServiceName": "default"}),
    ("UserGroupService", "e2e-ldap-users", LDAP + "LDAPUserGroupServiceConfig", LDAP + "LDAPUserGroupService",
     {**LDAP_SEARCH, "passwordEncoderName": "plainTextPasswordEncoder", "passwordPolicyName": "e2e-objects-policy",
      "populatedAttributes": "mail, telephoneNumber"}),
    ("UserGroupService", "e2e-xml-users", "org.geoserver.security.xml.XMLUserGroupServiceConfig",
     "org.geoserver.security.xml.XMLUserGroupService",
     {"fileName": "users.xml", "checkInterval": 10000, "validating": True, "passwordEncoderName": "plainTextPasswordEncoder",
      "passwordPolicyName": "e2e-objects-policy"}),
    ("RoleService", "e2e-jndi-roles", "org.geoserver.security.jdbc.config.JDBCRoleServiceConfig",
     "org.geoserver.security.jdbc.JDBCRoleService",
     {"jndi": True, "jndiName": "java:comp/env/jdbc/e2e", "adminRoleName": "ADMIN", "groupAdminRoleName": "GROUP_ADMIN"}),
    ("RoleService", "e2e-rest-roles", "org.geoserver.security.GeoServerRestRoleServiceConfig",
     "org.geoserver.security.GeoServerRestRoleService",
     {"baseUrl": "https://e2e-roles.example.org", "rolesRESTEndpoint": "/e2e/roles", "adminRoleRESTEndpoint": "/e2e/admin",
      "usersRESTEndpoint": "/e2e/users", "rolesJSONPath": "$.e2eRoles", "adminRoleJSONPath": "$.e2eAdmin",
      "usersJSONPath": "$.e2eUsers", "adminGroup": "E2E_ADMIN", "groupAdminGroup": "E2E_GROUP_ADMIN",
      "cacheConcurrencyLevel": 2, "cacheMaximumSize": 77, "cacheExpirationTime": 12345, "authApiKey": "e2e-api-key"}),
    ("RoleService", "e2e-ldap-roles-all", LDAP + "LDAPRoleServiceConfig", LDAP + "LDAPRoleService", LDAP_SEARCH),
    ("MasterPasswordProvider", "e2e-url-provider", "org.geoserver.security.password.URLMasterPasswordProviderConfig",
     "org.geoserver.security.password.URLMasterPasswordProvider", {"url": "file:e2e-passwd", "encrypting": False, "readOnly": True, "loginEnabled": True}),
]
FOLDERS = {"AuthFilter": "filter", "AuthProvider": "auth", "UserGroupService": "usergroup", "RoleService": "role",
           "PasswordPolicy": "pwpolicy", "MasterPasswordProvider": "masterpw"}
MADE = [f"filter/{n}" for n in FILTERS] + [f"{FOLDERS[k]}/{n}" for k, n, *_ in OTHERS] + ["pwpolicy/e2e-objects-policy"]


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    saved = gs.request("GET", SECURITY_FILE).text
    for folder in MADE:
        gs.request("DELETE", f"resource/security/{folder}", ok=(200, 404))
    yield gs
    gs.request("PUT", SECURITY_FILE, saved, content_type="application/xml")
    for folder in MADE:
        gs.request("DELETE", f"resource/security/{folder}", ok=(200, 404))
    reload_security(gs)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": os.environ.get("E2E_DB_HOST", "localhost"),
                       "E2EDB_PORT": os.environ.get("E2E_DB_PORT", "55433"), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres",
                       "E2EDB_PASSWORD": os.environ.get("E2E_DB_PASSWORD", "postgres")}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    docs = [{"kind": "Project", "metadata": {"name": "e2e-security-objects"}, "spec": {
        "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
        "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": "configops_objects"}},
        {"kind": "PasswordPolicy", "metadata": {"name": "e2e-objects-policy"},
         "spec": {"uppercase": True, "lowercase": True, "digit": True, "minLength": 9, "maxLength": 40}}]
    docs += [{"kind": "AuthFilter", "metadata": {"name": name}, "spec": {"class": cls, "className": impl, "config": config}}
             for name, (cls, impl, config) in FILTERS.items()]
    docs += [{"kind": kind, "metadata": {"name": name}, "spec": {"class": cls, "className": impl, "config": config}}
             for kind, name, cls, impl, config in OTHERS]
    docs.append({"kind": "Settings", "metadata": {"name": "global"}, "spec": {"global": {"security": {
        "rememberMe": {"className": "org.geoserver.security.rememberme.GeoServerTokenBasedRememberMeServices", "key": "e2e-remember-key"}}}}})
    (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in docs], sort_keys=False))
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def stored(gs, folder: str) -> dict:
    root = ET.fromstring(gs.request("GET", f"resource/security/{folder}/config.xml").text)
    return {child.tag: child for child in root}


def test_every_object_and_field_is_kept(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out

    for name, (_, _, config) in FILTERS.items():
        fields = stored(gs, f"filter/{name}")
        for key, value in config.items():
            if not isinstance(value, dict):
                assert fields[key].text == (str(value).lower() if isinstance(value, bool) else str(value)), (name, key)
    assert [s.text for s in stored(gs, "filter/e2e-authkey")["mapperParameters"].iter("string")] == ["e2e", "value"]
    for kind, name, _, _, config in OTHERS:
        fields = stored(gs, f"{FOLDERS[kind]}/{name}")
        for key, value in config.items():
            assert fields[key].text == (str(value).lower() if isinstance(value, bool) else str(value)), (name, key)
    assert "<key>e2e-remember-key</key>" in gs.request("GET", SECURITY_FILE).text
    policy = stored(gs, "pwpolicy/e2e-objects-policy")
    assert (policy["uppercaseRequired"].text, policy["lowercaseRequired"].text, policy["maxLength"].text) == ("true", "true", "40")
