"""Hardened LandXML parsing.

QGIS Processing algorithms accept a *file path chosen by the QGIS user*, but
that file can originate anywhere -- a shared drive, an email attachment, a
download -- so it must be treated as untrusted input, not as something the
plugin author controls. Plain ``xml.etree.ElementTree`` inherits CPython's
``expat``-based parser, which by default still expands general entities
declared in a document's own ``<!DOCTYPE ...>`` internal subset. A file that
declares a handful of nested entities (the classic "billion laughs" pattern)
expands to gigabytes of text during parsing and can hang or crash QGIS
before any of this plugin's own code ever runs -- a real denial-of-service
risk for a plugin whose entire job is opening arbitrary LandXML files. The
same underlying mechanism (external entity references) can also be used to
read local files or trigger network requests (XXE).

This module is a drop-in, dependency-free replacement for the two calls this
plugin needs (``ET.parse(path).getroot()`` and ``ET.fromstring(text)``). It
deliberately does not pull in the third-party ``defusedxml`` package, which
is not part of the Python environment QGIS ships on any platform and would
make the plugin fail to import on a stock install; nor does it rely on
``xml.etree.ElementTree.XMLParser``'s undocumented ``.parser`` attribute,
which is only reachable when the pure-Python fallback implementation is
active (defusedxml itself works around this with a ``sys.modules`` trick to
force that fallback) -- the normal, C-accelerated ``XMLParser`` that ships in
every CPython build does not expose it, so relying on it would silently stop
working the moment the C accelerator is present, i.e. always, on every real
QGIS install.

Instead this module drives ``xml.parsers.expat`` directly -- the same public,
documented, stable stdlib API that ``xml.etree.ElementTree`` itself is built
on (see ``xml.etree.ElementTree.XMLParser`` in the standard library source)
-- wiring its callbacks into an ``xml.etree.ElementTree.TreeBuilder`` by
hand, in the same way ElementTree's own pure-Python implementation does, and
registering handlers that reject any DOCTYPE declaration, entity
declaration, or external entity reference before the parser acts on one.

No real-world LandXML export (Civil 3D or otherwise) declares a DOCTYPE or
custom entities -- the schema has no legitimate use for either -- so forbidding
them entirely, rather than merely capping expansion size, is safe and simply
rejects a malformed/malicious file with a clear error instead of acting on it.
"""
from __future__ import annotations

import xml.parsers.expat as expat
from xml.etree.ElementTree import ElementTree, ParseError, TreeBuilder


class UnsafeXmlError(ValueError):
    """Raised when a LandXML file contains a DOCTYPE, entity declaration, or
    external entity reference. Real LandXML exports never need any of these;
    a file that does is either corrupt or deliberately crafted to attack the
    parser, and is refused rather than parsed."""


def _forbid_doctype(name, sysid, pubid, has_internal_subset):
    raise UnsafeXmlError(
        f"Refusing to parse: the file declares a DOCTYPE ('{name}'), which is "
        "never present in a genuine LandXML export and can be used to smuggle "
        "entity-expansion or external-reference attacks. Re-export the file "
        "without a DOCTYPE, or verify its source."
    )


def _forbid_entity(name, is_parameter_entity, value, base, sysid, pubid, notation_name):
    raise UnsafeXmlError(
        f"Refusing to parse: the file declares an XML entity ('{name}'), which "
        "is never present in a genuine LandXML export and can be used for "
        "entity-expansion denial-of-service attacks."
    )


def _forbid_unparsed_entity(name, base, sysid, pubid, notation_name):
    _forbid_entity(name, False, None, base, sysid, pubid, notation_name)


def _forbid_external_ref(context, base, sysid, pubid):
    raise UnsafeXmlError(
        "Refusing to parse: the file references an external entity "
        f"({sysid or pubid!r}), which is never present in a genuine LandXML "
        "export and can be used to read local files or trigger network "
        "requests (XXE)."
    )


def _new_parser():
    # Mirrors xml.etree.ElementTree.XMLParser's own construction exactly
    # (namespace_separator="}", buffered text, ordered attribute lists) so
    # namespace-qualified tags/attributes come out in the same Clark
    # notation ("{uri}local") the rest of this plugin's l: XPath lookups
    # (e.g. root.findall(".//l:Surfaces/l:Surface", NS)) already rely on.
    parser = expat.ParserCreate(None, "}")
    parser.buffer_text = True
    parser.ordered_attributes = True
    parser.StartDoctypeDeclHandler = _forbid_doctype
    parser.EntityDeclHandler = _forbid_entity
    parser.UnparsedEntityDeclHandler = _forbid_unparsed_entity
    parser.ExternalEntityRefHandler = _forbid_external_ref
    return parser


def _wire_builder(parser, builder):
    names = {}

    def fixname(key):
        try:
            return names[key]
        except KeyError:
            name = ("{" + key) if "}" in key else key
            names[key] = name
            return name

    def start(tag, attr_list):
        attrib = {}
        for i in range(0, len(attr_list), 2):
            attrib[fixname(attr_list[i])] = attr_list[i + 1]
        builder.start(fixname(tag), attrib)

    def end(tag):
        builder.end(fixname(tag))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data


def parse(source):
    """Safe replacement for ``xml.etree.ElementTree.parse(source)``. Accepts
    a file path or an open binary file object; returns an ElementTree (call
    ``.getroot()`` as usual)."""
    parser = _new_parser()
    builder = TreeBuilder()
    _wire_builder(parser, builder)

    owns_file = not hasattr(source, "read")
    fh = open(source, "rb") if owns_file else source
    try:
        try:
            parser.ParseFile(fh)
        except expat.ExpatError as exc:
            err = ParseError(str(exc))
            err.code = exc.code
            err.position = exc.lineno, exc.offset
            raise err from exc
    finally:
        if owns_file:
            fh.close()

    return ElementTree(builder.close())


def parse_root(source):
    """Convenience wrapper for the plugin's common
    ``ET.parse(path).getroot()`` pattern."""
    return parse(source).getroot()


def fromstring(text):
    """Safe replacement for ``xml.etree.ElementTree.fromstring(text)``."""
    parser = _new_parser()
    builder = TreeBuilder()
    _wire_builder(parser, builder)
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        err = ParseError(str(exc))
        err.code = exc.code
        err.position = exc.lineno, exc.offset
        raise err from exc
    return builder.close()
