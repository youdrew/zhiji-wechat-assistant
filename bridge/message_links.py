"""Display-only metadata for WeChat shared links; never fetch external resources.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from urllib.parse import urlsplit
from xml.etree import ElementTree


LINK_SUBTYPES = frozenset({5, 33, 36})
MAX_XML_LENGTH = 128000
MAX_URL_LENGTH = 8192


def _text(parent, path, maximum):
    element = parent.find(path)
    return "".join(element.itertext()).strip()[:maximum] if element is not None else ""


def _web_url(value):
    """Only an explicit, bounded web address can become an external target."""
    if (not value or len(value) > MAX_URL_LENGTH or "\\" in value or
            any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    try:
        parsed = urlsplit(value)
        # Accessing port also rejects malformed/out-of-range authorities.
        parsed.port
        if (parsed.scheme.lower() in ("http", "https") and parsed.hostname and
                parsed.username is None and parsed.password is None):
            return value
    except ValueError:
        pass
    return None


def link_metadata(local_type, content):
    """Read top-level shared-card fields without interpreting quoted/nested cards.

    The legacy message kind and analysis text stay unchanged. Mini-program cards
    may carry a usable web URL; their app IDs and page paths are never guessed
    into URLs. Missing or unsafe destinations still leave a recognizable card.
    """
    if (not isinstance(local_type, int) or local_type & 0xFFFFFFFF != 49 or
            local_type >> 32 not in ({0} | LINK_SUBTYPES) or
            not isinstance(content, str) or len(content) > MAX_XML_LENGTH or
            "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper()):
        return None
    start = content.find("<")
    if start < 0:
        return None
    try:
        root = ElementTree.fromstring(content[start:].strip())
    except (ElementTree.ParseError, UnicodeError):
        return None
    app = root if root.tag == "appmsg" else root.find("./appmsg")
    if app is None:
        return None
    subtype = _text(app, "./type", 17)
    if subtype:
        if (len(subtype) > 10 or not subtype.isascii() or not subtype.isdecimal() or
                int(subtype) not in LINK_SUBTYPES):
            return None
    elif local_type >> 32 not in LINK_SUBTYPES:
        return None
    title = _text(app, "./title", 512)
    description = _text(app, "./des", 2000)
    source = (_text(app, "./sourcedisplayname", 128) or
              _text(app, "./appinfo/appname", 128) or _text(root, "./appinfo/appname", 128))
    url_element = app.find("./url")
    url = "".join(url_element.itertext()).strip() if url_element is not None else ""
    return {"title": title, "description": description, "url": _web_url(url), "source": source}
