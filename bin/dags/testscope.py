"""Which tests should a worker run for its diff? (GH-50, part 1 of GH-34)

Pure: files in, a proposal out. A table (``tests/map.yaml``, glob -> test files) comes first;
for a changed ``bin/`` module with no table entry, the tests that import it are the fallback.
"Neighbours" are the tests of modules that import a changed module, one hop. The import scan
is static (AST), so dynamic imports are missed; a file nothing maps falls back to the full
suite, the safe direction.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import shlex
import subprocess
from pathlib import Path

from dags import records as R

SCOPES = ("none", "targeted", "neighbours", "full")
MAP_FILE = "tests/map.yaml"
ALWAYS_FULL = ("tests/conftest.py", "tests/fakes.py")      # shared by every test
DOC_SUFFIXES = (".md", ".txt", ".rst")
DOC_NAMES = ("LICENSE", "BATON")


class Unmapped(Exception):
    pass


# -- the diff -------------------------------------------------------------------------

def _git(args: list[str], wt: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(wt), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def changed_files(wt: Path, base: str = "main") -> list[str]:
    """Files this worktree changed against the base branch: committed, staged, unstaged and new."""
    wt = Path(wt)
    ref = f"origin/{base}"
    try:
        _git(["rev-parse", "--verify", "--quiet", ref], wt)
    except RuntimeError:
        ref = base
    fork = _git(["merge-base", ref, "HEAD"], wt).strip()
    names = set(_git(["diff", "--name-only", fork], wt).split("\n"))
    names |= set(_git(["ls-files", "--others", "--exclude-standard"], wt).split("\n"))
    return sorted(n for n in names if n and not n.startswith(".swarm-task/"))


def diff_sha(files: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(files)).encode()).hexdigest()[:12]


# -- the table and the import graph ----------------------------------------------------

def load_map(root: Path) -> list[tuple[str, list[str]]]:
    data = R.load_yaml(Path(root) / MAP_FILE)
    out = []
    for glob, tests in (data.get("map") or {}).items():
        out.append((str(glob), [str(tests)] if isinstance(tests, str) else [str(t) for t in tests or []]))
    return out


def module_of(path: str) -> str | None:
    """``bin/dags/plan.py`` -> ``dags.plan``; None for anything outside ``bin/`` modules."""
    if not path.startswith("bin/") or not path.endswith(".py") or path.startswith("bin/skill/"):
        return None
    parts = path[len("bin/"):-len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def imports_of(source: str, package: str = "") -> set[str]:
    """Every dotted name a file might be importing (``from a import b`` gives ``a`` and ``a.b``)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = package.split(".") if package else []
                pkg = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                base = ".".join([*pkg, *([node.module] if node.module else [])])
            if base:
                found.add(base)
            found.update(f"{base}.{a.name}" if base else a.name for a in node.names)
    return found


class Graph:
    """Who imports whom among the repo's ``bin/`` modules and ``tests/test_*.py`` files."""

    def __init__(self, root: Path):
        root = Path(root)
        self.modules: dict[str, str] = {}              # module name -> file
        for p in sorted((root / "bin").rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            name = module_of(rel)
            if name:
                self.modules[name] = rel
        self.imports: dict[str, set[str]] = {}         # file -> bin modules it imports
        self.tests: list[str] = []
        files = [(rel, rel) for rel in self.modules.values()]
        for p in sorted((root / "tests").glob("test_*.py")):
            rel = p.relative_to(root).as_posix()
            self.tests.append(rel)
            files.append((rel, rel))
        by_file = {f: m for m, f in self.modules.items()}
        for rel, _ in files:
            pkg = by_file.get(rel, "")
            if pkg and not rel.endswith("__init__.py"):
                pkg = pkg.rpartition(".")[0]
            names = imports_of((root / rel).read_text(encoding="utf-8", errors="replace"), pkg)
            known = {n for n in names if n in self.modules and self.modules[n] != rel}
            # `from dags import ledger` imports dags.ledger, not every module of the package
            self.imports[rel] = {n for n in known if not any(m.startswith(n + ".") for m in known)}

    def tests_importing(self, module: str) -> list[str]:
        return [t for t in self.tests if module in self.imports.get(t, ())]

    def importers(self, module: str) -> list[str]:
        """Modules (not tests) that import ``module``."""
        return sorted(m for m, f in self.modules.items() if m != module and module in self.imports.get(f, ()))


# -- the proposal ---------------------------------------------------------------------------

def is_docs(path: str) -> bool:
    return path.startswith("docs/") or path.endswith(DOC_SUFFIXES) or Path(path).name in DOC_NAMES


def _existing(root: Path, tests: list[str]) -> list[str]:
    return [t for t in tests if (Path(root) / t).is_file()]


def _lookup(path: str, table, graph: Graph, root: Path) -> list[tuple[str, str]]:
    """(test file, reason) for one changed file; raises Unmapped when nothing maps it."""
    if path in ALWAYS_FULL:
        raise Unmapped(f"{path} is shared by every test")
    hits: list[tuple[str, str]] = []
    for glob, tests in table:
        if fnmatch.fnmatch(path, glob):
            hits += [(t, "mapped in map.yaml") for t in _existing(root, tests)]
    if hits:
        return hits
    if path.startswith("tests/") and Path(path).name.startswith("test_") and path.endswith(".py"):
        return [(path, "changed itself")]
    module = module_of(path)
    if module:
        found = graph.tests_importing(module)
        if found:
            return [(t, f"imports {module}") for t in found]
    raise Unmapped(path)


def _merge(into: dict[str, str], pairs: list[tuple[str, str]], reason: str | None = None) -> None:
    for test, why in pairs:
        into.setdefault(test, reason or why)


def estimate_seconds(tests: list[str] | None, durations) -> float | None:
    """Seconds the recorded slowest tests add up to in this scope (a floor, not a total);
    None when no recorded test falls in it. ``tests=None`` means the whole suite."""
    total, hit = 0.0, False
    for d in durations or []:
        node = str(d.get("test") or "")
        if tests is None or node.split("::")[0] in tests:
            total += float(d.get("seconds") or 0)
            hit = True
    return round(total, 1) if hit else None


def propose(changed: list[str], root: Path, durations=()) -> dict:
    root = Path(root)
    table = load_map(root)
    graph = Graph(root)
    targeted: dict[str, str] = {}
    neighbours: dict[str, str] = {}
    unmapped: list[str] = []
    docs: list[str] = []
    for path in changed:
        if is_docs(path):
            docs.append(path)
            continue
        try:
            _merge(targeted, [(t, f"{path}: {why}") for t, why in _lookup(path, table, graph, root)])
        except Unmapped:
            unmapped.append(path)
            continue
        module = module_of(path)
        for importer in graph.importers(module) if module else []:
            try:
                _merge(neighbours, _lookup(graph.modules[importer], table, graph, root),
                       f"covers {importer}, which imports {module}")
            except Unmapped:
                pass                                   # an unmapped neighbour is not a reason to run more
    neighbours = {t: why for t, why in neighbours.items() if t not in targeted}

    if unmapped:
        rec, why = "full", "no test maps " + ", ".join(unmapped)
    elif not targeted:
        rec, why = "none", "only docs changed" if docs else "nothing changed"
    elif neighbours:
        rec, why = "neighbours", "mapped tests, plus tests of modules that import the changed ones"
    else:
        rec, why = "targeted", "every changed file is mapped to tests"

    def option(tests: dict[str, str] | None) -> dict:
        names = None if tests is None else sorted(tests)
        return {"tests": None if tests is None else [{"file": t, "reason": tests[t]} for t in names],
                "seconds": estimate_seconds(names, durations)}

    return {
        "recommendation": rec, "reason": why, "changed": list(changed), "diff_sha": diff_sha(changed),
        "unmapped": unmapped,
        "options": {
            "none": option({}),
            "targeted": option(targeted),
            "neighbours": option({**targeted, **neighbours}),
            "full": option(None),
        },
    }


# -- running a scope --------------------------------------------------------------------------

def scope_files(proposal: dict, scope: str) -> list[str] | None:
    """Test files to run for ``scope``; None means the whole suite."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    tests = ((proposal.get("options") or {}).get(scope) or {}).get("tests")
    return None if scope == "full" or tests is None else [t["file"] for t in tests]


def command_for(test_command: str | None, files: list[str] | None, scope_command: str | None = None) -> str | None:
    """The command for a scope: ``test_command`` for the whole suite; for ``files``, the repo's
    ``test_scope_command`` (e.g. ``.swarm/venv/bin/python -m pytest -q``) with the files appended.
    Without one, files are appended to ``test_command``, which only narrows a command that takes
    test paths as arguments (``./bin/dev-setup.sh`` always adds its own ``tests``, so it runs
    everything). None when there is nothing to run."""
    if files is None:
        return test_command
    if not files:
        return None
    base = scope_command or test_command
    return f"{base} {shlex.join(files)}" if base else None


def render_text(proposal: dict, recommended_mark: str = "recommended") -> str:
    lines = [f"Changed: {', '.join(proposal.get('changed') or []) or '(nothing)'}", ""]
    for scope in SCOPES:
        opt = proposal["options"][scope]
        mark = f"  <- {recommended_mark}: {proposal['reason']}" if scope == proposal["recommendation"] else ""
        secs = opt.get("seconds")
        eta = f"  (~{secs:g}s from the slowest recorded tests)" if secs is not None else ""
        lines.append(f"[{scope}]{eta}{mark}")
        if opt["tests"] is None:
            lines.append("  the whole suite")
        for t in opt["tests"] or []:
            lines.append(f"  {t['file']}  ({t['reason']})")
        if opt["tests"] == []:
            lines.append("  (no tests)")
    return "\n".join(lines)
