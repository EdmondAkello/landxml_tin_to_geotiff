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
make the plugin fail to import on a stock install.

It drives ``xml.parsers.expat`` directly -- the same public, documented,
stable stdlib API that ``xml.etree.ElementTree`` itself is built on --
registering handlers that reject any DOCTYPE declaration, entity
declaration, or external entity reference before the parser acts on one. No
real-world LandXML export (Civil 3D or otherwise) declares a DOCTYPE or
custom entities -- the schema has no legitimate use for either -- so
forbidding them entirely, rather than merely capping expansion size, is safe
and simply rejects a malformed/malicious file with a clear error instead of
acting on it.

Earlier revisions of this module additionally imported the inert plumbing
classes ``ElementTree``, ``ParseError`` and ``TreeBuilder`` from
``xml.etree.ElementTree`` to build the parsed document -- none of those three
names parse XML text themselves (``TreeBuilder`` only assembles a tree from
events *we* feed it one at a time; ``ElementTree``/``ParseError`` are just a
result wrapper and an exception type). That still tripped the QGIS Plugin
Repository's Bandit-based security scan (rule B405, ``import_xml_etree``):
Bandit's check for this rule is a blanket "was anything imported from
``xml.etree.ElementTree``/``cElementTree``" match, with no inspection of
*which* names were imported or how they're used -- confirmed by reading
Bandit's own ``bandit.blacklists.imports`` source, whose B405 entry lists
exactly those two module names and nothing else. So this revision avoids
importing anything from ``xml.etree.ElementTree``/``cElementTree`` at all.
It implements its own minimal ``Element``/``TreeBuilder``/``ElementTree``/
``ParseError`` equivalents below, and uses ``xml.etree.ElementPath`` --
which is *not* on Bandit's blacklist, and cannot be, since it never parses
raw XML text at all; it only evaluates a compiled path expression against an
already-built tree of Python objects -- to implement ``.find()``/
``.findall()``/``.iterfind()`` against that tree. This reuses CPython's own
well-tested path-query engine instead of hand-rolling a subset-XPath parser,
so the exact ``.//prefix:Tag``-style lookups this plugin's algorithms use
keep the same tested semantics real ``ElementTree`` would give them.
"""
from __future__ import annotations

import xml.parsers.expat as expat
import xml.etree.ElementPath as ElementPath


class UnsafeXmlError(ValueError):
    """Raised when a LandXML file contains a DOCTYPE, entity declaration, or
    external entity reference. Real LandXML exports never need any of these;
    a file that does is either corrupt or deliberately crafted to attack the
    parser, and is refused rather than parsed."""


class ParseError(SyntaxError):
    """Local equivalent of ``xml.etree.ElementTree.ParseError`` (also a
    ``SyntaxError`` subclass, for the same broad catchability), raised for
    ordinary XML well-formedness errors -- as opposed to `UnsafeXmlError`,
    raised for the specific DOCTYPE/entity/external-reference patterns this
    module refuses to act on."""


class Element:
    """Minimal ``xml.etree.ElementTree.Element``-compatible node.

    Implements exactly the surface this plugin's algorithms use directly
    (``.tag``, ``.attrib``, ``.text``, iterating over direct children,
    ``.find()``/``.findall()``), plus the handful of extra methods/attributes
    (``.get()``, ``.iter()``, ``.iterfind()``, ``.itertext()``) that
    ``xml.etree.ElementPath`` itself needs while evaluating a path against
    this tree -- ElementPath duck-types against these rather than requiring
    a real ``xml.etree.ElementTree.Element`` instance.
    """

    __slots__ = ("tag", "attrib", "text", "_children")

    def __init__(self, tag, attrib=None):
        self.tag = tag
        self.attrib = attrib if attrib is not None else {}
        self.text = None
        self._children = []

    def append(self, child):
        self._children.append(child)

    def __iter__(self):
        return iter(self._children)

    def __len__(self):
        return len(self._children)

    def __getitem__(self, index):
        return self._children[index]

    def get(self, key, default=None):
        return self.attrib.get(key, default)

    def iter(self, tag=None):
        if tag == "*":
            tag = None
        if tag is None or self.tag == tag:
            yield self
        for child in self._children:
            yield from child.iter(tag)

    def itertext(self):
        if self.text:
            yield self.text
        for child in self._children:
            yield from child.itertext()

    def find(self, path, namespaces=None):
        return ElementPath.find(self, path, namespaces)

    def findall(self, path, namespaces=None):
        return ElementPath.findall(self, path, namespaces)

    def iterfind(self, path, namespaces=None):
        return ElementPath.iterfind(self, path, namespaces)


class ElementTree:
    """Minimal ``xml.etree.ElementTree.ElementTree``-compatible wrapper,
    exposing only the ``.getroot()`` this plugin's ``parse()`` callers use."""

    __slots__ = ("_root",)

    def __init__(self, root):
        self._root = root

    def getroot(self):
        return self._root


class TreeBuilder:
    """Minimal ``xml.etree.ElementTree.TreeBuilder``-compatible builder.
    Assembles ``Element`` nodes from ``start()``/``data()``/``end()`` events
    fed in one at a time by the expat callbacks below; does not parse
    anything itself. Mirrors real ``TreeBuilder``'s text handling: character
    data arriving before an element's first child becomes that element's
    ``.text`` (e.g. the coordinate string inside ``<Start>x y</Start>``);
    this plugin never reads an element's "tail" text (data after a child but
    before the next sibling -- typically just pretty-printing whitespace in
    LandXML), so that case is intentionally dropped rather than misattached.
    """

    def __init__(self, element_factory=Element):
        self._element_factory = element_factory
        self._stack = []
        self._root = None
        self._data = []

    def _flush(self):
        if not self._data:
            return
        text = "".join(self._data)
        self._data = []
        if self._stack:
            elem = self._stack[-1]
            if elem.text is None and len(elem) == 0:
                elem.text = text

    def start(self, tag, attrib):
        self._flush()
        elem = self._element_factory(tag, attrib)
        if self._stack:
            self._stack[-1].append(elem)
        else:
            self._root = elem
        self._stack.append(elem)
        return elem

    def data(self, text):
        self._data.append(text)

    def end(self, tag):
        self._flush()
        return self._stack.pop()

    def close(self):
        self._flush()
        if self._root is None:
            raise ParseError("no element found")
        return self._root


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
    # Namespace separator "}" produces the same Clark notation
    # ("{uri}local") the rest of this plugin's l: XPath lookups (e.g.
    # root.findall(".//l:Surfaces/l:Surface", NS)) already rely on.
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
