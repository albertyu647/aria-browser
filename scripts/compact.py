"""
Compact renderer for Playwright AI-mode ARIA snapshots.

Input:  page.aria_snapshot(mode="ai") YAML  (- link "new" [ref=e17] [cursor=pointer]: ...)
Output: readable lines where inline text and controls are joined, e.g.

    ## Hacker News
    [Hacker News](e16) [new](e17) | [past](e18) | [comments](e19)
    1. [Voxtral 3](e40) (mistral.ai)
    229 points by [ilreb](e44) 2 hours ago | [hide](e46) | [58 comments](e47)

Every interactive element keeps its ref so it can be clicked/typed into with `aria-ref=eN`.
Structural wrappers (table/row/cell/generic/list...) are dissolved, `/url` lines dropped
(unless urls=True), unnamed images dropped, and duplicated names collapsed.
"""

import re

LINE_RE = re.compile(
    r'^(?P<role>[A-Za-z]+)'
    r'(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?: \[[^\]]*\])*)'
    r'(?::(?: (?P<inline>.*))?)?$'
)
ATTR_RE = re.compile(r'\[([^\]=]+)(?:=([^\]]*))?\]')

INTERACTIVE = {
    "link", "button", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
    "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "slider",
    "spinbutton", "treeitem", "listbox",
}
INPUTS = {"textbox", "searchbox", "combobox", "spinbutton", "slider", "listbox"}
# Containers whose boundaries become line breaks.
BLOCK = {
    "generic", "group", "list", "listitem", "table", "rowgroup", "row", "cell", "gridcell",
    "columnheader", "rowheader", "grid", "region", "section", "article", "paragraph",
    "navigation", "banner", "contentinfo", "complementary", "main", "form", "search",
    "document", "dialog", "alertdialog", "alert", "status", "log", "figure", "blockquote",
    "toolbar", "tablist", "tabpanel", "menu", "menubar", "tree", "feed", "note",
    "separator", "term", "definition", "application", "Section", "dialog", "heading",
    "radiogroup", "caption", "listbox",
}
# Landmarks that get a visible marker line when named (or always for dialogs).
MARKERS = {"dialog", "alertdialog", "article", "navigation", "main", "banner", "complementary", "region", "form", "search"}
STATE_ATTRS = {"checked", "expanded", "selected", "disabled", "pressed", "active"}
PUNCT_ONLY = re.compile(r'^[\s|·•,.;:\-–—/\u2800\u200b\u200c\u200d\ufeff]*$')


class Node:
    __slots__ = ("role", "name", "ref", "attrs", "inline", "children", "props")

    def __init__(self, role, name="", ref=None, attrs=None, inline=None):
        self.role = role
        self.name = name
        self.ref = ref
        self.attrs = attrs or {}
        self.inline = inline
        self.children = []
        self.props = {}


def _unq(s):
    if s is None:
        return None
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    elif len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1].replace("''", "'")
    return s


def _unwrap_yaml_key(body):
    """`'link "a: b" [ref=e1]': rest` -> `link "a: b" [ref=e1]: rest` (YAML-quoted keys)."""
    q = body[0]
    i = 1
    while i < len(body):
        if body[i] == q:
            if q == "'" and i + 1 < len(body) and body[i + 1] == "'":
                i += 2
                continue
            break
        i += 1
    key = body[1:i]
    key = key.replace("''", "'") if q == "'" else key.replace('\\"', '"')
    return key + body[i + 1:]


def parse(snapshot: str) -> Node:
    root = Node("root")
    stack = [(-1, root)]
    for raw in snapshot.splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if not stripped.startswith("- "):
            continue
        body = stripped[2:]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if body.startswith("/"):
            key, _, val = body[1:].partition(":")
            parent.props[key.strip()] = _unq(val)
            continue
        if body.startswith("text:"):
            parent.children.append(Node("text", inline=_unq(body[5:])))
            continue
        if body[:1] in ("'", '"'):
            body = _unwrap_yaml_key(body)
            if body.startswith("text:"):
                parent.children.append(Node("text", inline=_unq(body[5:])))
                continue
        m = LINE_RE.match(body)
        if not m:
            parent.children.append(Node("text", inline=_unq(body)))
            continue
        attrs = {}
        for a, v in ATTR_RE.findall(m.group("attrs") or ""):
            attrs[a.strip()] = v if v != "" else True
        name = m.group("name")
        node = Node(
            m.group("role"),
            name=name.replace('\\"', '"') if name else "",
            ref=attrs.pop("ref", None),
            attrs=attrs,
            inline=_unq(m.group("inline")),
        )
        parent.children.append(node)
        stack.append((indent, node))
    return root


def _text_of(node) -> str:
    """Concatenated visible text of a subtree (used for dedupe)."""
    parts = []
    if node.role == "text":
        return node.inline or ""
    if node.name:
        parts.append(node.name)
    elif node.inline:
        parts.append(node.inline)
    for c in node.children:
        t = _text_of(c)
        if t:
            parts.append(t)
    return " ".join(parts)


# Wrappers that only break lines when they contain block content (divs/spans/table cells).
SOFT = {"generic", "cell", "gridcell", "group", "LabelText", "label", "none", "presentation", "Section"}
INLINE = {"strong", "emphasis", "time", "code", "superscript", "subscript", "mark", "insertion", "deletion", "abbr"}


def _is_block(n) -> bool:
    cached = n.props.get("__block")
    if cached is not None:
        return cached
    if n.role in INLINE:
        b = False
    elif n.role in SOFT:
        b = any(c.role not in ("text", "img") and c.role not in INTERACTIVE and _is_block(c) for c in n.children)
    else:
        b = n.role in BLOCK or n.role not in INTERACTIVE
    n.props["__block"] = b
    return b


def _has_interactive(node) -> bool:
    for c in node.children:
        if c.role in INTERACTIVE or _has_interactive(c):
            return True
    return False


class Renderer:
    def __init__(self, urls=False, images=False, interactive_only=False):
        self.urls = urls
        self.images = images
        self.interactive_only = interactive_only
        self.lines = []
        self.cur = []

    # line management -------------------------------------------------
    def brk(self):
        if self.cur:
            line = " ".join(p for p in self.cur if p).strip()
            line = re.sub(r"\s+", " ", line)
            if line and not PUNCT_ONLY.match(line):
                if not self.lines or self.lines[-1] != line:
                    self.lines.append(line)
            self.cur = []

    def put(self, s):
        if s:
            self.cur.append(s)

    # element formatting ----------------------------------------------
    def fmt_control(self, n):
        label = n.name or n.props.get("placeholder") or ""
        if not label:
            # unnamed link/button: borrow child text or image alt
            label = _text_of(n).strip()
        label = re.sub(r"\s+", " ", label)
        if len(label) > 120:
            label = label[:117] + "…"
        if n.role == "link" and not label and not self.images:
            return ""
        if not n.ref:
            return ""  # not actionable (disabled / aria-hidden duplicate)
        if label.lower().startswith("skip to "):
            return ""
        half = len(label) // 2
        if len(label) > 3 and len(label) % 2 == 1 and label[:half] == label[half + 1:]:
            label = label[:half]  # "Jane Doe Jane Doe" -> "Jane Doe"
        state = [k for k in n.attrs if k in STATE_ATTRS and n.attrs[k] not in ("false", False)]
        ref = n.ref or "?"
        if n.role == "link":
            s = f"[{label}]({ref})"
            if self.urls and n.props.get("url"):
                s += f"<{n.props['url']}>"
        elif n.role in INPUTS:
            val = n.inline if n.inline and n.inline != label else ""
            s = f"[{n.role}: {label}" + (f' = "{val}"' if val else "") + f"]({ref})"
        else:
            s = f"[{n.role}: {label}]({ref})" if label else f"[{n.role}]({ref})"
        if state:
            s += "{" + ",".join(state) + "}"
        return s

    # walk ------------------------------------------------------------
    def walk(self, n):
        role = n.role
        if role == "text":
            if not self.interactive_only:
                self.put(n.inline)
            return
        if role == "img":
            if self.images and n.name and not self.interactive_only:
                self.put(f"[img: {n.name}]")
            return
        if role in INTERACTIVE:
            # controls that wrap other controls (e.g. listbox) → recurse
            if role in ("listbox",) or (_has_interactive(n) and role not in INPUTS):
                if n.name and role != "listbox":
                    self.put(self.fmt_control(n))
                for c in n.children:
                    if c.role in INTERACTIVE or _has_interactive(c):
                        self.walk(c)
                return
            self.put(self.fmt_control(n))
            return
        if role == "heading":
            self.brk()
            level = n.attrs.get("level", "2")
            try:
                level = int(level)
            except Exception:
                level = 2
            if _has_interactive(n):
                self.put("#" * min(level, 6))
                if n.inline:
                    self.put(n.inline)
                for c in n.children:
                    self.walk(c)
            elif not self.interactive_only:
                self.put("#" * min(level, 6) + " " + (n.name or n.inline or _text_of(n)))
            self.brk()
            return
        is_block = _is_block(n)
        if is_block:
            self.brk()
        if role in MARKERS and (n.name or role in ("dialog", "alertdialog")):
            nm = n.name if len(n.name) <= 80 else n.name[:77] + "…"
            self.put(f"<{role}{': ' + nm if nm else ''}>")
            self.brk()
        if n.children:
            if n.inline and not self.interactive_only:
                self.put(n.inline)
            for c in n.children:
                self.walk(c)
        else:
            if not self.interactive_only:
                self.put(n.inline or (n.name if role not in MARKERS else ""))
        if is_block:
            self.brk()


def dominant_prefix(snapshot: str) -> str:
    """Most common frame prefix of refs (e.g. 'f26' in 'f26e176'); '' for the main frame."""
    pre = re.findall(r"\[ref=(f\d+)?e\d+\]", snapshot)
    if not pre:
        return ""
    from collections import Counter
    return Counter(pre).most_common(1)[0][0] or ""


def compact(snapshot: str, urls=False, images=False, interactive_only=False, prefix=None) -> str:
    if prefix is None:
        prefix = dominant_prefix(snapshot)
    if prefix:
        # show refs of the dominant frame as plain eN (resolver re-adds the prefix)
        snapshot = re.sub(r"\[ref=" + prefix + r"(e\d+)\]", r"[ref=\1]", snapshot)
    root = parse(snapshot)
    r = Renderer(urls=urls, images=images, interactive_only=interactive_only)
    for c in root.children:
        r.walk(c)
    r.brk()
    if interactive_only:
        # one control per line is easier to scan
        out = []
        for line in r.lines:
            out.extend(re.findall(r"\[[^\]]*\]\([a-z0-9]+\)(?:\{[^}]*\})?", line) or [line])
        return "\n".join(out)
    return "\n".join(r.lines)


def refs_in(text: str):
    return re.findall(r"\(((?:f\d+)?e\d+)\)", text)
