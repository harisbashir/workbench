"""Company logo: validation and SVG cleaning.

SVG files are XML and can contain scripts, so every uploaded SVG is rebuilt
from an allow-list of drawing elements and attributes. PNG/JPEG/WebP files are
checked with Pillow and must be real images of a sensible size.
"""
import io
import re
import xml.etree.ElementTree as ET

from django import forms

MAX_BYTES = 1024 * 1024  # 1 MB
RASTER_TYPES = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ALLOWED_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "defs",
    "lineargradient", "radialgradient", "stop", "clippath", "mask", "use", "symbol", "title", "desc", "style",
    "pattern", "filter", "fegaussianblur", "feoffset", "feblend", "fecolormatrix", "femerge", "femergenode",
    "feflood", "fecomposite",
}
ALLOWED_ATTRS = {
    "id", "class", "viewbox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "d", "points", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "stroke-opacity",
    "opacity", "transform", "offset", "stop-color", "stop-opacity", "gradientunits", "gradienttransform",
    "spreadmethod", "fx", "fy", "clip-path", "clippathunits", "clip-rule", "mask", "maskunits", "font-family",
    "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline", "letter-spacing",
    "preserveaspectratio", "version", "href", "style", "patternunits", "patterntransform", "filter",
    "stddeviation", "dx", "dy", "in", "in2", "result", "mode", "values", "type", "operator", "flood-color",
    "flood-opacity", "visibility", "display", "xml:space", "vector-effect", "paint-order",
}
DANGEROUS_CSS = re.compile(r"(url\s*\(\s*['\"]?\s*(?!#)|expression|javascript:|@import|behavior)", re.I)


def _local(tag):
    return tag.split("}", 1)[-1].lower() if isinstance(tag, str) else ""


def clean_svg(data: bytes) -> bytes:
    if len(data) > MAX_BYTES:
        raise forms.ValidationError("The logo must be under 1 MB.")
    if b"<!ENTITY" in data or b"<!DOCTYPE" in data.upper():
        raise forms.ValidationError("SVG files with DOCTYPE or entity declarations aren't accepted.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise forms.ValidationError("That SVG file couldn't be read.")
    if _local(root.tag) != "svg":
        raise forms.ValidationError("That isn't an SVG image.")

    def scrub(el):
        for child in list(el):
            if _local(child.tag) not in ALLOWED_TAGS:
                el.remove(child)
                continue
            scrub(child)
        for attr in list(el.attrib):
            name = _local(attr) if attr.startswith("{") else attr.lower()
            value = el.attrib[attr]
            if attr == f"{{{XLINK_NS}}}href":
                name = "href"
            if name.startswith("on") or name not in ALLOWED_ATTRS:
                del el.attrib[attr]
                continue
            if name == "href" and not value.strip().startswith("#"):
                del el.attrib[attr]  # only internal references (#id)
                continue
            if DANGEROUS_CSS.search(value) or "javascript" in value.lower():
                del el.attrib[attr]
        if _local(el.tag) == "style" and el.text and DANGEROUS_CSS.search(el.text):
            el.text = ""

    scrub(root)
    if "viewBox" not in root.attrib and "viewbox" not in {a.lower() for a in root.attrib}:
        w, h = root.attrib.get("width", "").rstrip("px"), root.attrib.get("height", "").rstrip("px")
        if w.replace(".", "").isdigit() and h.replace(".", "").isdigit():
            root.set("viewBox", f"0 0 {w} {h}")
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def validate_logo(uploaded):
    """Returns (clean bytes, extension)."""
    data = uploaded.read()
    if len(data) > MAX_BYTES:
        raise forms.ValidationError("The logo must be under 1 MB.")
    name = (uploaded.name or "").lower()
    head = data[:512].lstrip().lower()
    if name.endswith(".svg") or head.startswith(b"<?xml") or head.startswith(b"<svg"):
        return clean_svg(data), "svg"
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise forms.ValidationError("Upload an SVG, PNG, JPEG or WebP image.")
    if img.format not in RASTER_TYPES:
        raise forms.ValidationError("Upload an SVG, PNG, JPEG or WebP image.")
    if img.width < 16 or img.height < 16 or img.width > 4000 or img.height > 4000:
        raise forms.ValidationError("Use an image between 16 and 4000 pixels on each side.")
    return data, RASTER_TYPES[img.format]
