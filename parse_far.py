#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["beautifulsoup4>=4.12", "lxml>=5"]
# ///
"""
Parse the FAR HTML download (dita_html/) from acquisition.gov into JSONL.

    uv run parse_far.py --input dita_html --output data/parsed

Writes three files into the output folder:

    sections.jsonl     one record per FAR section (metadata, text, blocks)
    hierarchy.json     subchapter > part > subpart > section tree
    parse_report.json  counts, sanity checks, warnings, size stats

There is no chunking in here on purpose. Each section is stored as an ordered
list of blocks (paragraphs, definitions, tables, figures) together with the
nesting depth and the paragraph path, e.g. (a)(1)(ii), so the chunking can be
decided later without touching the HTML again.

The text is left as it is apart from whitespace. Runs of whitespace are
collapsed and the stray spaces the HTML has around punctuation
("3.202 , insert", "( e.g., see") are dropped.
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag

# What a page is can be read off its file name.
SECTION_FILE = re.compile(r"^(\d+)\.(\d+)(?:-(\d+))?\.html$")
PART_FILE = re.compile(r"^Part_(\d+)\.html$")
SUBPART_FILE = re.compile(r"^Subpart_(\d+\.\d+)\.html$")
SUBCHAPTER_FILE = re.compile(r"^Subchapter_([A-Z])\.html$")
# FAR_1.html .. FAR_8.html, one per subchapter. The Part pages point at these
# as their parent, not at Subchapter_X.html.
GROUP_FILE = re.compile(r"^FAR_(\d+)\.html$")

FILE_KINDS = [
    ("section", SECTION_FILE),
    ("part", PART_FILE),
    ("subpart", SUBPART_FILE),
    ("subchapter", SUBCHAPTER_FILE),
    ("group", GROUP_FILE),
]

BLOCK_TAGS = {
    "p", "div", "section", "table", "thead", "tbody", "tr", "td", "th",
    "caption", "br", "figure", "ul", "ol", "li",
}
ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# Paragraph nesting comes from the class: ListL1 = (a), ListL2 = (1), ...
# Alternates use List1/List2 instead, without the L.
DEPTH_CLASS = re.compile(r"^ListL?(\d+)$")
PAREN_LABEL = r"\(\s*(?:[a-z]{1,2}|[A-Z]{1,2}|\d{1,3}|[ivxlc]{1,7})\s*\)"
INLINE_LABELS = re.compile(r"^_*\s*((?:" + PAREN_LABEL + r"\s*)+)(.*)$", re.S)
ONE_LABEL = re.compile(r"\([^()]*\)")
# "(f) Interest. (1) The Contractor ..." - a run-in heading followed straight
# away by the first label of the next level
RUNIN_LABEL = re.compile(r"^[A-Z][^().]{0,80}\.\s*(\((?:1|i|A|a)\))\s*\S")

MONTH_YEAR = r"[A-Z][A-Za-z]{2,8}\.?\s*\d{4}"      # "Apr 1984", "Sept1989"
DATE_PAREN = re.compile(r"\((" + MONTH_YEAR + r")\)\s*$")
# "Alternate I (Nov 2021). As prescribed in ..." or "Alternate II [Reserved]"
ALTERNATE_HEAD = re.compile(
    r"^(Alternate\s+[IVXL]+)\s*(?:\((" + MONTH_YEAR + r")\)|\[Reserved\])")

KIND_INTRO = re.compile(r"following (?:\w+ )?(provision|clause)")
KIND_END = re.compile(r"end of (provision|clause)")
MEANS = re.compile(r"^\s*(?:\([^)]*\)\s*)?means\b")
QUOTED_TERM = re.compile(r"^[\"“]([^\"”]{2,120})[\"”]\s+(?:\([^)]*\)\s*)?means\b")

BIG = 10**6


def classify(name):
    for kind, pattern in FILE_KINDS:
        if pattern.match(name):
            return kind
    return None


def section_sort_key(number):
    m = re.match(r"^(\d+)\.(\d+)(?:-(\d+))?$", number)
    if not m:
        return (BIG, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or -1))


def raw_text(nodes, figure=None):
    """Text under the given nodes, with a space wherever a block element starts or ends.

    Images are skipped, unless `figure` is passed, then figure(img) goes in their place.
    """
    out = []

    def visit(node):
        if isinstance(node, NavigableString):
            if not isinstance(node, Comment):
                out.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "img":
            if figure is not None:
                out.append(" " + figure(node) + " ")
            return
        is_block = node.name in BLOCK_TAGS
        if is_block:
            out.append(" ")
        for child in node.children:
            visit(child)
        if is_block:
            out.append(" ")

    for node in nodes:
        visit(node)
    return "".join(out)


def clean(text):
    """Tidy up whitespace. Nothing other than whitespace is changed."""
    text = ZERO_WIDTH.sub("", text)
    text = " ".join(text.split())
    # "3.202 , insert" -> "3.202, insert"   (but leave ".5 percent" and "_____ .")
    text = re.sub(r"(?<=[^\s(_])\s+(?=[,;:.](?:\s|$|[)\]\"”’]))", "", text)
    # "( e.g., see" -> "(e.g., see",  "15.201 )" -> "15.201)"   ("( )" checkboxes stay)
    text = re.sub(r"\(\s+(?=[^\s)])", "(", text)
    text = re.sub(r"(?<=[^\s(])\s+\)", ")", text)
    return text


def squash(text):
    # no whitespace at all - only used to check that no text went missing
    return "".join(ZERO_WIDTH.sub("", text).split())


def word_count(text):
    return len(text.split())


def classes(tag):
    return list(tag.get("class") or [])


def is_autonumber(node):
    return isinstance(node, Tag) and node.name == "span" and "autonumber" in classes(node)


def list_depth(cls):
    for c in cls:
        m = DEPTH_CLASS.match(c)
        if m:
            return int(m.group(1))
    return None


def dedupe(items):
    return list(dict.fromkeys(items))


def read_topic(path):
    """The bits every page has: id, number, title, parent link and the body div."""
    with path.open(encoding="utf-8") as fh:
        soup = BeautifulSoup(fh, "lxml")

    ident = soup.find("meta", attrs={"name": "DC.Identifier"})
    relations = [m.get("content", "")
                 for m in soup.find_all("meta", attrs={"name": "DC.Relation"})]

    article = soup.find("article", class_="nested0")
    h1 = article.find("h1") if article else None
    label = title = ""
    if h1 is not None:
        span = h1.find("span", class_="autonumber")
        label = clean(span.get_text()) if span else ""
        full = clean(raw_text([h1]))
        title = full[len(label):] if label and full.startswith(label) else full
        title = re.sub(r"^\s*[-–—]\s*", "", title).strip()   # "Part 1 - Federal ..."

    # not every body div has the conbody class (1.400 for one), so go by "body" only
    body = None
    if article is not None:
        for div in article.find_all("div", recursive=False):
            if "body" in classes(div):
                body = div
                break

    parent = soup.select_one("div.parentlink a[href]")
    return {
        "file": path.name,
        "id": ident.get("content") if ident else None,
        "label": label,
        "title": title,
        "parent_file": parent["href"].split("#")[0] if parent else None,
        "relations": relations,
        "body": body,
    }


class Hierarchy:
    """Subchapter > part > subpart > section tree, built from the parent links."""

    def __init__(self):
        self.nodes = {}        # node id -> node, goes into hierarchy.json as is
        self.by_file = {}      # html file name -> node id
        self.relations = {}    # node id -> files in its DC.Relation metas (child order)
        self.parent_file = {}  # node id -> file its "Parent topic" link points to

    def add(self, topic, level, number, url):
        node_id = topic["id"]
        if not node_id:
            stem = topic["file"][: -len(".html")]
            node_id = "FAR_" + stem.replace(".", "_").replace("-", "_")
        self.nodes[node_id] = {
            "id": node_id, "level": level, "number": number, "title": topic["title"],
            "parent": None, "children": [], "url": url, "source_file": topic["file"],
        }
        self.by_file[topic["file"]] = node_id
        self.relations[node_id] = topic["relations"]
        self.parent_file[node_id] = topic["parent_file"]
        return node_id

    def link(self):
        """Set parent and children on every node. Returns nodes whose parent is unknown."""
        unresolved = []
        for node_id, node in self.nodes.items():
            parent = self.parent_file.get(node_id)
            if parent is None:
                continue
            if parent not in self.by_file:
                unresolved.append(node["number"])
                continue
            node["parent"] = self.by_file[parent]

        children = {}
        for node_id, node in self.nodes.items():
            if node["parent"]:
                children.setdefault(node["parent"], []).append(node_id)

        # children in the order the parent page lists them, FAR number as fallback
        for parent_id, ids in children.items():
            listed = self.relations.get(parent_id, [])
            order = {self.by_file.get(f): i for i, f in enumerate(listed)}

            def position(c):
                number = self.nodes[c]["number"]
                return (order.get(c, BIG), section_sort_key(number), number)

            ids.sort(key=position)
            self.nodes[parent_id]["children"] = ids
        return unresolved

    def ancestors(self, node_id):
        """Nodes above this one, top first."""
        chain, seen = [], set()
        cur = self.nodes[node_id]["parent"]
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(self.nodes[cur])
            cur = self.nodes[cur]["parent"]
        return chain[::-1]

    def numbers_by_file(self):
        return {f: self.nodes[i]["number"] for f, i in self.by_file.items()
                if self.nodes[i]["level"] != "subchapter"}


class SectionParser:
    """Turns the body of one section page into a flat, ordered list of blocks."""

    def __init__(self, section_id, number, body, known_files, figures, input_dir):
        self.section_id = section_id
        self.number = number
        self.body = body
        self.body_classes = classes(body) if body is not None else []
        self.known_files = known_files
        self.figures = figures
        self.input_dir = input_dir

        self.blocks = []
        self.raw_parts = []       # raw text of every block, for the nothing-lost check
        self.warnings = []
        self.unresolved_refs = []
        self.figure_log = []
        self.runin_labels = 0

        # state while walking
        self._stack = {}              # depth -> label, i.e. where we are in the (a)(1)(i) list
        self._term = None             # definition we are currently inside
        self._term_depth = 0
        self._plain_def_depth = None  # see the definition handling in _emit_paragraph
        self._alt = None
        self._alt_date = None
        self._auto = 0

    def parse(self):
        if self.body is not None:
            self._walk(self.body)

    def conservation_ok(self):
        """Every character of the body must have ended up in exactly one block."""
        expected = squash(raw_text([self.body])) if self.body is not None else ""
        return expected == "".join(squash(part) for part in self.raw_parts)

    def resolve_refs(self, numbers_by_file):
        # Links are collected as file names. They can only be turned into FAR
        # numbers once all files are read, because a file name can be wrong
        # (42.200.html holds 40.200).
        for block in self.blocks:
            for key in ("cross_refs", "prescribed_in"):
                if key not in block:
                    continue
                refs = [numbers_by_file.get(f) for f in block[key]]
                block[key] = dedupe(r for r in refs if r and r != self.number)
            if block.get("prescribed_in") == []:
                del block["prescribed_in"]

    # -- walking the body --

    def _walk(self, container):
        loose = []   # inline stuff sitting directly in a div, outside any <p>

        def flush():
            if loose:
                self._emit_loose(list(loose))
                loose.clear()

        for child in container.children:
            if isinstance(child, NavigableString):
                if not isinstance(child, Comment) and str(child).strip():
                    loose.append(child)
                continue
            if not isinstance(child, Tag):
                continue

            if child.name not in ("p", "table", "figure", "img", "section", "div"):
                loose.append(child)
                continue

            flush()
            if child.name == "p":
                self._emit_paragraph(child)
            elif child.name == "table":
                self._emit_table(child)
            elif child.name == "figure":
                imgs = child.find_all("img")
                for img in imgs:
                    self._emit_figure(img)
                if not imgs:
                    self._emit_loose([child])
            elif child.name == "img":
                self._emit_figure(child)
            elif child.name == "section":
                self._walk_section(child)
            else:
                self._walk(child)
        flush()

    def _walk_section(self, section):
        # A <section> holds the alternates of a clause, sometimes several in one.
        # Each alternate starts with its own "Alternate I (date)." paragraph and
        # that is picked up in _emit_paragraph, here it is only about the scope.
        saved = (self._alt, self._alt_date, dict(self._stack), self._term,
                 self._term_depth, self._plain_def_depth)
        self._reset_list_state()
        self._walk(section)
        (self._alt, self._alt_date, self._stack, self._term,
         self._term_depth, self._plain_def_depth) = saved

    def _reset_list_state(self):
        self._stack = {}
        self._term = None
        self._plain_def_depth = None

    # -- small helpers --

    def _block_id(self, el):
        if el is not None and el.get("id"):
            return el["id"]
        self._auto += 1
        return f"{self.section_id}__b{self._auto}"

    def _new_block(self, el, btype, text, refs):
        block = {
            "block_id": self._block_id(el),
            "type": btype,
            "depth": None,
            "label": None,
            "path": None,
            "term": None,
            "alternate": self._alt,
            "text": text,
            "word_count": word_count(text),
            "cross_refs": refs,
        }
        if self._alt is not None:
            block["alternate_date"] = self._alt_date
        return block

    def _path(self):
        path = "".join(self._stack[d] for d in sorted(self._stack) if self._stack[d])
        return path or None

    def _figure_info(self, img):
        src = img.get("src", "")
        name = src.rsplit("/", 1)[-1]
        entry = self.figures.get(name)
        info = {
            "file": name,
            "image_path": src,
            "image_found": bool(src) and (self.input_dir / src).is_file(),
            "transcribed": entry is not None,
            "figure_kind": entry.get("kind") if entry else None,
            "text": entry["text"] if entry else name,
            "section": self.number,
        }
        self.figure_log.append(info)
        return info

    def _figure_inline(self, img):
        return f"[Figure: {self._figure_info(img)['text']}]"

    def _cross_refs(self, nodes):
        """File names of the FAR pages linked from these nodes."""
        refs = []
        for node in nodes:
            if not isinstance(node, Tag):
                continue
            anchors = node.find_all("a")
            if node.name == "a":
                anchors.insert(0, node)
            for a in anchors:
                href = a.get("href") or ""
                if not href or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", href, re.I):
                    continue          # external (U.S.C., CFR, ...)
                target = href.split("#")[0]
                if not target.endswith(".html"):
                    continue          # same-page anchor, or a URL typed without http
                if target not in self.known_files:
                    self.unresolved_refs.append(href)
                    continue
                if classify(target) in ("section", "part", "subpart"):
                    refs.append(target)
        return dedupe(refs)

    def _labels(self, kids, text):
        """Paragraph label(s), e.g. ["(a)"] or ["(b)", "(1)"], and the span they came from."""
        for kid in kids:
            if is_autonumber(kid):
                span_text = re.sub(r"\s+", "", kid.get_text())
                if span_text:     # there are a few empty autonumber spans around
                    return ONE_LABEL.findall(span_text) or [span_text], kid
        # no span: alternates and some clauses just type the label into the text
        m = INLINE_LABELS.match(text)
        if m and m.group(2).strip():
            return [re.sub(r"\s+", "", x) for x in ONE_LABEL.findall(m.group(1))], None
        return [], None

    def _runin_label(self, text, labels):
        rest = text.lstrip("_ ")
        for label in labels:
            rest = re.sub(r"^\(\s*" + re.escape(label[1:-1]) + r"\s*\)\s*", "", rest, count=1)
        m = RUNIN_LABEL.match(rest)
        return m.group(1) if m else None

    def _defined_term(self, kids, label_span, labels, text):
        """The term, if this paragraph is a definition ("Acquisition means ...")."""
        content = [k for k in kids if k is not label_span and not is_autonumber(k)]
        lead = content[0] if content else None
        if (isinstance(lead, NavigableString) and len(content) > 1
                and str(lead).strip() in {'"', "“", "'", "‘"}):
            lead = content[1]     # opening quote before the <em>

        if isinstance(lead, Tag) and lead.name == "em":
            # Mostly <em class="Term">. Some definitions only have a plain <em>,
            # those count when "means" comes right after.
            after = raw_text(list(lead.next_siblings)).lstrip(' "”’\'')
            if "Term" in classes(lead) or MEANS.match(after):
                term = clean(lead.get_text()).strip(' "“”‘’\'.,:;')
                if term:
                    return term

        if not labels:
            quoted = QUOTED_TERM.match(text)      # "Component" means ...
            if quoted:
                return quoted.group(1).strip(" .,:;") or None
        return None

    # -- blocks --

    def _emit_paragraph(self, p):
        raw = raw_text([p])
        has_img = p.find("img") is not None
        if not raw.strip() and not has_img:
            return      # lots of empty <p class="p"></p> in the download
        self.raw_parts.append(raw)
        text = clean(raw_text([p], self._figure_inline)) if has_img else clean(raw)
        cls = classes(p)

        # An alternate runs until the next alternate or the end of the <section>.
        # What follows is only a piece of the basic clause, e.g. just a new
        # paragraph (b), so the list position starts from scratch.
        alt = ALTERNATE_HEAD.match(text)
        if alt:
            self._alt, self._alt_date = clean(alt.group(1)), alt.group(2)
            self._reset_list_state()

        depth = list_depth(cls)
        kids = [c for c in p.children
                if not (isinstance(c, NavigableString) and not str(c).strip())]
        labels, label_span = self._labels(kids, text)
        runin = None
        if labels and depth is not None:
            runin = self._runin_label(text, labels)
        term = self._defined_term(kids, label_span, labels, text)

        if "Endofclause" in cls or "Endofprovision" in cls:
            btype = "end_marker"
        elif "Ctr_SmCaps" in cls:
            # the clause title line ends with its date, other centered caps
            # lines ("Recitals", "Covenants") are just headings
            btype = "clause_heading" if DATE_PAREN.search(text) else "heading"
        elif term:
            btype = "definition"
        else:
            btype = "paragraph"

        block = self._new_block(p, btype, text, self._cross_refs([p]))

        # Sub-paragraphs under a definition carry its term.
        # In most definition sections the definitions are plain paragraphs with
        # no list class while their (1), (2) are ListL2. Such a definition is
        # taken to sit at whatever level the list had reached when the first
        # one showed up.
        if (depth is not None and self._plain_def_depth is not None
                and depth <= self._plain_def_depth):
            self._plain_def_depth = None
        if btype == "definition":
            if depth is not None:
                self._term_depth = depth
            else:
                if self._plain_def_depth is None:
                    self._plain_def_depth = max(self._stack, default=0)
                self._term_depth = self._plain_def_depth
                for d in [d for d in self._stack if d > self._term_depth]:
                    del self._stack[d]
            self._term = term
            block["term"] = term
        elif self._term is not None:
            if depth is not None and depth > self._term_depth:
                block["term"] = self._term
            else:
                self._term = None

        if depth is not None:
            # A labelled paragraph with nothing one level up. Fine below a
            # definition and inside an alternate, anywhere else it gets reported.
            jump = (bool(labels) and depth > 1 and (depth - 1) not in self._stack
                    and block["term"] is None and self._alt is None)

            for d in [d for d in self._stack if d >= depth]:
                del self._stack[d]
            if labels:
                for i, label in enumerate(labels):
                    self._stack[depth + i] = label
            else:
                self._stack[depth] = None
            block["depth"] = depth
            block["label"] = "".join(labels) or None
            block["path"] = self._path()

            if runin:
                # so that the (i) below "(f) Interest. (1) ..." becomes (f)(1)(i)
                self._stack[depth + len(labels)] = runin
                block["runin_label"] = runin
                self.runin_labels += 1
            if jump:
                self.warnings.append({
                    "type": "depth_jump",
                    "section": self.number,
                    "block_id": block["block_id"],
                    "detail": f"{block['path']} at depth {depth} "
                              f"has no paragraph at depth {depth - 1}",
                })
        elif labels:
            block["label"] = "".join(labels)

        if btype == "clause_heading":
            m = DATE_PAREN.search(text)
            block["clause_date"] = m.group(1)
            block["clause_title"] = text[: m.start()].strip()

        prescribed = self._cross_refs(p.find_all("span", class_="ClauseProvisionXREF"))
        if prescribed:
            block["prescribed_in"] = prescribed
        self.blocks.append(block)

    def _emit_loose(self, nodes):
        raw = raw_text(nodes)
        if not raw.strip():
            return
        self.raw_parts.append(raw)
        block = self._new_block(None, "paragraph", clean(raw), self._cross_refs(nodes))
        first = next((n for n in nodes if isinstance(n, Tag)), None)
        if first is not None and first.parent is not None and "note" in classes(first.parent):
            block["note"] = True
        self._term = None
        self.blocks.append(block)

    def _emit_table(self, table):
        self.raw_parts.append(raw_text([table]))
        has_img = table.find("img") is not None
        figure = self._figure_inline if has_img else None

        cap = table.find("caption")
        caption = clean(raw_text([cap])) if cap is not None else ""

        header_rows, body_rows = [], []
        for tr in table.find_all("tr"):
            row = []
            for cell in tr.find_all(["td", "th"], recursive=False):
                row.append(clean(raw_text([cell], figure)))
                try:
                    span = int(cell.get("colspan", 1))
                except ValueError:
                    span = 1
                row.extend([""] * (span - 1))    # pad so the columns still line up
            if not any(row):
                continue
            if tr.find_parent("thead") is not None:
                header_rows.append(row)
            else:
                body_rows.append(row)

        rows = header_rows + body_rows
        width = max((len(r) for r in rows), default=0)
        rows = [r + [""] * (width - len(r)) for r in rows]

        if not has_img:
            cells = squash(caption) + "".join(squash(c) for r in rows for c in r)
            if cells != squash(raw_text([table])):
                self.warnings.append({
                    "type": "table_text_mismatch", "section": self.number,
                    "block_id": "", "detail": caption or "(no caption)",
                })

        lines = []
        if caption:
            lines += [caption, ""]
        for i, row in enumerate(rows):
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
            if header_rows and i == 0:
                lines.append("|" + " --- |" * width)

        block = self._new_block(table, "table", "\n".join(lines), self._cross_refs([table]))
        block["term"] = self._term
        block["caption"] = caption or None
        block["header_rows"] = len(header_rows)
        block["rows"] = rows
        block["path"] = self._path()
        self.blocks.append(block)

    def _emit_figure(self, img):
        info = self._figure_info(img)
        block = self._new_block(None, "figure", f"[Figure: {info['text']}]", [])
        block["term"] = self._term
        block["path"] = self._path()
        for key in ("image_path", "image_found", "transcribed", "figure_kind"):
            block[key] = info[key]
        self.blocks.append(block)


def clause_or_provision(main_blocks, body_classes):
    """Is this part 52 entry a clause or a provision?

    The class on the body div gets it wrong for a good number of them, so the
    text goes first: the "insert the following ..." sentence, then the
    "(End of ...)" line, and the class only if both are missing.
    Returns (kind, what the intro says, what the end line says).
    """
    intro = KIND_INTRO.search(main_blocks[0]["text"].lower()) if main_blocks else None
    end = None
    for b in main_blocks:
        if b["type"] == "end_marker":
            end = KIND_END.search(b["text"].lower())
            break
    intro = intro.group(1) if intro else None
    end = end.group(1) if end else None
    by_class = next((k for k in ("clause", "provision") if k in body_classes), None)
    return intro or end or by_class or "section", intro, end


def distribution(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p):
        return ordered[min(n - 1, int(n * p))]

    return {
        "count": n,
        "total": sum(ordered),
        "mean": round(statistics.fmean(ordered), 1),
        "median": pct(0.50),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": ordered[-1],
    }


def top_level_groups(blocks):
    """Word count of each top-level paragraph plus everything nested under it.

    A new group starts at a depth 1 paragraph or at any text that is not in a
    list. Tables and figures stay with whatever came before them.
    """
    groups = []
    for b in blocks:
        starts = b["type"] not in ("table", "figure") and (b["depth"] is None or b["depth"] <= 1)
        if starts or not groups:
            groups.append(0)
        groups[-1] += b["word_count"]
    return groups


def count_over(values, limits):
    return {str(n): sum(1 for v in values if v > n) for n in limits}


def warning(kind, section, detail=""):
    return {"type": kind, "section": section, "block_id": "", "detail": detail}


def add_containers(tree, by_kind, base_url, warnings):
    """Subchapter, part and subpart pages. They only have a TOC, no text of their own."""
    for path in by_kind.get("subchapter", []):
        topic = read_topic(path)
        letter = SUBCHAPTER_FILE.match(path.name).group(1)
        node_id = tree.add(topic, "subchapter", f"Subchapter {letter}", None)
        tree.parent_file[node_id] = None
        # the FAR_n.html listed here is the same subchapter under another name
        for rel in topic["relations"]:
            if GROUP_FILE.match(rel):
                tree.by_file[rel] = node_id

    for path in by_kind.get("group", []):
        if path.name not in tree.by_file:
            warnings.append(warning("group_without_subchapter", path.name))
        else:
            # FAR_n.html is the one that lists the parts, in order
            tree.relations[tree.by_file[path.name]] = read_topic(path)["relations"]

    for path in by_kind.get("part", []):
        n = PART_FILE.match(path.name).group(1)
        tree.add(read_topic(path), "part", f"Part {n}", f"{base_url}/part-{n}")

    for path in by_kind.get("subpart", []):
        n = SUBPART_FILE.match(path.name).group(1)
        tree.add(read_topic(path), "subpart", f"Subpart {n}", f"{base_url}/subpart-{n}")


def parse_sections(paths, tree, known_files, figures, args, warnings):
    """Parse every section page. Returns {node id: SectionParser} and the misnamed files."""
    parsers = {}
    misnamed = []
    seen = {}
    for path in paths:
        topic = read_topic(path)
        stem = path.name[: -len(".html")]
        # Trust the number in the heading over the file name.
        number = stem
        if re.match(r"^\d+\.\d+(?:-\d+)?$", topic["label"]):
            number = topic["label"]
        if number != stem:
            misnamed.append({"file": path.name, "section": number})
        if number in seen:
            warnings.append(warning("duplicate_section_number", number,
                                    f"{seen[number]} and {path.name}"))
        seen[number] = path.name

        level = "subsection" if "-" in number else "section"
        node_id = tree.add(topic, level, number, f"{args.base_url}/{number}")
        sp = SectionParser(node_id, number, topic["body"], known_files, figures, args.input)
        sp.parse()
        parsers[node_id] = sp
        warnings.extend(sp.warnings)

    numbers = tree.numbers_by_file()
    for sp in parsers.values():
        sp.resolve_refs(numbers)
    return parsers, misnamed


def build_record(node, sp, tree, args, stats, warnings):
    number, blocks = node["number"], sp.blocks
    part_no = int(number.split(".")[0])

    chain = tree.ancestors(node["id"])
    above = {a["level"]: a for a in chain}
    subchapter, part, subpart = above.get("subchapter"), above.get("part"), above.get("subpart")
    crumbs = [a["number"] for a in chain if a["level"] != "subchapter"]

    parent = tree.nodes[node["parent"]] if node["parent"] else None
    parent_section = None
    if parent and parent["level"] in ("section", "subsection"):
        parent_section = parent["number"]

    # the basic clause, without its alternates
    main_blocks = [b for b in blocks if b["alternate"] is None]

    kind = "section"
    if part_no == 52 and blocks:
        kind, intro, end = clause_or_provision(main_blocks, sp.body_classes)
        if intro and end and intro != end:
            # the FAR itself says "provision" at the top and "(End of clause)" at the bottom
            stats["kind_conflicts"].append({
                "section": number, "prescription_says": intro,
                "end_marker_says": end, "used": kind,
            })
    elif blocks and {"clause", "provision", "prescribed"} & set(sp.body_classes):
        warnings.append(warning("clause_class_outside_part_52", number,
                                " ".join(sp.body_classes)))
    is_clause = kind in ("clause", "provision")

    heading = next((b for b in main_blocks if b["type"] == "clause_heading"), None)
    prescribed = dedupe(ref for b in main_blocks for ref in b.get("prescribed_in", []))
    if is_clause and not prescribed:
        stats["clauses_without_prescription"].append(number)

    if part and part["number"] != f"Part {part_no}":
        warnings.append(warning("part_mismatch", number, f"hierarchy says {part['number']}"))

    return {
        "section_id": node["id"],
        "section_number": number,
        "section_title": node["title"],
        "level": node["level"],
        "part": part_no,
        "part_title": part["title"] if part else None,
        "subpart": subpart["number"].split(" ", 1)[1] if subpart else None,
        "subpart_title": subpart["title"] if subpart else None,
        "subchapter": subchapter["number"].split(" ", 1)[1] if subchapter else None,
        "subchapter_title": subchapter["title"] if subchapter else None,
        "parent_section": parent_section,
        "children": [tree.nodes[c]["number"] for c in node["children"]],
        "breadcrumb": " > ".join(crumbs + [number]),
        "kind": kind,
        "clause_title": heading["clause_title"] if is_clause and heading else None,
        "clause_date": heading["clause_date"] if is_clause and heading else None,
        "prescribed_in": prescribed if is_clause else [],
        "alternates": dedupe(b["alternate"] for b in blocks if b["alternate"]),
        "is_reserved": bool(re.search(r"\breserved\b", node["title"], re.I)),
        "has_text": bool(blocks),
        "text": "\n\n".join(b["text"] for b in blocks),
        "word_count": sum(b["word_count"] for b in blocks),
        "block_count": len(blocks),
        "cross_refs": dedupe(ref for b in blocks for ref in b["cross_refs"]),
        "url": node["url"],
        "source_file": node["source_file"],
        "fac_number": args.fac,
        "effective_date": args.effective_date,
        "blocks": blocks,
    }


def write_sections(path, tree, parsers, args, warnings):
    """Write sections.jsonl in FAR order and collect the numbers for the report."""
    stats = {
        "kinds": Counter(),
        "block_types": Counter(),
        "section_words": [],
        "group_words": [],
        "block_words": [],
        "largest": [],
        "large_blocks": [],
        "empty": 0,
        "reserved": 0,
        "with_definitions": 0,
        "clauses_without_prescription": [],
        "kind_conflicts": [],
    }
    ids = sorted(parsers, key=lambda i: section_sort_key(tree.nodes[i]["number"]))

    with path.open("w", encoding="utf-8") as out:
        for node_id in ids:
            node = tree.nodes[node_id]
            rec = build_record(node, parsers[node_id], tree, args, stats, warnings)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")

            number, blocks, words = rec["section_number"], rec["blocks"], rec["word_count"]
            # handy to have in hierarchy.json as well
            node["kind"] = rec["kind"]
            node["has_text"] = rec["has_text"]
            node["word_count"] = words

            stats["kinds"][rec["kind"]] += 1
            stats["reserved"] += rec["is_reserved"]
            if not blocks:
                stats["empty"] += 1
                continue

            stats["section_words"].append(words)
            stats["largest"].append((words, number, rec["section_title"]))
            stats["group_words"].extend(top_level_groups(blocks))
            if any(b["type"] == "definition" for b in blocks):
                stats["with_definitions"] += 1
            for b in blocks:
                stats["block_types"][b["type"]] += 1
                stats["block_words"].append(b["word_count"])
                if b["word_count"] > 500:
                    stats["large_blocks"].append({
                        "section": number, "block_id": b["block_id"], "type": b["type"],
                        "path": b["path"], "word_count": b["word_count"],
                    })

    stats["total"] = len(ids)
    return stats


def build_report(args, html_files, by_kind, skipped, tree, parsers, stats,
                 misnamed, unresolved_parents, warnings):
    nodes = tree.nodes.values()
    figures = [f for sp in parsers.values() for f in sp.figure_log]
    bad_refs = Counter(r for sp in parsers.values() for r in sp.unresolved_refs)
    warn_types = Counter(w["type"] for w in warnings)

    section_words = stats["section_words"]
    group_words = stats["group_words"]
    block_words = stats["block_words"]
    largest = sorted(stats["largest"], reverse=True)[:15]

    return {
        "fac_number": args.fac,
        "effective_date": args.effective_date,
        "input": str(args.input),
        "files": {
            "html_total": len(html_files),
            "sections": len(by_kind.get("section", [])),
            "parts": len(by_kind.get("part", [])),
            "subparts": len(by_kind.get("subpart", [])),
            "subchapters": len(by_kind.get("subchapter", [])),
            "navigation_groups": len(by_kind.get("group", [])),
            "skipped": skipped,
        },
        "hierarchy": dict(Counter(n["level"] for n in nodes)),
        "sections": {
            "total": stats["total"],
            "with_text": stats["total"] - stats["empty"],
            "without_text": stats["empty"],
            "title_reserved": stats["reserved"],
            "by_kind": dict(stats["kinds"]),
            "with_definitions": stats["with_definitions"],
        },
        "blocks": {
            "total": sum(stats["block_types"].values()),
            "by_type": dict(stats["block_types"].most_common()),
        },
        "word_counts": {
            "note": "sections with text only; a top-level group is a depth-1 paragraph "
                    "plus everything under it",
            "section": distribution(section_words),
            "top_level_group": distribution(group_words),
            "block": distribution(block_words),
            "sections_over": count_over(section_words, (300, 500, 800, 1500, 3000)),
            "top_level_groups_over": count_over(group_words, (300, 500, 800, 1500)),
            "blocks_over": count_over(block_words, (300, 500, 800)),
        },
        "largest_sections": [{"section": n, "title": t, "word_count": w} for w, n, t in largest],
        "blocks_over_500_words": sorted(stats["large_blocks"], key=lambda b: -b["word_count"]),
        "figures": figures,
        "checks": {
            "text_conservation_failures": [sp.number for sp in parsers.values()
                                           if not sp.conservation_ok()],
            "table_text_mismatches": warn_types.get("table_text_mismatch", 0),
            "unresolved_parents": unresolved_parents,
            "nodes_without_parent": [n["number"] for n in nodes
                                     if n["level"] != "subchapter" and not n["parent"]],
            "unresolved_cross_refs": dict(bad_refs.most_common()),
            "figures_missing_file_or_transcription": [
                f for f in figures if not f["image_found"] or not f["transcribed"]],
            "depth_jumps": warn_types.get("depth_jump", 0),
            "clauses_without_prescription": stats["clauses_without_prescription"],
            "clause_kind_conflicts": stats["kind_conflicts"],
            "misnamed_files": misnamed,
            "runin_labels_inferred": sum(sp.runin_labels for sp in parsers.values()),
        },
        "warnings": warnings,
    }


def print_summary(report, output):
    files, levels = report["files"], report["hierarchy"]
    secs, checks = report["sections"], report["checks"]

    print(f"FAR {report['fac_number']} (effective {report['effective_date']})")
    print(f"  files      {files['html_total']} html: {secs['total']} sections, "
          f"{levels.get('part', 0)} parts, {levels.get('subpart', 0)} subparts, "
          f"{levels.get('subchapter', 0)} subchapters, {len(files['skipped'])} skipped")
    print(f"  sections   {secs['with_text']} with text, {secs['without_text']} without "
          f"({secs['title_reserved']} reserved); kinds {secs['by_kind']}")
    print(f"  blocks     {report['blocks']['total']}: {report['blocks']['by_type']}")
    for name in ("section", "top_level_group", "block"):
        d = report["word_counts"][name]
        print(f"  words/{name:<16} n={d['count']:<6} median={d['median']:<5} "
              f"p90={d['p90']:<5} p99={d['p99']:<5} max={d['max']}")
    print(f"  figures    {len(report['figures'])} referenced, "
          f"{len(checks['figures_missing_file_or_transcription'])} "
          f"missing a file or transcription")
    print(f"  checks     conservation failures={len(checks['text_conservation_failures'])}, "
          f"table mismatches={checks['table_text_mismatches']}, "
          f"unresolved parents={len(checks['unresolved_parents'])}, "
          f"orphans={len(checks['nodes_without_parent'])}, "
          f"unresolved cross-refs={sum(checks['unresolved_cross_refs'].values())}, "
          f"depth jumps={checks['depth_jumps']}")
    for m in checks["misnamed_files"]:
        print(f"  note       {m['file']} contains section {m['section']}; "
              f"indexed as {m['section']}")
    print(f"  wrote      {output}/sections.jsonl, hierarchy.json, parse_report.json")


def parse_args():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Parse the FAR HTML download into JSONL.")
    ap.add_argument("--input", type=Path, default=Path("dita_html"),
                    help="folder of FAR HTML files")
    ap.add_argument("--output", type=Path, default=Path("data/parsed"), help="output folder")
    ap.add_argument("--figures", type=Path, default=here / "figure_text.json",
                    help="JSON of figure transcriptions keyed by image filename")
    # the HTML does not say which FAC it is, so it has to be passed in
    ap.add_argument("--fac", default="2026-01",
                    help="Federal Acquisition Circular number of the download")
    ap.add_argument("--effective-date", default="2026-03-13",
                    help="effective date of that FAC (YYYY-MM-DD)")
    ap.add_argument("--base-url", default="https://www.acquisition.gov/far",
                    help="base for citation URLs")
    return ap.parse_args()


def main():
    args = parse_args()
    if not args.input.is_dir():
        print(f"error: input folder not found: {args.input}", file=sys.stderr)
        return 2

    figures = {}
    if args.figures.is_file():
        figures = json.loads(args.figures.read_text(encoding="utf-8"))

    html_files = sorted(p for p in args.input.iterdir() if p.suffix == ".html")
    by_kind, skipped = {}, []     # skipped = index, cover, foreword and the like
    for path in html_files:
        kind = classify(path.name)
        if kind is None:
            skipped.append(path.name)
        else:
            by_kind.setdefault(kind, []).append(path)

    warnings = []
    tree = Hierarchy()
    add_containers(tree, by_kind, args.base_url, warnings)
    parsers, misnamed = parse_sections(by_kind.get("section", []), tree,
                                       {p.name for p in html_files}, figures, args, warnings)
    unresolved_parents = tree.link()

    args.output.mkdir(parents=True, exist_ok=True)
    stats = write_sections(args.output / "sections.jsonl", tree, parsers, args, warnings)

    roots = [i for i, n in tree.nodes.items() if n["level"] == "subchapter"]
    roots.sort(key=lambda i: tree.nodes[i]["number"])
    hierarchy = {
        "fac_number": args.fac,
        "effective_date": args.effective_date,
        "roots": roots,
        "nodes": tree.nodes,
    }
    (args.output / "hierarchy.json").write_text(
        json.dumps(hierarchy, ensure_ascii=False, indent=1), encoding="utf-8")

    report = build_report(args, html_files, by_kind, skipped, tree, parsers, stats,
                          misnamed, unresolved_parents, warnings)
    (args.output / "parse_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print_summary(report, args.output)

    checks = report["checks"]
    if (checks["text_conservation_failures"] or checks["unresolved_parents"]
            or checks["nodes_without_parent"]):
        print("  FAILED integrity checks; see parse_report.json", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
