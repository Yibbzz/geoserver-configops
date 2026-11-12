"""docs/geoserver-fields.yaml: every GeoServer field sorted, each `yes` pointing at the schema,
and the README's coverage badge up to date. tests/geoserver_coverage.py explains the file."""

from tests import geoserver_coverage as coverage


def test_every_field_is_sorted_and_points_at_the_schema():
    assert coverage.problems(coverage.load()) == []


def test_readme_badge_is_up_to_date():
    expected = coverage.badge(coverage.load())
    assert expected in coverage.README.read_text(), \
        "the README's coverage line is out of date: python tests/geoserver_coverage.py report"


def test_java_fields():
    source = '''
    package x;
    /** a class */
    @SuppressWarnings("serial")
    public class FooImpl extends Bar implements Foo {
        public static final String KEY = "a; b { c";
        protected transient Filter filter;
        protected String schemaBaseURL = "http://schemas.opengis.net"; // a comment
        protected Map<String, Serializable> connectionParameters = new HashMap<>();
        @Deprecated private int a, b;
        Boolean encodeMeasures;
        public int hashCode() { final int prime = 31; return prime; }
        class Inner { int hidden; }
    }
    '''
    assert coverage.java_fields(source) == ["schemaBaseURL", "connectionParameters", "a", "b", "encodeMeasures"]


def test_xstream_rules_ignore_comments():
    source = '''
        xs.omitField(impl(StoreInfo.class), "catalog");
        // xs.omitField(StoreInfo.class), "workspace");
        xs.aliasField("abstract", ResourceInfoImpl.class, "_abstract");
    '''
    omitted, aliases = coverage.xstream_rules([source])
    assert omitted == {("StoreInfoImpl", "catalog")}
    assert aliases == {("ResourceInfoImpl", "_abstract"): "abstract"}


def test_schema_paths():
    assert coverage.schema_has("layer.wfs.maxFeatures")
    assert coverage.schema_has("layer.bands.unit")  # through an array
    assert coverage.schema_has("service_wms.geoserver.anything")  # GeoServer's own fields, passed through
    assert not coverage.schema_has("layer.wfs.nope")
    assert not coverage.schema_has("nope")


MANIFEST = {"pages": {"Layers": {
    "ResourceInfoImpl": {"name": {}, "title": {}, "metadata": {}, "metadata.time": {}},
    "FeatureTypeInfoImpl": {"maxFeatures": {}},
    "DimensionInfoImpl": {"enabled": {}},
    "AttributeTypeInfoImpl": {"name": {}}}}}
FT = "/geoserver-cloud/rest/workspaces/uk/datastores/s/featuretypes/roads.json"


def test_fields_a_manifest_doesnt_list():
    body = {"featureType": {
        "name": "roads", "maxFeatures": 0, "newField": True, "store": "http://x/rest/stores.json",
        "metadata": {"entry": [{"@key": "time", "dimensionInfo": {"enabled": True, "newDim": 1}}, {"@key": "brandNew", "$": "1"}]},
        "attributes": {"attribute": [{"name": "id", "binding": "java.lang.Integer"}]}}}
    ev = coverage.Evidence(MANIFEST)
    ev.read(FT, body)
    assert set(ev.unlisted) == {"ResourceInfoImpl/FeatureTypeInfoImpl.newField", "ResourceInfoImpl/FeatureTypeInfoImpl.metadata.brandNew",
                                "DimensionInfoImpl.newDim", "AttributeTypeInfoImpl.binding",
                                "ResourceInfoImpl/FeatureTypeInfoImpl.attributes"}


def test_a_field_is_proved_when_changed_and_shown():
    ev = coverage.Evidence(MANIFEST)
    ev.read(FT, {"featureType": {"name": "roads", "title": "Old", "maxFeatures": 0}})
    # configops changes the title; maxFeatures goes back as it was, which proves nothing
    ev.write("/geoserver-cloud/rest/workspaces/uk/featuretypes/roads", {"featureType": {"title": "New", "maxFeatures": 0}})
    ev.read("/geoserver-cloud/rest/workspaces/uk/featuretypes/roads.json", {"featureType": {"name": "roads", "title": "New", "maxFeatures": "0"}})
    assert ev.proved == {"ResourceInfoImpl.title"}


def test_a_field_shown_differently_is_a_mismatch():
    ev = coverage.Evidence(MANIFEST)
    ev.write(FT, {"featureType": {"name": "roads", "maxFeatures": 5}})
    ev.read(FT, {"featureType": {"name": "roads", "maxFeatures": 0}})
    assert ev.proved == {"ResourceInfoImpl.name"}  # created with this name
    assert "FeatureTypeInfoImpl.maxFeatures" in ev.mismatched


def test_xml_reads_as_json_does():
    xml = '<featureType><name>roads</name><maxFeatures>5</maxFeatures><metadata><entry key="time"><dimensionInfo>' \
          '<enabled>true</enabled></dimensionInfo></entry></metadata></featureType>'
    assert coverage.parse(xml) == {"featureType": {"name": "roads", "maxFeatures": "5", "metadata": {
        "entry": {"@key": "time", "dimensionInfo": {"enabled": "true"}}}}}
    ev = coverage.Evidence(MANIFEST)
    ev.write(FT, xml)
    ev.read(FT, {"featureType": {"name": "roads", "maxFeatures": 5, "metadata": {"entry": [{"@key": "time", "dimensionInfo": {"enabled": True}}]}}})
    assert ev.proved == {"ResourceInfoImpl.name", "FeatureTypeInfoImpl.maxFeatures", "ResourceInfoImpl.metadata.time", "DimensionInfoImpl.enabled"}


def test_rest_paths():
    assert coverage.rest_path("/geoserver-cloud/rest/workspaces/uk.json") == "/workspaces/uk"
    assert coverage.rest_path("/geoserver-cloud/gwc/rest/layers/uk:roads.xml") == "/gwc/rest/layers/uk:roads"
    assert coverage.rest_path("/rest/resource/security/config.xml") == "/resource/security/config.xml"


def test_named_tests():
    manifest = {"pages": {"Security": {"Entries": {"user.password": {"status": "yes", "test": "test_e2e_security.py::test_password"},
                                                   "user.name": {"status": "yes"}}}}}
    assert coverage.named_tests(manifest, {"tests/test_e2e_security.py::test_password"}) == {"Entries.user.password"}
    assert coverage.named_tests(manifest, set()) == set()


def test_an_encrypted_password_stands_for_the_one_written():
    ev = coverage.Evidence({"pages": {"Stores": {"StoreInfoImpl": {"name": {}, "connectionParameters": {}}}}})
    path = "/rest/workspaces/w/datastores/s"
    ev.write(path, {"dataStore": {"name": "s", "connectionParameters": {"entry": [{"@key": "passwd", "$": "secret"}]}}})
    ev.read(path, {"dataStore": {"name": "s", "connectionParameters": {"entry": [{"@key": "passwd", "$": "crypt1:abc"}]}}})
    assert "StoreInfoImpl.connectionParameters" in ev.proved
