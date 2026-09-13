"""Build self-contained HTML from sections and the included OJS Labs house style.

Optional report covers, contents and citations are resolved locally. Fonts use
system fallbacks. No parent repository, asset download or browser build tool is
required. The output keeps the report kit's copy and unresolved-marker checks.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

KIT = Path(__file__).parent


def _fail(msg: str):
    sys.exit(msg)


def _css(root: Path) -> str:
    css = (KIT / "house.css").read_text()
    extra = root / "extra.css"
    if extra.exists():
        css += "\n/* ---- report-specific ---- */\n" + extra.read_text()
    return "<style>\n" + css + "</style>\n"


def _txt(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&ensp;", " "), ("&rsquo;", "’"), ("&amp;", "&"),
                 ("&middot;", "·"), ("&nbsp;", " "), ("&times;", "×")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


_TOC = re.compile(
    r'<h2 id="([a-z0-9-]+)"[^>]*>\s*<span class="no">[^<]*</span>(.*?)</h2>'
    r'|<h3 id="(s\d+-\d+)"[^>]*>(.*?)</h3>', re.S)


def _contents(body: str) -> str:
    secs: list = []
    for m in _TOC.finditer(body):
        if m.group(1):
            secs.append([m.group(1), _txt(m.group(2)), []])
        elif secs:
            secs[-1][2].append([m.group(3), _txt(m.group(4))])
    parts = ['<div class="tocx-wrap">']
    for sid, title, subs in secs:
        n = sid[1:] if re.fullmatch(r"s\d+", sid) else "&middot;"
        if subs:
            rows = "".join(f'<a href="#{i}">{t}</a>' for i, t in subs)
            parts.append(
                f'<details class="tocx"><summary><span class="tocx-n">{n}</span>'
                f'<span class="tocx-t">{title}</span><span class="tocx-c"></span></summary>'
                f'<div class="tocx-subs"><a href="#{sid}">Overview</a>{rows}</div></details>')
        else:
            parts.append(
                f'<a class="tocx-flat" href="#{sid}"><span class="tocx-n">{n}</span>'
                f'<span class="tocx-t">{title}</span></a>')
    parts.append("</div>")
    return "".join(parts), len(secs)


def _shorturl(u: str) -> str:
    u2 = re.sub(r"^https?://(www\.)?", "", u).rstrip("/")
    return html.escape(u2 if len(u2) <= 68 else u2[:65] + "&hellip;")


def _fmt(e: dict) -> str:
    who, t = e.get("who", ""), e.get("title", "").rstrip(". ")
    when, url = e.get("when", ""), e.get("url", "")
    link = f' <a href="{url}">{_shorturl(url)}</a>' if url else ""
    note = f' {e["note"]}' if e.get("note") else ""
    return f"{who}, &ldquo;{t},&rdquo; {when}.{note}{link}"


def _check(body: str):
    bad = []
    for ch, name in (("—", "EM DASH"), ("–", "EN DASH"), ("―", "HORIZONTAL BAR")):
        for m in re.finditer(re.escape(ch), body):
            bad.append(f"{name} at ...{body[max(0, m.start()-70):m.start()+70]!r}...")
    for m in re.finditer(r"text-transform:\s*uppercase", body):
        bad.append(f"UPPERCASE TRANSFORM at ...{body[max(0, m.start()-70):m.start()+70]!r}...")
    for m in re.finditer(r'class="[^"]*\beyebrow\b', body):
        bad.append(f"EYEBROW CLASS at ...{body[max(0, m.start()-70):m.start()+70]!r}...")
    if bad:
        _fail("LAW VIOLATIONS:\n" + "\n".join(bad[:40]))
    for marker in ("[[c:", "<!--CONTENTS-->", "<!--REFERENCES-->", "<!--COVER:"):
        if marker in body:
            _fail(f"UNRESOLVED MARKER {marker} remains in output")


def build(root: Path, title: str, lang: str = "en") -> str:
    sections = sorted((root / "sections").glob("*.html"))
    if not sections:
        _fail(f"no sections in {root/'sections'}")
    body = "\n".join(p.read_text() for p in sections)

    covers_path = root / "covers.json"
    covers = json.loads(covers_path.read_text()) if covers_path.exists() else {}

    def cover_sub(m):
        k = m.group(1)
        if k not in covers:
            _fail(f"MISSING COVER: {k} (have: {', '.join(covers) or 'none'})")
        return covers[k]
    body = re.sub(r"<!--COVER:([a-z0-9_]+)-->", cover_sub, body)

    if "<!--CONTENTS-->" in body:
        toc, n_secs = _contents(body)
        body = body.replace("<!--CONTENTS-->", toc)
    else:
        n_secs = 0

    refs_path = root / "refs.json"
    refs = json.loads(refs_path.read_text()) if refs_path.exists() else {}
    order: list[str] = []
    missing: list[str] = []

    def resolve(m):
        out = []
        for k in (k.strip() for k in m.group(1).split("|")):
            if k not in refs:
                missing.append(k)
                continue
            if k not in order:
                order.append(k)
            out.append(f'<a href="#ref-{order.index(k)+1}">{order.index(k)+1}</a>')
        return '<sup class="cite">' + ",".join(out) + "</sup>"

    body = re.sub(r"\[\[c:([^\]]+)\]\]", resolve, body)
    if missing:
        _fail("UNKNOWN CITE KEYS:\n  " + "\n  ".join(sorted(set(missing))))
    if "<!--REFERENCES-->" in body:
        rows = "\n".join(f'<li id="ref-{i+1}">{_fmt(refs[k])}</li>' for i, k in enumerate(order))
        body = body.replace("<!--REFERENCES-->", f'<ol class="refs">\n{rows}\n</ol>')

    body = _css(root) + body
    _check(body)

    out = root / "out"
    out.mkdir(exist_ok=True)
    (out / "artifact.html").write_text(body)
    (out / "standalone.html").write_text(
        f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n</head>\n<body>\n{body}\n</body>\n</html>\n")
    print(f"OK: {len(order)} references, {len(body):,} chars, "
          f"{len(sections)} sections, {n_secs} toc entries")
    return body
