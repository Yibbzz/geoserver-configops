from configops.keywords import to_entries, to_strings


def test_keywords_with_a_language_or_vocabulary_round_trip():
    keywords = ["maps", {"keyword": "afonydd", "language": "cy"}, {"keyword": "rivers", "language": "en", "vocabulary": "GEMET"}]
    strings = to_strings(keywords)
    assert strings == ["maps", "afonydd\\@language=cy\\;", "rivers\\@language=en\\;\\@vocabulary=GEMET\\;"]
    assert to_entries(strings) == keywords
    assert to_strings(None) is None
