r"""Keywords with a language or vocabulary. GeoServer's REST API writes such a keyword as one string,
`word\@language=en\;\@vocabulary=GEMET\;`; the files write `{ keyword, language, vocabulary }`."""

from __future__ import annotations

import re

PART = re.compile(r"\\@(language|vocabulary)=(.*?)\\;")


def to_strings(keywords: list | None) -> list[str] | None:
    """The files' keywords as GeoServer's strings."""
    if keywords is None:
        return None
    out = []
    for k in keywords:
        if isinstance(k, dict):
            text = str(k["keyword"])
            for part in ("language", "vocabulary"):
                if k.get(part):
                    text += f"\\@{part}={k[part]}\\;"
            out.append(text)
        else:
            out.append(str(k))
    return out


def to_entries(strings: list[str]) -> list:
    """GeoServer's keyword strings in the files' form: plain text, or an object when it has a language or vocabulary."""
    out: list = []
    for s in strings:
        parts = dict(PART.findall(s))
        if not parts:
            out.append(s)
            continue
        out.append({"keyword": PART.sub("", s), **{k: parts[k] for k in ("language", "vocabulary") if k in parts}})
    return out
