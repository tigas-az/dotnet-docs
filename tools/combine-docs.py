#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required. Install with: pip install pyyaml") from exc


@dataclass
class DocEntry:
    depth: int
    title: str
    path: Path


class TocWalker:
    def __init__(self, repo_root: Path, docs_root: Path, section: Path | None) -> None:
        self.repo_root = repo_root
        self.docs_root = docs_root
        self.section = section
        self.visited_yaml: set[Path] = set()
        self.seen_docs: set[Path] = set()

    def build(self) -> list[DocEntry]:
        start_file = self._find_start_file()
        if start_file.suffix.lower() == ".md":
            return [DocEntry(depth=1, title=self._title_from_path(start_file), path=start_file)]

        entries: list[DocEntry] = []
        if start_file.exists():
            self._walk_yaml(start_file, parent_title=None, depth=1, entries=entries)

        if not entries and self.section:
            for md in sorted(self.section.rglob("*.md")):
                entries.append(DocEntry(depth=1, title=self._title_from_path(md), path=md))

        return entries

    def _find_start_file(self) -> Path:
        if not self.section:
            return self.docs_root / "toc.yml"

        section = self.section
        if section.is_file():
            return section

        for candidate in ("toc.yml", "index.yml", "index.md"):
            path = section / candidate
            if path.exists():
                return path

        return section

    def _walk_yaml(self, yaml_path: Path, parent_title: str | None, depth: int, entries: list[DocEntry]) -> None:
        yaml_path = yaml_path.resolve()
        if yaml_path in self.visited_yaml or not yaml_path.exists():
            return
        self.visited_yaml.add(yaml_path)

        data = self._load_yaml(yaml_path)
        if data is None:
            return

        if isinstance(data, dict) and isinstance(data.get("items"), list):
            for item in data["items"]:
                self._walk_toc_item(item, yaml_path.parent, depth, entries)
            return

        links = self._extract_links(data)
        for title, href in links:
            self._process_href(title=title, href=href, base_dir=yaml_path.parent, depth=depth, entries=entries)

    def _walk_toc_item(self, item: Any, base_dir: Path, depth: int, entries: list[DocEntry]) -> None:
        if not isinstance(item, dict):
            return

        title = str(item.get("name") or item.get("title") or "Untitled").strip()
        href = item.get("href")
        if isinstance(href, str) and href.strip():
            self._process_href(title=title, href=href, base_dir=base_dir, depth=depth, entries=entries)

        child_items = item.get("items")
        if isinstance(child_items, list):
            for child in child_items:
                self._walk_toc_item(child, base_dir=base_dir, depth=depth + 1, entries=entries)

    def _process_href(self, title: str, href: str, base_dir: Path, depth: int, entries: list[DocEntry]) -> None:
        href = href.strip()
        if not href or self._is_external(href):
            return

        resolved = self._resolve_local_path(href, base_dir)
        if not resolved:
            return

        if resolved.is_dir():
            toc = resolved / "toc.yml"
            if toc.exists():
                self._walk_yaml(toc, parent_title=title, depth=depth + 1, entries=entries)
            return

        suffix = resolved.suffix.lower()
        if suffix == ".md":
            if self.section and not self._is_in_section(resolved):
                return
            if resolved not in self.seen_docs:
                self.seen_docs.add(resolved)
                entries.append(DocEntry(depth=depth, title=title or self._title_from_path(resolved), path=resolved))
            return

        if suffix in {".yml", ".yaml"}:
            self._walk_yaml(resolved, parent_title=title, depth=depth + 1, entries=entries)

    def _extract_links(self, node: Any) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                title = str(value.get("title") or value.get("name") or value.get("text") or "").strip()
                for key in ("href", "url", "homepage"):
                    href = value.get(key)
                    if isinstance(href, str) and href.strip():
                        links.append((title or self._title_from_href(href), href))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(node)
        return links

    @staticmethod
    def _is_external(href: str) -> bool:
        lower = href.lower()
        return lower.startswith(("http://", "https://", "mailto:", "tel:")) or href.startswith("/")

    def _resolve_local_path(self, href: str, base_dir: Path) -> Path | None:
        clean = href.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean:
            return None

        candidate = (base_dir / clean).resolve()
        if candidate.exists():
            return candidate

        repo_candidate = (self.repo_root / clean.lstrip("./")).resolve()
        if repo_candidate.exists():
            return repo_candidate

        return None

    def _is_in_section(self, path: Path) -> bool:
        if not self.section:
            return True
        try:
            path.relative_to(self.section)
            return True
        except ValueError:
            return False

    @staticmethod
    def _title_from_path(path: Path) -> str:
        return path.stem.replace("-", " ").replace("_", " ").strip().title() or "Untitled"

    @staticmethod
    def _title_from_href(href: str) -> str:
        name = href.split("#", 1)[0].strip("/")
        if not name:
            return "Untitled"
        return Path(name).stem.replace("-", " ").replace("_", " ").title()

    @staticmethod
    def _load_yaml(path: Path) -> Any:
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return None


class MarkdownPreprocessor:
    INCLUDE_PATTERN = re.compile(r"\[!INCLUDE\s*\[[^\]]*\]\(([^)]+)\)\]", re.IGNORECASE)
    CODE_PATTERN = re.compile(r"^\s*:::\s*code\s+([^\n]*?):::\s*$", re.IGNORECASE | re.MULTILINE)
    XREF_TAG_PATTERN = re.compile(r"<xref:([^>]+)>", re.IGNORECASE)
    LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    def __init__(self, repo_root: Path, include_code: bool, strip_xref: bool) -> None:
        self.repo_root = repo_root
        self.include_code = include_code
        self.strip_xref = strip_xref

    def preprocess(self, doc_path: Path) -> tuple[str, str | None]:
        raw = doc_path.read_text(encoding="utf-8", errors="replace")
        metadata, body = self._split_frontmatter(raw)

        body = self._resolve_includes(body, doc_path, include_stack={doc_path.resolve()})
        body = self._replace_code_directives(body, doc_path)
        body = self._strip_docfx_directives(body)
        body = self._normalize_links(body)

        if self.strip_xref:
            body = self._strip_xref_markup(body)

        return body.strip() + "\n", metadata.get("title") if metadata else None

    def _split_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---\n"):
            return {}, text

        end = text.find("\n---\n", 4)
        if end == -1:
            return {}, text

        frontmatter = text[4:end]
        body = text[end + 5 :]

        try:
            meta = yaml.safe_load(frontmatter) or {}
            if not isinstance(meta, dict):
                return {}, body
            return meta, body
        except Exception:
            return {}, body

    def _resolve_includes(self, text: str, doc_path: Path, include_stack: set[Path]) -> str:
        def replace(match: re.Match[str]) -> str:
            target = match.group(1).strip()
            include_path = self._resolve_doc_relative_path(target, doc_path.parent)
            if not include_path or not include_path.exists() or include_path.is_dir():
                return ""

            include_real = include_path.resolve()
            if include_real in include_stack:
                return ""

            include_stack.add(include_real)
            include_raw = include_path.read_text(encoding="utf-8", errors="replace")
            _, include_body = self._split_frontmatter(include_raw)
            resolved = self._resolve_includes(include_body, include_path, include_stack)
            include_stack.remove(include_real)
            return resolved

        return self.INCLUDE_PATTERN.sub(replace, text)

    def _replace_code_directives(self, text: str, doc_path: Path) -> str:
        def replace(match: re.Match[str]) -> str:
            attrs = self._parse_attrs(match.group(1))
            source = attrs.get("source")
            language = attrs.get("language", "text")
            snippet_id = attrs.get("id")

            if not source:
                return ""

            source_path = self._resolve_doc_relative_path(source, doc_path.parent)
            if not source_path or not source_path.exists() or source_path.is_dir():
                return ""

            if not self.include_code:
                return f"\n```{language}\n[Snippet omitted: {source_path.as_posix()}]\n```\n"

            snippet_text = source_path.read_text(encoding="utf-8", errors="replace")
            extracted = self._extract_snippet_region(snippet_text, snippet_id) if snippet_id else snippet_text
            return f"\n```{language}\n{extracted.strip()}\n```\n"

        return self.CODE_PATTERN.sub(replace, text)

    @staticmethod
    def _parse_attrs(attr_string: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for key, value in re.findall(r"(\w+)\s*=\s*\"([^\"]*)\"", attr_string):
            attrs[key.lower()] = value
        return attrs

    @staticmethod
    def _extract_snippet_region(text: str, snippet_id: str) -> str:
        lines = text.splitlines()

        region_start = re.compile(rf"#region\s+{re.escape(snippet_id)}\b", re.IGNORECASE)
        region_end = re.compile(r"#endregion\b", re.IGNORECASE)

        tag_start = re.compile(rf"<\s*{re.escape(snippet_id)}\s*>", re.IGNORECASE)
        tag_end = re.compile(rf"<\s*/\s*{re.escape(snippet_id)}\s*>", re.IGNORECASE)

        for start_re, end_re in ((region_start, region_end), (tag_start, tag_end)):
            start_idx = None
            for i, line in enumerate(lines):
                if start_idx is None and start_re.search(line):
                    start_idx = i + 1
                    continue
                if start_idx is not None and end_re.search(line):
                    return "\n".join(lines[start_idx:i])

        return text

    def _strip_docfx_directives(self, text: str) -> str:
        out: list[str] = []
        zone_depth = 0
        moniker_depth = 0

        for line in text.splitlines():
            if re.match(r"^\s*:::\s*zone\b", line, flags=re.IGNORECASE):
                zone_depth += 1
                continue
            if re.match(r"^\s*:::\s*zone-end\b", line, flags=re.IGNORECASE):
                zone_depth = max(0, zone_depth - 1)
                continue
            if re.match(r"^\s*:::\s*moniker\b", line, flags=re.IGNORECASE):
                moniker_depth += 1
                continue
            if re.match(r"^\s*:::\s*moniker-end\b", line, flags=re.IGNORECASE):
                moniker_depth = max(0, moniker_depth - 1)
                continue
            if re.match(r"^\s*:::\s*image\b", line, flags=re.IGNORECASE):
                continue

            if zone_depth > 0:
                continue

            out.append(line)

        return "\n".join(out)

    def _strip_xref_markup(self, text: str) -> str:
        text = self.XREF_TAG_PATTERN.sub(lambda m: self._xref_to_text(m.group(1)), text)

        def replace_link(match: re.Match[str]) -> str:
            label, url = match.group(1), match.group(2)
            if url.lower().startswith("xref:"):
                return label or self._xref_to_text(url.split(":", 1)[1])
            return match.group(0)

        return self.LINK_PATTERN.sub(replace_link, text)

    def _normalize_links(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            label, url = match.group(1), match.group(2).strip()
            if url.startswith(("#", "http://", "https://", "mailto:", "tel:")):
                return match.group(0)
            if url.lower().startswith("xref:"):
                return match.group(0)
            clean = url.split("#", 1)[0].split("?", 1)[0]
            if clean.lower().endswith(".md") or clean.lower().endswith(".yml"):
                return label
            return match.group(0)

        return self.LINK_PATTERN.sub(replace, text)

    def _resolve_doc_relative_path(self, value: str, base_dir: Path) -> Path | None:
        clean = value.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean:
            return None

        candidate = (base_dir / clean).resolve()
        if candidate.exists():
            return candidate

        repo_candidate = (self.repo_root / clean.lstrip("./")).resolve()
        if repo_candidate.exists():
            return repo_candidate

        return None

    @staticmethod
    def _xref_to_text(value: str) -> str:
        token = value.strip().strip("`")
        if "/" in token:
            token = token.rsplit("/", 1)[-1]
        if token.startswith("T:") or token.startswith("M:"):
            token = token[2:]
        return token


def slugify(text: str, used: dict[str, int]) -> str:
    slug = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    slug = slug or "section"
    count = used[slug]
    used[slug] += 1
    if count:
        return f"{slug}-{count}"
    return slug


def assemble(entries: list[DocEntry], preprocessor: MarkdownPreprocessor, output_path: Path, title: str) -> None:
    used_slugs: dict[str, int] = defaultdict(int)
    toc_lines = ["## Table of contents", ""]
    body_parts = [f"# {title}", "", "_Generated by `tools/combine-docs.py`._", ""]

    section_records: list[tuple[str, str]] = []
    doc_sections: list[str] = []

    for idx, entry in enumerate(entries, start=1):
        content, meta_title = preprocessor.preprocess(entry.path)
        heading = meta_title or entry.title or entry.path.stem
        anchor = slugify(f"{idx}-{heading}", used_slugs)
        section_heading = f"## {idx}. {heading}"
        section_records.append((section_heading, anchor))

        relative_path = entry.path.as_posix()
        try:
            relative_path = entry.path.relative_to(output_path.parent).as_posix()
        except ValueError:
            pass
        doc_lines = ["---", "", f'<a id="{anchor}"></a>', section_heading, f"<!-- {relative_path} -->", ""]

        if not re.search(r"^\s*#\s+", content, flags=re.MULTILINE):
            doc_lines.append(f"# {heading}")
            doc_lines.append("")

        doc_lines.append(content.rstrip())
        doc_sections.append("\n".join(doc_lines))

    for heading, anchor in section_records:
        toc_lines.append(f"- [{heading}](#{anchor})")

    body_parts.extend(toc_lines)
    body_parts.append("")
    body_parts.extend(doc_sections)
    body_parts.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(body_parts), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Combine .NET docs markdown into one file.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root path (default: current directory)",
    )
    parser.add_argument(
        "--section",
        type=Path,
        default=None,
        help="Optional section path (example: docs/csharp)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("combined-docs.md"),
        help="Output markdown file path",
    )
    parser.add_argument(
        "--include-code",
        action="store_true",
        help="Expand :::code directives into fenced code blocks",
    )

    xref_group = parser.add_mutually_exclusive_group()
    xref_group.add_argument("--strip-xref", action="store_true", default=True, help="Strip xref markup")
    xref_group.add_argument("--keep-xref", action="store_true", help="Keep xref markup")

    parser.add_argument(
        "--title",
        default="Combined .NET documentation",
        help="Output document title",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    repo_root = args.repo_root.resolve()
    docs_root = repo_root / "docs"

    if not docs_root.exists():
        raise SystemExit(f"Docs root not found: {docs_root}")

    if args.section:
        if args.section.is_absolute():
            section = args.section.resolve()
        else:
            section = (repo_root / args.section).resolve()
    else:
        section = None
    output_path = args.output if args.output.is_absolute() else (repo_root / args.output)

    strip_xref = args.strip_xref and not args.keep_xref

    walker = TocWalker(repo_root=repo_root, docs_root=docs_root, section=section)
    entries = walker.build()

    if not entries:
        raise SystemExit("No markdown documents resolved from TOC/section.")

    preprocessor = MarkdownPreprocessor(repo_root=repo_root, include_code=args.include_code, strip_xref=strip_xref)
    assemble(entries=entries, preprocessor=preprocessor, output_path=output_path, title=args.title)

    print(f"Wrote {len(entries)} documents to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
