"""Check the docs/ knowledge base: reachability, links, anchors, related frontmatter.

Links and images in the repository README.md are checked too; it is the entry point
outside docs/, so it counts for neither reachability nor size.

Policy lives in docs/README.md ("Maintaining this knowledge base"). Exits 1 on errors;
size warnings never fail. Standard library only: `uv run python scripts/check_docs.py`.
"""

import re
import sys
from pathlib import Path

DOCS = (Path(__file__).resolve().parent.parent / "docs").resolve()
ROOT_INDEX = DOCS / "README.md"
REPO_README = DOCS.parent / "README.md"
# Matches images too: their targets must exist, but only .md targets become graph edges.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:")
HUB_MAX, LEAF_MAX = 80, 300


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, text[end + 4 :]


def strip_blocks(body: str) -> str:
    body = re.sub(r"^(```|~~~).*?^\1", "", body, flags=re.DOTALL | re.MULTILINE)
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def strip_code(body: str) -> str:
    return re.sub(r"`[^`\n]*`", "", strip_blocks(body))


def slugs(body: str) -> set[str]:
    """Heading anchors as GitHub generates them, including -1, -2 for duplicates."""
    seen: dict[str, int] = {}
    result = set()
    for line in strip_blocks(body).splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match.group(1).lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def parse_related(value: str) -> list[str]:
    return [item.strip().strip("\"'") for item in value.strip("[]").split(",") if item.strip()]


def check_links(
    path: Path, body: str, anchors: dict[Path, set[str]]
) -> tuple[set[Path], list[str]]:
    """Check one file's links and images; return the documents it links to and errors."""
    name = path.relative_to(DOCS.parent)
    linked, errors = set(), []
    for target in LINK.findall(strip_code(body)):
        if EXTERNAL.match(target) or target.startswith("/"):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            errors.append(f"{name}: broken link -> {target}")
            continue
        if dest.is_dir():
            dest = dest / "README.md"
        if dest in anchors:
            linked.add(dest)
            if anchor and anchor.lower() not in anchors[dest]:
                errors.append(f"{name}: missing anchor -> {target}")
    return linked, errors


def check_file(
    path: Path, meta: dict[str, str], body: str, anchors: dict[Path, set[str]]
) -> tuple[set[Path], list[str], list[str]]:
    """Check one document; return the documents it links to, errors and warnings."""
    name = path.relative_to(DOCS.parent)
    linked, errors = check_links(path, body, anchors)
    warnings = []
    for rel in parse_related(meta.get("related", "")):
        if not (path.parent / rel).resolve().is_file():
            errors.append(f"{name}: related entry does not resolve -> {rel}")
    lines = body.count("\n")
    limit = HUB_MAX if path.name == "README.md" else LEAF_MAX
    if lines > limit:
        warnings.append(f"{name}: {lines} lines, over ~{limit}; consider splitting")
    return linked, errors, warnings


def reachable_from(root: Path, edges: dict[Path, set[Path]]) -> set[Path]:
    seen, queue = {root}, [root]
    while queue:
        for dest in edges[queue.pop()] - seen:
            seen.add(dest)
            queue.append(dest)
    return seen


def main() -> int:
    if not ROOT_INDEX.is_file():
        print(f"ERROR: missing {ROOT_INDEX}")
        return 1
    docs = sorted(DOCS.rglob("*.md"))
    parsed = {path: split_frontmatter(path.read_text(encoding="utf-8")) for path in docs}
    anchors = {path: slugs(body) for path, (_, body) in parsed.items()}
    catalogs = {
        path.parent
        for path, (meta, _) in parsed.items()
        if path.name == "README.md" and meta.get("catalog") == "true"
    }

    edges, errors, warnings = {}, [], []
    for path, (meta, body) in parsed.items():
        edges[path], file_errors, file_warnings = check_file(path, meta, body, anchors)
        errors += file_errors
        warnings += file_warnings
    errors += check_links(REPO_README, REPO_README.read_text(encoding="utf-8"), anchors)[1]

    reachable = reachable_from(ROOT_INDEX, edges)
    for path in docs:
        in_catalog = path.name != "README.md" and path.parent in catalogs
        if path not in reachable and not in_catalog:
            name = path.relative_to(DOCS.parent)
            errors.append(f"{name}: orphan, not reachable from docs/README.md")

    for line in warnings:
        print(f"WARN: {line}")
    for line in errors:
        print(f"ERROR: {line}")
    print(f"{len(docs)} documents, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
