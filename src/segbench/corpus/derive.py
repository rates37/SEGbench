"""Derive ``ground_truth.fix.files`` and ``.symbols`` mechanically from the fix commit.

CLAUDE.md forbids fabricating corpus content, and plan.md section 3.3 says these two fields are
generated, never hand-maintained — a hand-written file list drifts from the commit and quietly
corrupts every ``file_f1`` score computed against it.

Phase 1 reads the commit from a **local clone** given on the command line. Phase 4 will route the
same extraction through the truncating mirror; the interface here (a repository path plus a commit
ref, in and a :class:`DerivedFix` out) is what that phase will reuse.

Symbol extraction is per language and deliberately conservative:

* **Python** — parse the pre- and post-image of each changed file with :mod:`ast` and report the
  qualified names (``Class.method``) whose source range covers a changed line.
* **Go** — regex over top-level declarations, which is what the language's formatting conventions
  make reliable without pulling in a parser.
* **Anything else** — ``None``, meaning "no symbol information for this bug", which the grader
  treats differently from an empty list and redistributes the symbol weight (plan.md section 7.3).
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from segbench.logging import get_logger

log = get_logger(__name__)

PYTHON_SUFFIXES = frozenset({".py"})
GO_SUFFIXES = frozenset({".go"})


class DeriveError(Exception):
    """The fix commit could not be read, or the repository is unusable."""


@dataclass(frozen=True)
class DerivedFix:
    """What ``derive`` learned from the commit."""

    commit: str
    files: list[str]
    symbols: list[str] | None


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo``, returning stdout. The full command line is logged for debugging."""
    command = ["git", "-C", str(repo), *args]
    log.debug("running git", extra={"command": " ".join(command)})
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DeriveError(
            f"git {' '.join(args)} failed in {repo} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def changed_files(repo: Path, commit: str) -> list[str]:
    """Paths changed by ``commit``, repository-relative, sorted."""
    out = _git(repo, "show", "--no-renames", "--name-only", "--pretty=format:", commit)
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def changed_line_numbers(repo: Path, commit: str, path: str) -> set[int]:
    """Line numbers in the *post-image* of ``path`` touched by ``commit``.

    Parsed from unified-diff hunk headers, which is enough: a symbol counts as changed if any of
    its lines is inside a hunk.
    """
    out = _git(repo, "show", "--no-renames", "--unified=0", "--pretty=format:", commit, "--", path)
    lines: set[int] = set()
    for header in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", out, re.MULTILINE):
        start = int(header.group(1))
        count = int(header.group(2) or 1)
        # A pure deletion hunk has count 0 and points at the line it was removed after.
        lines.update(range(start, start + max(count, 1)))
    return lines


def file_at_commit(repo: Path, commit: str, path: str) -> str | None:
    """The content of ``path`` at ``commit``, or ``None`` if it does not exist there."""
    try:
        return _git(repo, "show", f"{commit}:{path}")
    except DeriveError:
        return None


def python_symbols(source: str, changed: set[int]) -> list[str]:
    """Qualified names of Python defs/classes whose source range covers a changed line.

    Nested functions are reported under their enclosing qualified name, so a changed helper inside
    ``LibvirtDriver._get_guest_config`` reports the method, which is the granularity the grader's
    ``symbol_hit`` compares at.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A file that does not parse at this revision (Python 2, a template) yields no symbols
        # rather than failing the whole derivation.
        return []

    found: list[str] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            qualified = f"{prefix}.{child.name}" if prefix else child.name
            # Decorators sit above the `def` line but are part of the symbol.
            start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
            end = child.end_lineno or child.lineno
            if changed & set(range(start, end + 1)):
                found.append(qualified)
            visit(child, qualified)

    visit(tree, "")
    # Drop a class when one of its own methods is also reported: the method is the precise answer.
    precise = {name for name in found if "." in name}
    prefixes = {name.rsplit(".", 1)[0] for name in precise}
    return sorted({name for name in found if name not in prefixes} | precise)


_GO_DECL_RE = re.compile(
    r"""(?m)^
    (?:
        func \s* (?:\(\s*\w+\s+\*?(?P<receiver>\w+)\s*\))? \s* (?P<func>\w+)
      | (?:type|var|const) \s+ (?P<other>\w+)
    )\b
    """,
    re.VERBOSE,
)


def go_symbols(source: str, changed: set[int]) -> list[str]:
    """Top-level Go declarations covering a changed line.

    Regex rather than a parser: gofmt guarantees top-level declarations start in column zero, so
    the next column-zero declaration reliably bounds the previous one. Methods are reported as
    ``Receiver.Method``.
    """
    declarations: list[tuple[int, str]] = []
    for match in _GO_DECL_RE.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        if match.group("func"):
            receiver = match.group("receiver")
            name = f"{receiver}.{match.group('func')}" if receiver else match.group("func")
        else:
            name = match.group("other")
        declarations.append((line, name))

    total_lines = source.count("\n") + 1
    found: list[str] = []
    for index, (start, name) in enumerate(declarations):
        end = declarations[index + 1][0] - 1 if index + 1 < len(declarations) else total_lines
        if changed & set(range(start, end + 1)):
            found.append(name)
    return sorted(set(found))


def derive_fix(repo: Path, commit: str) -> DerivedFix:
    """Extract changed files and, where the language is supported, changed symbols."""
    repo = Path(repo)
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise DeriveError(f"{repo}: not a git repository (no .git directory and not a bare repo)")

    resolved = _git(repo, "rev-parse", commit).strip()
    files = changed_files(repo, resolved)
    if not files:
        raise DeriveError(f"{resolved}: commit changes no files; is this an empty or merge commit?")

    supported = [f for f in files if Path(f).suffix in PYTHON_SUFFIXES | GO_SUFFIXES]
    if not supported:
        log.info(
            "no symbol extractor for this bug's languages; symbols left null",
            extra={"commit": resolved, "files": files},
        )
        return DerivedFix(commit=resolved, files=files, symbols=None)

    symbols: set[str] = set()
    for path in supported:
        changed = changed_line_numbers(repo, resolved, path)
        if not changed:
            continue
        source = file_at_commit(repo, resolved, path)
        if source is None:
            continue  # Deleted by the fix; its post-image symbols do not exist.
        suffix = Path(path).suffix
        extracted = (
            python_symbols(source, changed)
            if suffix in PYTHON_SUFFIXES
            else go_symbols(source, changed)
        )
        symbols.update(extracted)

    return DerivedFix(commit=resolved, files=files, symbols=sorted(symbols))


def _render_list(key: str, values: list[str] | None, indent: str) -> list[str]:
    if values is None:
        return [f"{indent}{key}: null"]
    if not values:
        return [f"{indent}{key}: []"]
    return [f"{indent}{key}:", *(f"{indent}  - {value}" for value in values)]


def write_derived_fix(path: Path, derived: DerivedFix) -> None:
    """Rewrite ``fix.files`` and ``fix.symbols`` in ``ground_truth.yaml`` in place.

    A line-level rewrite rather than a YAML round-trip, because ``ground_truth.yaml`` is a
    hand-maintained file whose comments — the TODO markers from ``corpus add``, the maintainer's
    notes on an alternate framing — are load-bearing, and ``yaml.safe_dump`` would silently drop
    all of them. Only the two generated keys are touched; every other byte is preserved.
    """
    lines = path.read_text(encoding="utf-8").splitlines()

    fix_index = next((i for i, line in enumerate(lines) if re.match(r"^fix:\s*$", line)), None)
    if fix_index is None:
        raise DeriveError(f"{path}: no top-level 'fix:' block to write files and symbols into")

    # The fix block runs until the next line that starts in column zero.
    end = next(
        (i for i in range(fix_index + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    block = lines[fix_index + 1 : end]

    indent = next((line[: len(line) - len(line.lstrip())] for line in block if line.strip()), "  ")

    kept: list[str] = []
    skipping = False
    for line in block:
        stripped = line.strip()
        if re.match(rf"^{re.escape(indent)}(files|symbols):", line):
            skipping = True
            continue
        # Continuation lines of the key being replaced: its list items, or a comment inside it.
        if skipping and (stripped.startswith("- ") or not stripped):
            continue
        skipping = False
        kept.append(line)

    replacement = [
        *kept,
        *_render_list("files", derived.files, indent),
        *_render_list("symbols", derived.symbols, indent),
    ]
    lines[fix_index + 1 : end] = replacement
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
