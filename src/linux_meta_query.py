#!/usr/bin/env python3
"""
Query VS Code C/C++ browse metadata for a Linux tree, then enrich it with
source-level heuristics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


KIND = {
    2: "struct",
    3: "union",
    4: "enum",
    8: "enumerator",
    9: "parameter",
    21: "typedef",
    27: "function",
    28: "variable",
    37: "macro_define",
}

INTERESTING_KINDS = (2, 3, 4, 8, 21, 28, 37)
TYPE_DEP_KINDS = (2, 3, 4, 21)
CONTROL_WORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "typeof",
    "__typeof__",
    "__alignof__",
    "alignof",
    "_Generic",
    "case",
}
C_KEYWORDS = {
    "auto",
    "break",
    "case",
    "char",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extern",
    "float",
    "for",
    "goto",
    "if",
    "inline",
    "int",
    "long",
    "register",
    "restrict",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "struct",
    "switch",
    "typedef",
    "union",
    "unsigned",
    "void",
    "volatile",
    "while",
    "__user",
    "__iomem",
    "__init",
    "__exit",
    "__maybe_unused",
}


@dataclass
class CodeItem:
    id: int
    kind: str
    name: str
    type: str | None
    attributes: int
    file: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    parent_id: int

    @property
    def span(self) -> int:
        return max(0, self.end_line - self.start_line)


@dataclass
class CallSite:
    line: int
    expression: str
    callee: str
    resolved_file: str | None = None
    resolved_line: int | None = None


@dataclass
class CallerSite:
    caller: CodeItem
    line: int
    text: str


@dataclass
class ParamReport:
    name: str
    type: str | None
    inferred: list[str]
    evidence: list[str]


def clean_type(value: str | None, name: str | None = None) -> str | None:
    if value is None:
        return None
    marker = name or ""
    cleaned = (
        value.replace("* \x01", "*" + marker)
        .replace("& \x01", "&" + marker)
        .replace("\x01", marker)
        .replace("\x02", "")
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def open_db(db_path: Path) -> sqlite3.Connection:
    uri = "file:" + db_path.as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_repo_and_db(repo_arg: str | Path, db_arg: str | Path | None) -> tuple[Path, Path]:
    repo = Path(repo_arg)
    if not repo.is_absolute():
        repo = (Path.cwd() / repo).resolve()

    if db_arg:
        db_path = Path(db_arg)
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()
        if db_path.is_dir():
            direct = db_path / "BROWSE.VC.DB"
            nested = db_path / ".vscode" / "BROWSE.VC.DB"
            db_path = direct if direct.exists() else nested

        if db_path.parent.name.lower() == ".vscode":
            repo = db_path.parent.parent
        elif db_path.name.upper() == "BROWSE.VC.DB":
            repo = db_path.parent
    else:
        db_path = repo / ".vscode" / "BROWSE.VC.DB"

    if not db_path.exists():
        raise SystemExit(f"SQLite DB not found: {db_path}")
    if not repo.exists():
        raise SystemExit(f"Source repo root not found: {repo}")
    return repo, db_path


def row_to_item(row: sqlite3.Row) -> CodeItem:
    return CodeItem(
        id=row["id"],
        kind=KIND.get(row["kind"], str(row["kind"])),
        name=row["name"],
        type=clean_type(row["type"], row["name"]),
        attributes=row["attributes"],
        file=row["file_name"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        start_column=row["start_column"],
        end_column=row["end_column"],
        parent_id=row["parent_id"],
    )


def item_query(where: str) -> str:
    return f"""
        select ci.*, f.name as file_name
        from code_items ci
        join files f on f.id = ci.file_id
        where {where}
    """


def find_functions(
    con: sqlite3.Connection, name: str, file_filter: str | None = None
) -> list[CodeItem]:
    params: list[object] = [name]
    where = "ci.kind = 27 and ci.name = ?"
    if file_filter:
        file_filter = file_filter.replace("/", "\\").replace("\\\\", "\\")
        where += " and lower(f.name) like ?"
        params.append("%" + file_filter.lower() + "%")
    rows = con.execute(
        item_query(where)
        + " order by (ci.end_line - ci.start_line) desc, ci.start_line",
        params,
    ).fetchall()
    return [row_to_item(row) for row in rows]


def function_params(con: sqlite3.Connection, function_id: int) -> list[CodeItem]:
    rows = con.execute(
        item_query("ci.parent_id = ? and ci.kind = 9")
        + " order by ci.param_number, ci.start_line, ci.start_column",
        (function_id,),
    ).fetchall()
    return [row_to_item(row) for row in rows]


def read_lines(path_text: str) -> list[str]:
    path = Path(path_text)
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def source_slice(item: CodeItem, max_lines: int | None = None) -> str:
    lines = read_lines(item.file)
    start = max(1, item.start_line)
    end = max(start, item.end_line)
    if max_lines is not None and end - start + 1 > max_lines:
        end = start + max_lines - 1
    return "\n".join(lines[start - 1 : end])


def numbered_slice(item: CodeItem, max_lines: int | None = None) -> list[str]:
    lines = read_lines(item.file)
    start = max(1, item.start_line)
    end = max(start, item.end_line)
    truncated = False
    if max_lines is not None and end - start + 1 > max_lines:
        end = start + max_lines - 1
        truncated = True
    out = [f"{lineno}: {lines[lineno - 1]}" for lineno in range(start, end + 1)]
    if truncated:
        out.append("... <truncated>")
    return out


def strip_comments_and_strings(source: str) -> str:
    out: list[str] = []
    i = 0
    n = len(source)
    in_block = False
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_block:
            if ch == "*" and nxt == "/":
                out.extend("  ")
                i += 2
                in_block = False
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            out.extend("  ")
            i += 2
            in_block = True
            continue
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                c = source[i]
                out.append("\n" if c == "\n" else " ")
                if c == "\\":
                    i += 2
                    if i <= n:
                        out.append(" ")
                    continue
                i += 1
                if c == quote:
                    break
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokens_from_source(source: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"\b[A-Za-z_]\w*\b", strip_comments_and_strings(source))
        if tok not in C_KEYWORDS
    }


def discover_local_names(source: str) -> set[str]:
    names: set[str] = set()
    names.update(re.findall(r"\bCLASS\s*\(\s*[A-Za-z_]\w*\s*,\s*([A-Za-z_]\w*)", source))
    clean_lines = strip_comments_and_strings(source).splitlines()
    declaration = re.compile(
        r"^\s*(?:static\s+|const\s+|unsigned\s+|signed\s+|volatile\s+|struct\s+\w+\s+|union\s+\w+\s+|enum\s+\w+\s+|[A-Za-z_]\w+\s+)+"
        r"\*?\s*(?P<name>[A-Za-z_]\w*)\s*(?:\[|=|;|,)"
    )
    for idx, line in enumerate(clean_lines):
        if idx == 0:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("if", "for", "while", "switch", "return", "else")):
            continue
        match = declaration.match(line)
        if match:
            names.add(match.group("name"))
    return names


def chunked(values: list[str], size: int = 500) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def score_item(item: CodeItem, function_item: CodeItem, source_text: str) -> tuple[int, int]:
    path = item.file.lower()
    func_path = function_item.file.lower()
    score = 0
    if path == func_path:
        if item.kind in ("struct", "union", "enum", "typedef"):
            score += 100
        else:
            score += 1000
    if "\\include\\" in path or "/include/" in path:
        score += 80
    if item.parent_id == 1024:
        score += 40
    if item.attributes & 2:
        score += 500
    else:
        score -= 30
    if item.span > 0:
        score += min(item.span, 200)
    if item.kind == "macro_define" and re.search(rf"\b{re.escape(item.name)}\s*\(", source_text):
        score -= 40
    return (score, item.span)


def lookup_items_for_tokens(
    con: sqlite3.Connection,
    tokens: set[str],
    function_item: CodeItem,
    source_text: str,
    kinds: tuple[int, ...] = INTERESTING_KINDS,
) -> list[CodeItem]:
    if not tokens:
        return []
    found: list[CodeItem] = []
    token_list = sorted(tokens)
    kind_placeholders = ",".join("?" for _ in kinds)
    for names in chunked(token_list):
        placeholders = ",".join("?" for _ in names)
        rows = con.execute(
            item_query(f"ci.kind in ({kind_placeholders}) and ci.name in ({placeholders})"),
            (*kinds, *names),
        ).fetchall()
        found.extend(row_to_item(row) for row in rows)

    best_by_name_kind: dict[tuple[str, str], CodeItem] = {}
    for item in found:
        key = (item.name, item.kind)
        old = best_by_name_kind.get(key)
        if old is None or score_item(item, function_item, source_text) > score_item(
            old, function_item, source_text
        ):
            best_by_name_kind[key] = item
    return sorted(
        best_by_name_kind.values(),
        key=lambda item: (-score_item(item, function_item, source_text)[0], item.kind, item.name),
    )


def classify_dependencies(
    items: list[CodeItem], ignored_names: set[str], type_names: set[str]
) -> dict[str, list[CodeItem]]:
    groups = {
        "structures": [],
        "typedefs": [],
        "enums": [],
        "constants": [],
        "global_variables": [],
        "static_variables": [],
    }
    seen_static: set[int] = set()
    macro_names = {item.name for item in items if item.kind == "macro_define"}
    for item in items:
        if item.name in ignored_names and item.name not in type_names:
            continue
        if item.kind in ("struct", "union"):
            groups["structures"].append(item)
        elif item.kind == "typedef":
            groups["typedefs"].append(item)
        elif item.kind in ("enum", "enumerator"):
            groups["enums"].append(item)
            if item.kind == "enumerator":
                groups["constants"].append(item)
        elif item.kind == "macro_define":
            groups["constants"].append(item)
        elif item.kind == "variable":
            if item.name in ignored_names or item.name in macro_names:
                continue
            line = ""
            try:
                line = source_slice(item, max_lines=1)
            except OSError:
                pass
            if re.search(r"\bstatic\b", line):
                groups["static_variables"].append(item)
                seen_static.add(item.id)
            else:
                groups["global_variables"].append(item)
    groups["global_variables"] = [
        item for item in groups["global_variables"] if item.id not in seen_static
    ]
    return groups


def extract_type_dependency_tokens(source: str) -> set[str]:
    clean = strip_comments_and_strings(source)
    tokens = tokens_from_source(clean)
    tokens.update(re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b", clean))
    tokens.difference_update(C_KEYWORDS)
    return tokens


def extract_tag_type_tokens(source: str) -> set[str]:
    clean = strip_comments_and_strings(source)
    return set(re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b", clean))


def lookup_nested_type_items(
    con: sqlite3.Connection,
    source: str,
    function_item: CodeItem,
) -> list[CodeItem]:
    tag_tokens = extract_tag_type_tokens(source)
    typedef_tokens = tokens_from_source(source)
    typedef_tokens.difference_update(tag_tokens)

    found: list[CodeItem] = []
    if tag_tokens:
        found.extend(
            lookup_items_for_tokens(
                con,
                tag_tokens,
                function_item,
                source,
                kinds=(2, 3, 4),
            )
        )
    if typedef_tokens:
        found.extend(
            lookup_items_for_tokens(
                con,
                typedef_tokens,
                function_item,
                source,
                kinds=(21,),
            )
        )

    seen: set[int] = set()
    unique: list[CodeItem] = []
    for item in found:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique


def append_unique_dependency(
    groups: dict[str, list[CodeItem]], item: CodeItem, seen_ids: set[int]
) -> bool:
    if item.id in seen_ids:
        return False
    if item.kind in ("struct", "union"):
        groups["structures"].append(item)
    elif item.kind == "typedef":
        groups["typedefs"].append(item)
    elif item.kind == "enum":
        groups["enums"].append(item)
    else:
        return False
    seen_ids.add(item.id)
    return True


def expand_nested_type_dependencies(
    con: sqlite3.Connection,
    dependencies: dict[str, list[CodeItem]],
    function_item: CodeItem,
    max_depth: int,
) -> dict[str, list[CodeItem]]:
    if max_depth <= 0:
        return dependencies

    expanded = {name: list(items) for name, items in dependencies.items()}
    seen_ids = {item.id for items in expanded.values() for item in items}
    queue: list[tuple[CodeItem, int]] = [
        (item, 0)
        for group_name in ("structures", "typedefs", "enums")
        for item in expanded[group_name]
    ]

    while queue:
        item, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            item_source = source_slice(item)
        except OSError:
            continue
        nested_items = [
            nested
            for nested in lookup_nested_type_items(con, item_source, function_item)
            if nested.name != item.name
        ]
        for nested in nested_items:
            if nested.id == item.id:
                continue
            if append_unique_dependency(expanded, nested, seen_ids):
                queue.append((nested, depth + 1))
    return expanded


CALL_RE = re.compile(
    r"(?<![\w])(?P<expr>[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*\("
)
_FILE_ID_CACHE: dict[str, int | None] = {}


def discover_calls(
    con: sqlite3.Connection, function_item: CodeItem, source: str, include_macros: bool = True
) -> list[CallSite]:
    clean = strip_comments_and_strings(source)
    calls: list[CallSite] = []
    for offset, line in enumerate(clean.splitlines(), start=function_item.start_line):
        for match in CALL_RE.finditer(line):
            expr = re.sub(r"\s+", "", match.group("expr"))
            callee = re.split(r"->|\.", expr)[-1]
            if callee in CONTROL_WORDS:
                continue
            if callee == function_item.name and offset == function_item.start_line:
                continue
            if not include_macros and callee.isupper():
                continue
            resolved = None
            if "->" not in expr and "." not in expr:
                resolved = resolve_function(con, callee, function_item.file)
            calls.append(
                CallSite(
                    line=offset,
                    expression=expr,
                    callee=callee,
                    resolved_file=resolved.file if resolved else None,
                    resolved_line=resolved.start_line if resolved else None,
                )
            )
    return calls


def resolve_function(
    con: sqlite3.Connection, name: str, caller_file: str | None = None
) -> CodeItem | None:
    rows = con.execute(
        item_query("ci.kind = 27 and ci.name = ?")
        + " order by (ci.end_line - ci.start_line) desc",
        (name,),
    ).fetchall()
    if not rows:
        return None
    items = [row_to_item(row) for row in rows]
    caller_lower = (caller_file or "").lower()

    def score(candidate: CodeItem) -> tuple[int, int]:
        path = candidate.file.lower()
        value = candidate.span
        if caller_lower and path == caller_lower:
            value += 1000
        caller_is_tools = "\\tools\\" in caller_lower or "/tools/" in caller_lower
        candidate_is_tools = "\\tools\\" in path or "/tools/" in path
        if caller_lower and not caller_is_tools:
            value += -300 if candidate_is_tools else 300
        if candidate.attributes & 2:
            value += 100
        if candidate.span == 0:
            value -= 100
        return (value, candidate.span)

    return max(items, key=score)


def find_file_id(con: sqlite3.Connection, file_path: str) -> int | None:
    normalized = str(Path(file_path).resolve()).upper()
    if normalized in _FILE_ID_CACHE:
        return _FILE_ID_CACHE[normalized]
    row = con.execute("select id from files where name = ?", (normalized,)).fetchone()
    file_id = int(row["id"]) if row else None
    _FILE_ID_CACHE[normalized] = file_id
    return file_id


def find_enclosing_function(con: sqlite3.Connection, file_path: str, line: int) -> CodeItem | None:
    file_id = find_file_id(con, file_path)
    if file_id is None:
        return None
    rows = con.execute(
        item_query("ci.kind = 27 and ci.file_id = ? and ci.start_line <= ? and ci.end_line >= ?")
        + " order by (ci.end_line - ci.start_line) asc limit 1",
        (file_id, line, line),
    ).fetchall()
    if not rows:
        return None
    return row_to_item(rows[0])


def line_has_call(line: str, callee_name: str) -> bool:
    clean = strip_comments_and_strings(line)
    return re.search(rf"\b{re.escape(callee_name)}\s*\(", clean) is not None


def function_line_has_call(function_item: CodeItem, line: int, callee_name: str) -> bool:
    try:
        clean_source = strip_comments_and_strings(source_slice(function_item))
    except OSError:
        return False
    offset = line - function_item.start_line
    clean_lines = clean_source.splitlines()
    if offset < 0 or offset >= len(clean_lines):
        return False
    return re.search(rf"\b{re.escape(callee_name)}\s*\(", clean_lines[offset]) is not None


def iter_call_matches_with_rg(repo: Path, callee_name: str) -> list[tuple[str, int, str]]:
    if shutil.which("rg") is None:
        return []
    pattern = rf"\b{re.escape(callee_name)}\s*\("
    cmd = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--glob",
        "*.c",
        "--glob",
        "*.h",
        "--glob",
        "*.S",
        pattern,
        str(repo),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return []
    if proc.returncode not in (0, 1):
        return []
    matches: list[tuple[str, int, str]] = []
    for raw in proc.stdout.splitlines():
        match = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not match:
            continue
        path_text, line_text, source_line = match.groups()
        if line_has_call(source_line, callee_name):
            matches.append((path_text, int(line_text), source_line))
    return matches


def iter_call_matches_multi_with_rg(
    repo: Path, callee_names: set[str], chunk_size: int = 80
) -> list[tuple[str, str, int, str]]:
    rg_path = shutil.which("rg")
    if rg_path is None or not callee_names:
        return []
    matches: list[tuple[str, str, int, str]] = []
    names = sorted(callee_names, key=lambda value: (-len(value), value))
    for name_chunk in chunked(names, chunk_size):
        alternation = "|".join(re.escape(name) for name in name_chunk)
        pattern = rf"\b(?:{alternation})\s*\("
        cmd = [
            rg_path,
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--glob",
            "*.c",
            "--glob",
            "*.h",
            "--glob",
            "*.S",
            pattern,
            str(repo),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue
        if proc.returncode not in (0, 1):
            continue
        line_call_re = re.compile(rf"\b({alternation})\s*\(")
        for raw in proc.stdout.splitlines():
            match = re.match(r"^(.*?):(\d+):(.*)$", raw)
            if not match:
                continue
            path_text, line_text, source_line = match.groups()
            clean_line = strip_comments_and_strings(source_line)
            for call_match in line_call_re.finditer(clean_line):
                matches.append((call_match.group(1), path_text, int(line_text), source_line))
    return matches


def iter_call_matches_fallback(repo: Path, callee_name: str) -> list[tuple[str, int, str]]:
    pattern = re.compile(rf"\b{re.escape(callee_name)}\s*\(")
    matches: list[tuple[str, int, str]] = []
    for path in repo.rglob("*"):
        if path.suffix.lower() not in {".c", ".h", ".s"}:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if pattern.search(line) and line_has_call(line, callee_name):
                matches.append((str(path), line_no, line))
    return matches


def find_direct_callers(
    con: sqlite3.Connection,
    repo: Path,
    callee_name: str,
    target_ids: set[int] | None = None,
    max_callers: int = 100,
) -> list[CallerSite]:
    matches = iter_call_matches_with_rg(repo, callee_name)
    if not matches:
        matches = iter_call_matches_fallback(repo, callee_name)

    callers: dict[int, CallerSite] = {}
    for path_text, line, source_line in matches:
        caller = find_enclosing_function(con, path_text, line)
        if caller is None:
            continue
        if not function_line_has_call(caller, line, callee_name):
            continue
        if target_ids and caller.id in target_ids:
            continue
        if caller.name == callee_name:
            continue
        old = callers.get(caller.id)
        if old is None or line < old.line:
            callers[caller.id] = CallerSite(caller=caller, line=line, text=source_line.strip())

    return sorted(
        callers.values(),
        key=lambda site: (site.caller.file.lower(), site.line, site.caller.name),
    )[:max_callers]


def find_direct_callers_multi(
    con: sqlite3.Connection,
    repo: Path,
    callee_names: set[str],
    max_callers_per_name: int = 100,
) -> dict[str, list[CallerSite]]:
    grouped: dict[str, dict[int, CallerSite]] = {name: {} for name in callee_names}
    matches = iter_call_matches_multi_with_rg(repo, callee_names)
    if not matches:
        for name in callee_names:
            for path_text, line, source_line in iter_call_matches_fallback(repo, name):
                matches.append((name, path_text, line, source_line))

    function_call_cache: dict[tuple[int, int, str], bool] = {}
    for callee_name, path_text, line, _source_line in matches:
        caller = find_enclosing_function(con, path_text, line)
        if caller is None or caller.name == callee_name:
            continue
        cache_key = (caller.id, line, callee_name)
        valid = function_call_cache.get(cache_key)
        if valid is None:
            valid = function_line_has_call(caller, line, callee_name)
            function_call_cache[cache_key] = valid
        if not valid:
            continue
        old = grouped.setdefault(callee_name, {}).get(caller.id)
        if old is None or line < old.line:
            grouped[callee_name][caller.id] = CallerSite(
                caller=caller,
                line=line,
                text="",
            )

    result: dict[str, list[CallerSite]] = {}
    for name, callers in grouped.items():
        result[name] = sorted(
            callers.values(),
            key=lambda site: (site.caller.file.lower(), site.line, site.caller.name),
        )[:max_callers_per_name]
    return result


def build_upstream_call_chains(
    con: sqlite3.Connection,
    repo: Path,
    target: CodeItem,
    max_depth: int,
    max_chains: int,
    max_callers_per_level: int,
) -> list[list[CallerSite]]:
    direct_target = CallerSite(caller=target, line=target.start_line, text="")
    paths: list[list[CallerSite]] = [[direct_target]]
    terminal_paths: list[list[CallerSite]] = []
    caller_cache: dict[str, list[CallerSite]] = {}

    for _depth in range(max_depth):
        expandable = [path for path in paths if len(path) <= max_depth]
        if not expandable:
            break
        needed_names = {path[0].caller.name for path in expandable if path[0].caller.name not in caller_cache}
        if needed_names:
            caller_cache.update(
                find_direct_callers_multi(
                    con,
                    repo,
                    needed_names,
                    max_callers_per_name=max_callers_per_level,
                )
            )

        next_paths: list[list[CallerSite]] = []
        extended_any = False
        for path in paths:
            top_name = path[0].caller.name
            seen_ids = {site.caller.id for site in path}
            callers = [
                site
                for site in caller_cache.get(top_name, [])
                if site.caller.id not in seen_ids
            ]
            if not callers:
                terminal_paths.append(path)
                continue
            extended_any = True
            for site in callers:
                next_paths.append([site, *path])
                if len(next_paths) >= max_chains:
                    break
            if len(next_paths) >= max_chains:
                break
        paths = next_paths[:max_chains]
        if not extended_any:
            break

    all_paths = terminal_paths + paths
    unique: list[list[CallerSite]] = []
    seen_paths: set[tuple[int, ...]] = set()
    for chain in all_paths:
        key = tuple(site.caller.id for site in chain)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        unique.append(chain)
    return unique[:max_chains]


def print_upstream_call_chains(
    con: sqlite3.Connection,
    repo: Path,
    target: CodeItem,
    max_depth: int,
    max_chains: int,
    max_callers_per_level: int,
) -> None:
    chains = build_upstream_call_chains(
        con,
        repo,
        target,
        max_depth=max_depth,
        max_chains=max_chains,
        max_callers_per_level=max_callers_per_level,
    )
    print(
        f"Target: {target.name} ({relpath(target.file)}:{target.start_line}-{target.end_line})"
    )
    if not chains:
        print("No upstream callers found.")
        return
    for idx, chain in enumerate(chains, start=1):
        print(f"{idx}. " + " -> ".join(site.caller.name for site in chain))
        detail = " | ".join(
            f"{site.caller.name}@{relpath(site.caller.file)}:{site.line}"
            for site in chain[:-1]
        )
        if detail:
            print(f"   {detail}")


def infer_param_constraints(
    function_item: CodeItem, params: list[CodeItem], source: str
) -> list[ParamReport]:
    reports: list[ParamReport] = []
    lines = source.splitlines()
    clean_lines = strip_comments_and_strings(source).splitlines()
    for param in params:
        name = param.name
        inferred: list[str] = []
        evidence: list[str] = []
        ptype = param.type
        if not name:
            reports.append(ParamReport(name="<anonymous>", type=ptype, inferred=[], evidence=[]))
            continue
        if ptype:
            if "*" in ptype:
                inferred.append("指针参数；需要调用者提供有效地址，除非函数显式允许 NULL。")
            if "__user" in ptype:
                inferred.append("用户态指针；通常需要配合 access_ok/copy_*_user 等检查。")
            if ptype.strip().startswith("const ") or " const " in ptype:
                inferred.append("只读语义；函数不应修改该入参指向的数据。")

        for idx, clean_line in enumerate(clean_lines):
            if not re.search(rf"\b{re.escape(name)}\b", clean_line):
                continue
            original = lines[idx].rstrip()
            lineno = function_item.start_line + idx
            interesting = False
            if re.search(r"\b(if|while|WARN_ON|BUG_ON|BUILD_BUG_ON|likely|unlikely)\b", clean_line):
                interesting = True
                cond = clean_line.strip()
                if re.search(rf"!\s*{re.escape(name)}\b", cond) or re.search(
                    rf"\b{re.escape(name)}\b\s*==\s*NULL", cond
                ):
                    inferred.append("存在 NULL/空值检查。")
                if re.search(rf"\b{re.escape(name)}\b\s*(?:[<>]=?|==|!=)", cond):
                    inferred.append("存在数值或状态比较约束。")
            if re.search(rf"\baccess_ok\s*\([^;]*\b{re.escape(name)}\b", clean_line):
                interesting = True
                inferred.append("通过 access_ok 校验可访问范围。")
            if re.search(rf"\bcopy_(?:to|from)_user\s*\([^;]*\b{re.escape(name)}\b", clean_line):
                interesting = True
                inferred.append("参与 copy_to_user/copy_from_user，入参格式受用户态缓冲区约束。")
            if idx > 0 and re.search(rf"\b{re.escape(name)}\b\s*=", clean_line):
                interesting = True
                inferred.append("函数内部会重写该参数的局部值。")
            if idx > 0 and (
                re.search(rf"\b{re.escape(name)}\s*->", clean_line) or re.search(
                rf"\*\s*{re.escape(name)}\b", clean_line
                )
            ):
                interesting = True
                inferred.append("函数会解引用该参数。")
            if interesting:
                evidence.append(f"{lineno}: {original}")

        deduped: list[str] = []
        for text in inferred:
            if text not in deduped:
                deduped.append(text)
        reports.append(ParamReport(name=name, type=ptype, inferred=deduped, evidence=evidence[:20]))
    return reports


def item_to_dict(item: CodeItem, max_snippet_lines: int) -> dict[str, object]:
    data = asdict(item)
    try:
        data["snippet"] = source_slice(item, max_lines=max_snippet_lines)
    except OSError as exc:
        data["snippet_error"] = str(exc)
    return data


def analyze(args: argparse.Namespace) -> dict[str, object]:
    _repo, db_path = resolve_repo_and_db(args.repo, args.db)
    con = open_db(db_path)
    try:
        candidates = find_functions(con, args.function, args.file)
        if not candidates:
            raise SystemExit(f"未找到函数: {args.function}")
        function_item = candidates[0]
        params = function_params(con, function_item.id)
        source = source_slice(function_item)
        tokens = tokens_from_source(source)
        param_names = {param.name for param in params if param.name}
        member_names = set(
            re.findall(r"(?:->|\.)\s*([A-Za-z_]\w*)\b", strip_comments_and_strings(source))
        )
        call_names = {
            re.split(r"->|\.", re.sub(r"\s+", "", match.group("expr")))[-1]
            for match in CALL_RE.finditer(strip_comments_and_strings(source))
        }
        local_names = param_names | discover_local_names(source) | member_names | call_names
        type_names = set()
        if function_item.type:
            type_names.update(tokens_from_source(function_item.type))
        for param in params:
            if param.type:
                param_type_tokens = tokens_from_source(param.type)
                tokens.update(param_type_tokens)
                type_names.update(param_type_tokens)
        type_names.difference_update(param_names)
        type_names.update(
            re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b", strip_comments_and_strings(source))
        )
        dependencies = classify_dependencies(
            lookup_items_for_tokens(con, tokens, function_item, source), local_names, type_names
        )
        if getattr(args, "expand_nested_types", False):
            dependencies = expand_nested_type_dependencies(
                con,
                dependencies,
                function_item,
                getattr(args, "max_nesting_depth", 4),
            )
        calls = discover_calls(con, function_item, source, include_macros=args.include_macros)
        return {
            "selected": asdict(function_item),
            "ambiguous_candidates": [asdict(item) for item in candidates[1: args.max_candidates]],
            "parameters": [asdict(item) for item in params],
            "source": source,
            "dependencies": {
                name: [item_to_dict(item, args.max_snippet_lines) for item in items[: args.max_deps]]
                for name, items in dependencies.items()
            },
            "calls": [asdict(call) for call in calls],
            "param_constraints": [asdict(r) for r in infer_param_constraints(function_item, params, source)],
            "notes": [
                "BROWSE.VC.DB 的 symbols/symbol_refs/symbol_relations 为空；调用序列和约束来自源码启发式分析。",
                "同名函数默认选择源码跨度最大的定义；可用 --file 缩小到指定路径。",
            ],
        }
    finally:
        con.close()


def analyze_function(
    function: str,
    *,
    repo: str = "linux-7.0",
    db: str | None = None,
    file_filter: str | None = None,
    include_macros: bool = True,
    max_deps: int = 20,
    max_candidates: int = 12,
    max_snippet_lines: int = 80,
    expand_nested_types: bool = False,
    max_nesting_depth: int = 4,
) -> dict[str, object]:
    """Return the complete analysis report for one function name."""
    args = argparse.Namespace(
        function=function,
        repo=repo,
        db=db,
        file=file_filter,
        include_macros=include_macros,
        max_deps=max_deps,
        max_candidates=max_candidates,
        max_snippet_lines=max_snippet_lines,
        expand_nested_types=expand_nested_types,
        max_nesting_depth=max_nesting_depth,
    )
    return analyze(args)


def export_source_bundle(
    function: str,
    output: str | Path | None = None,
    *,
    repo: str = "linux-7.0",
    db: str | None = None,
    file_filter: str | None = None,
    include_macros: bool = True,
    max_deps: int = 200,
    max_candidates: int = 12,
    max_snippet_lines: int = 80,
    max_nesting_depth: int = 4,
) -> Path:
    """API 1: write function source and related snippets into a .c file."""
    report = analyze_function(
        function,
        repo=repo,
        db=db,
        file_filter=file_filter,
        include_macros=include_macros,
        max_deps=max_deps,
        max_candidates=max_candidates,
        max_snippet_lines=max_snippet_lines,
        expand_nested_types=True,
        max_nesting_depth=max_nesting_depth,
    )
    output_path = Path(output) if output else default_bundle_path(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_c_bundle(report), encoding="utf-8")
    return output_path


def print_function_call_sequence(
    function: str,
    *,
    repo: str = "linux-7.0",
    db: str | None = None,
    file_filter: str | None = None,
    include_macros: bool = True,
    max_depth: int = 5,
    max_chains: int = 200,
    max_callers_per_level: int = 80,
    max_candidates: int = 12,
) -> None:
    """API 2: print upstream caller chains ending at the target function."""
    repo_path, db_path = resolve_repo_and_db(repo, db)
    con = open_db(db_path)
    try:
        candidates = find_functions(con, function, file_filter)
        if not candidates:
            raise SystemExit(f"未找到函数: {function}")
        target = candidates[0]
        print_upstream_call_chains(
            con,
            repo_path,
            target,
            max_depth=max_depth,
            max_chains=max_chains,
            max_callers_per_level=max_callers_per_level,
        )
    finally:
        con.close()


def print_function_param_constraints(
    function: str,
    *,
    repo: str = "linux-7.0",
    db: str | None = None,
    file_filter: str | None = None,
    include_macros: bool = True,
    max_deps: int = 20,
    max_candidates: int = 12,
    max_snippet_lines: int = 80,
) -> None:
    """API 3: print inferred parameter constraints for one function."""
    report = analyze_function(
        function,
        repo=repo,
        db=db,
        file_filter=file_filter,
        include_macros=include_macros,
        max_deps=max_deps,
        max_candidates=max_candidates,
        max_snippet_lines=max_snippet_lines,
    )
    print_param_constraints(report)


def api_export_source_bundle(args: argparse.Namespace) -> Path:
    return export_source_bundle(
        args.function,
        args.output,
        repo=args.repo,
        db=args.db,
        file_filter=args.file,
        include_macros=args.include_macros,
        max_deps=args.max_deps,
        max_candidates=args.max_candidates,
        max_snippet_lines=args.max_snippet_lines,
        max_nesting_depth=args.max_nesting_depth,
    )


def api_print_call_sequence(args: argparse.Namespace) -> None:
    print_function_call_sequence(
        args.function,
        repo=args.repo,
        db=args.db,
        file_filter=args.file,
        max_depth=args.max_depth,
        max_chains=args.max_chains,
        max_callers_per_level=args.max_callers_per_level,
        max_candidates=args.max_candidates,
    )


def api_print_param_constraints(args: argparse.Namespace) -> None:
    print_function_param_constraints(
        args.function,
        repo=args.repo,
        db=args.db,
        file_filter=args.file,
        include_macros=args.include_macros,
        max_deps=args.max_deps,
        max_candidates=args.max_candidates,
        max_snippet_lines=args.max_snippet_lines,
    )


def default_bundle_path(report: dict[str, object]) -> Path:
    selected = report["selected"]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(selected["name"]))
    return Path(f"{name}_source_bundle.c")


def render_c_bundle(report: dict[str, object]) -> str:
    selected = report["selected"]
    deps = report["dependencies"]
    lines: list[str] = []
    lines.append("/*")
    lines.append(" * Generated by tools/linux_meta_query.py source")
    lines.append(f" * Function: {selected['name']}")
    lines.append(
        f" * Location: {selected['file']}:{selected['start_line']}-{selected['end_line']}"
    )
    lines.append(" *")
    lines.append(" * This file is a source-analysis bundle. It is intended for review,")
    lines.append(" * not as a directly compilable Linux translation unit.")
    lines.append(" * Nested struct/union/enum/typedef dependencies are included recursively.")
    lines.append(" */")
    lines.append("")
    append_bundle_group(lines, "Constants and macros", deps["constants"])
    append_bundle_group(lines, "Typedefs", deps["typedefs"])
    append_bundle_group(lines, "Enums and enumerators", deps["enums"])
    append_bundle_group(lines, "Global variables", deps["global_variables"])
    append_bundle_group(lines, "Static variables", deps["static_variables"])
    append_bundle_group(lines, "Structures and unions", deps["structures"])
    lines.append("/* ===== Target function source ===== */")
    lines.append(f"/* {selected['file']}:{selected['start_line']}-{selected['end_line']} */")
    lines.append(str(report["source"]).rstrip())
    lines.append("")
    return "\n".join(lines)


def append_bundle_group(lines: list[str], title: str, items: list[dict[str, object]]) -> None:
    lines.append(f"/* ===== {title} ===== */")
    if not items:
        lines.append("/* not found */")
        lines.append("")
        return
    seen: set[tuple[str, str, int]] = set()
    for item in items:
        key = (str(item["file"]), str(item["name"]), int(item["start_line"]))
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"/* {item['kind']} {item['name']} - {item['file']}:{item['start_line']} */"
        )
        snippet = str(item.get("snippet", "")).rstrip()
        if snippet:
            lines.append(snippet)
        else:
            lines.append("/* source snippet unavailable */")
        lines.append("")


def print_call_sequence(report: dict[str, object]) -> None:
    selected = report["selected"]
    print(
        f"Function: {selected['name']} ({relpath(str(selected['file']))}:"
        f"{selected['start_line']}-{selected['end_line']})"
    )
    calls = report["calls"]
    if not calls:
        print("No direct call sites found.")
        return
    for idx, call in enumerate(calls, start=1):
        target = ""
        if call["resolved_file"]:
            target = f" -> {relpath(str(call['resolved_file']))}:{call['resolved_line']}"
        print(f"{idx}. line {call['line']}: {call['expression']}(){target}")


def print_param_constraints(report: dict[str, object]) -> None:
    selected = report["selected"]
    print(
        f"Function: {selected['name']} ({relpath(str(selected['file']))}:"
        f"{selected['start_line']}-{selected['end_line']})"
    )
    params = report["param_constraints"]
    if not params:
        print("No parameters found.")
        return
    for param in params:
        print()
        print(f"Parameter: {param['name']}")
        print(f"Type/format: {param['type']}")
        inferred = param["inferred"]
        if inferred:
            print("Constraints:")
            for text in inferred:
                print(f"- {text}")
        else:
            print("Constraints: no explicit constraint inferred from function context.")
        evidence = param["evidence"]
        if evidence:
            print("Evidence:")
            for line in evidence:
                print(f"- {line}")


def relpath(path_text: str) -> str:
    try:
        return os.path.relpath(path_text)
    except ValueError:
        return path_text


def print_item_group(title: str, items: list[dict[str, object]]) -> None:
    print(f"### {title}")
    if not items:
        print("- 未发现")
        print()
        return
    for item in items:
        print(
            f"- `{item['name']}` ({item['kind']}, {relpath(str(item['file']))}:{item['start_line']})"
        )
        snippet = str(item.get("snippet", "")).strip()
        if snippet:
            print("```c")
            print(snippet)
            print("```")
    print()


def print_markdown(report: dict[str, object]) -> None:
    selected = report["selected"]
    print(f"# 函数分析: `{selected['name']}`")
    print()
    print(
        f"- 位置: `{relpath(str(selected['file']))}:{selected['start_line']}-{selected['end_line']}`"
    )
    if report["ambiguous_candidates"]:
        print(f"- 还有 {len(report['ambiguous_candidates'])} 个同名候选，当前选择源码跨度最大的定义。")
    print()
    print("## 函数源码")
    print("```c")
    print(report["source"])
    print("```")
    print()

    deps = report["dependencies"]
    print("## 源码涉及的结构/变量/常量")
    print_item_group("结构体/联合体", deps["structures"])
    print_item_group("typedef 类型", deps["typedefs"])
    print_item_group("枚举/枚举值", deps["enums"])
    print_item_group("常量/宏", deps["constants"])
    print_item_group("全局变量", deps["global_variables"])
    print_item_group("静态变量", deps["static_variables"])

    print("## 函数调用序列")
    calls = report["calls"]
    if not calls:
        print("- 未发现直接调用")
    for idx, call in enumerate(calls, start=1):
        target = ""
        if call["resolved_file"]:
            target = f" -> `{relpath(str(call['resolved_file']))}:{call['resolved_line']}`"
        print(f"{idx}. line {call['line']}: `{call['expression']}()`{target}")
    print()

    print("## 入参约束和格式")
    params = report["param_constraints"]
    if not params:
        print("- 无入参")
    for param in params:
        print(f"### `{param['name']}`")
        print(f"- 类型/格式: `{param['type']}`")
        inferred = param["inferred"]
        if inferred:
            for text in inferred:
                print(f"- {text}")
        else:
            print("- 未从函数上下文发现显式约束。")
        evidence = param["evidence"]
        if evidence:
            print("- 证据:")
            for line in evidence:
                print(f"  - `{line}`")
    print()

    print("## 说明")
    for note in report["notes"]:
        print(f"- {note}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    commands = {"source", "calls", "params", "report"}
    if argv and argv[0] not in commands and not argv[0].startswith("-"):
        argv = ["report", *argv]

    parser = argparse.ArgumentParser(
        description="Query a VS Code C/C++ BROWSE.VC.DB by function name.",
        epilog="""examples:
  python tools/linux_meta_query.py source vfs_read --file fs\\read_write.c --output vfs_read_bundle.c
  python tools/linux_meta_query.py calls vfs_read --file fs\\read_write.c --max-depth 5
  python tools/linux_meta_query.py params vfs_read --file fs\\read_write.c
  python tools/linux_meta_query.py report start_kernel --file init\\main.c --no-macros

notes:
  --file is a substring filter used to choose one definition when Linux has
  multiple functions with the same name.
  The legacy form still works: python tools/linux_meta_query.py vfs_read --file fs\\read_write.c
  source recursively includes nested struct/union/enum/typedef dependencies by default.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("function", help="function name to query")
        subparser.add_argument("--repo", default="linux-7.0", help="Linux source root")
        subparser.add_argument(
            "--db",
            help=(
                "path to BROWSE.VC.DB, a directory containing BROWSE.VC.DB, "
                "or a repo root containing .vscode/BROWSE.VC.DB"
            ),
        )
        subparser.add_argument("--file", help="substring used to disambiguate source file")
        subparser.add_argument("--max-deps", type=int, default=20)
        subparser.add_argument("--max-candidates", type=int, default=12)
        subparser.add_argument("--max-snippet-lines", type=int, default=80)
        subparser.add_argument(
            "--no-macros",
            dest="include_macros",
            action="store_false",
            help="exclude upper-case macro-like call sites from call sequence",
        )
        subparser.set_defaults(include_macros=True)

    source_parser = subparsers.add_parser(
        "source",
        help="write function source and related snippets into a .c bundle",
    )
    add_common(source_parser)
    source_parser.add_argument(
        "--output",
        "-o",
        help="output .c file path; default is <function>_source_bundle.c",
    )
    source_parser.add_argument(
        "--max-nesting-depth",
        type=int,
        default=4,
        help="nested type dependency recursion depth for source bundles",
    )
    source_parser.set_defaults(max_deps=200)

    calls_parser = subparsers.add_parser(
        "calls",
        help="print upstream caller chains to the command line",
    )
    add_common(calls_parser)
    calls_parser.add_argument(
        "--max-depth",
        type=int,
        default=5,
        help="maximum upstream caller depth",
    )
    calls_parser.add_argument(
        "--max-chains",
        type=int,
        default=200,
        help="maximum number of caller chains to print",
    )
    calls_parser.add_argument(
        "--max-callers-per-level",
        type=int,
        default=80,
        help="maximum direct callers explored for each function name",
    )

    params_parser = subparsers.add_parser(
        "params",
        help="print inferred parameter constraints to the command line",
    )
    add_common(params_parser)

    report_parser = subparsers.add_parser(
        "report",
        help="print the original full report",
    )
    add_common(report_parser)
    report_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "source":
        output = api_export_source_bundle(args)
        print(f"Wrote source bundle: {output}")
        return 0
    if args.command == "calls":
        api_print_call_sequence(args)
        return 0
    if args.command == "params":
        api_print_param_constraints(args)
        return 0

    report = analyze(args)
    if getattr(args, "format", "markdown") == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_markdown(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
