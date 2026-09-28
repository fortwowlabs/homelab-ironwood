#!/usr/bin/env python3
"""Render docs/household/*.md into the static site served at guide.<domain>.

One renderer, two callers: roles/svc_infra/tasks/guide.yml runs it on the
controller at deploy time, and tests/validate_household_guide.py runs it in
`make validate`. A page the gate accepts is therefore byte-for-byte the page
that gets served.

The output must be deterministic — no timestamps, sorted traversal — because
the deploy copies it with checksums and `make deploy-proof` requires an
unchanged guide to report changed=0.

Links between pages are written as `other.md` in the source, so they work on
GitHub and in an editor too; they are rewritten to `other.html` here.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from pathlib import Path
from string import Template

import markdown

EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list"]
ASSET_DIR = "img"
TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
# A relative href ending in .md (optionally with a #fragment). Anything with a
# scheme, an absolute path or a bare fragment is left alone.
MD_LINK_RE = re.compile(r'href="(?![a-z][a-z0-9+.-]*:|/|#)([^"#]+)\.md(#[^"]*)?"', re.IGNORECASE)

STYLE = """
:root { --bg:#fbfaf7; --fg:#1d1d1f; --muted:#5f6368; --accent:#2f6f4e;
        --card:#ffffff; --line:#e3e1dc; --code:#f1efea; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141517; --fg:#ececec; --muted:#a0a4a8; --accent:#7cc8a0;
          --card:#1d1f22; --line:#2e3135; --code:#25282c; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:17px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { position:sticky; top:0; background:var(--card); border-bottom:1px solid var(--line);
         padding:12px 16px; }
header a { color:var(--accent); font-weight:600; text-decoration:none; }
main { max-width:720px; margin:0 auto; padding:16px 16px 48px; }
h1 { font-size:1.6rem; line-height:1.25; margin:.6em 0 .4em; }
h2 { font-size:1.25rem; margin:1.6em 0 .4em; padding-top:.4em; border-top:1px solid var(--line); }
h3 { font-size:1.05rem; margin:1.2em 0 .3em; }
a { color:var(--accent); }
ol, ul { padding-left:1.4em; }
li { margin:.35em 0; }
img { max-width:100%; height:auto; border:1px solid var(--line); border-radius:8px; }
code { background:var(--code); padding:.1em .35em; border-radius:4px; font-size:.95em;
       overflow-wrap:anywhere; }
table { width:100%; border-collapse:collapse; display:block; overflow-x:auto; }
th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }
blockquote { margin:1em 0; padding:.6em 1em; background:var(--card);
             border-left:4px solid var(--accent); border-radius:4px; }
em:last-child { color:var(--muted); }
"""

PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title · Home guide</title>
<style>$style</style>
</head>
<body>
<header><a href="index.html">&#8962; Home guide</a></header>
<main>
$body
</main>
</body>
</html>
"""
)


def render_page(text: str, name: str) -> str:
    match = TITLE_RE.search(text)
    if match is None:
        raise ValueError(f"{name}: no '# ' title line")
    body = markdown.markdown(text, extensions=EXTENSIONS, output_format="html")
    body = MD_LINK_RE.sub(lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"', body)
    return PAGE.substitute(title=html.escape(match.group(1).strip()), style=STYLE, body=body)


def render_site(src: Path, out: Path) -> list[str]:
    if not (src / "index.md").is_file():
        raise ValueError(f"{src}: no index.md, so the site would have no front page")
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for page in sorted(src.glob("*.md")):
        target = out / f"{page.stem}.html"
        target.write_text(
            render_page(page.read_text(encoding="utf-8"), page.name),
            encoding="utf-8",
            newline="\n",
        )
        written.append(target.name)
    assets = src / ASSET_DIR
    if assets.is_dir():
        for asset in sorted(p for p in assets.rglob("*") if p.is_file()):
            relative = asset.relative_to(src)
            (out / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset, out / relative)
            written.append(relative.as_posix())
    return sorted(written)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        written = render_site(args.src, args.out)
    except ValueError as exc:
        print(f"render_household_guide: {exc}", file=sys.stderr)
        return 1
    print(f"rendered {len(written)} files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
