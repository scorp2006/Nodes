"""Multi-language units via tree-sitter (tree-sitter-language-pack): functions, classes, the import block and
module-level blocks, for every supported language. Calls are matched by NAME only, so they are labeled
`resolved_by: 'name-match'` with low confidence (approximate). Python packages keep the precise study resolver
(nodes/indexer.py); this module covers everything else.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tree_sitter_language_pack import get_parser

EXT = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
       ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c", ".h": "c",
       ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".cs": "csharp", ".rb": "ruby",
       ".php": "php", ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift"}
SKIP_DIRS = {"node_modules", "vendor", "third_party", "third-party", "dist", "build", ".venv", "venv", "__pycache__",
             "site-packages", "target", "bower_components", ".git"}
MAX_BYTES = 1_000_000

FUNC = {"function_definition", "function_declaration", "generator_function_declaration", "method_definition",
        "method_declaration", "function_item", "constructor_declaration", "method", "singleton_method",
        "function_signature_item", "arrow_function_declarator"}
CLASS = {"class_definition", "class_declaration", "class_specifier", "struct_specifier", "interface_declaration",
         "enum_declaration", "struct_item", "enum_item", "trait_item", "impl_item", "class", "module",
         "struct_declaration", "object_declaration", "protocol_declaration", "type_declaration",
         "record_declaration"}
IMPORT = {"import_statement", "import_from_statement", "import_declaration", "use_declaration", "preproc_include",
          "using_directive", "namespace_use_declaration", "import_header", "import_list", "package_clause",
          "future_import_statement"}
CONTAINER = {"export_statement", "decorated_definition", "namespace_definition", "namespace_declaration",
             "declaration_list", "template_declaration", "file_scoped_namespace_declaration"}
CALL = {"call_expression", "call", "method_invocation", "invocation_expression", "function_call_expression",
        "member_call_expression"}


def tracked_files(root: Path) -> list[str]:
    """Source files git tracks (so .gitignore applies), minus vendored dirs, large files and binaries."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        files = [f for f in out.decode("utf-8", "replace").split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    keep = []
    for f in files:
        parts = f.split("/")
        if Path(f).suffix.lower() not in EXT or SKIP_DIRS & set(parts[:-1]):
            continue
        p = root / f
        try:
            if p.stat().st_size > MAX_BYTES or b"\0" in p.read_bytes()[:4096]:
                continue
        except OSError:
            continue
        keep.append(f)
    return sorted(keep)


def _name(n) -> str:
    if n.type == "impl_item":  # Rust: `impl Order` / `impl Trait for Order` is named by its type
        t = n.child_by_field_name("type")
        return t.text.decode("utf-8", "replace") if t is not None else ""
    if n.type == "type_declaration":  # Go: `type Server struct{}` keeps the name in a type_spec
        spec = next((c for c in n.children if c.type in ("type_spec", "type_alias")), None)
        return _name(spec) if spec is not None else ""
    for field in ("name", "declarator"):
        c = n.child_by_field_name(field)
        while c is not None and c.type not in ("identifier", "field_identifier", "type_identifier", "property_identifier",
                                               "name", "constant", "simple_identifier", "qualified_identifier"):
            c = c.child_by_field_name("declarator") or c.child_by_field_name("name") or next(
                (x for x in c.children if "identifier" in x.type or x.type in ("name", "constant")), None)
        if c is not None:
            return c.text.decode("utf-8", "replace")
    return ""


def _arrow_name(n) -> str:
    """`const f = (x) => ...` / `const f = function () {...}`: a function named by its variable."""
    if n.type not in ("lexical_declaration", "variable_declaration"):
        return ""
    for d in n.children:
        if d.type == "variable_declarator":
            v = d.child_by_field_name("value")
            if v is not None and v.type in ("arrow_function", "function", "function_expression"):
                return _name(d)
    return ""


def _calls(n, out: set):
    if n.type in CALL:
        f = n.child_by_field_name("function") or n.child_by_field_name("name") or n.child_by_field_name("method")
        if f is not None:
            t = f.text.decode("utf-8", "replace")
            out.add(t.rsplit(".", 1)[-1].rsplit("::", 1)[-1].rsplit("->", 1)[-1])
    for c in n.children:
        _calls(c, out)


def extract(rel: str, src: bytes, lang: str, repo: str):
    """Units and outgoing call names for one file. Units: file, class, function, region (imports / module@N)."""
    tree = get_parser(lang).parse(src)
    lines = src.count(b"\n") + 1
    units = [{"uid": f"file:{repo}:{rel}", "kind": "file", "path": rel, "symbol": "", "qualname": "", "lang": lang,
              "start": 1, "end": lines, "file": "", "line": 0}]
    calls = []  # (caller uid, callee short name)

    def add(kind, symbol, node, prefix_kind="fn"):
        uid = f"{prefix_kind}:{repo}:{rel}#{symbol}"
        units.append({"uid": uid, "kind": kind, "path": rel, "symbol": symbol, "qualname": "", "lang": lang,
                      "start": node.start_point[0] + 1, "end": node.end_point[0] + 1, "file": "", "line": 0})
        return uid

    def walk(node, prefix: str, top: bool):
        imports, run = [], []

        def flush():
            if run:
                add("region", f"module@{run[0].start_point[0] + 1}", _span(run), "region")
                run.clear()
        for c in node.children:
            t = c.type
            arrow = _arrow_name(c) if top or prefix else ""
            if t in IMPORT:
                imports.append(c)
                flush()
            elif t in CONTAINER:
                flush()
                walk(c, prefix, top)
            elif t in FUNC or arrow:
                flush()
                name = arrow or _name(c) or f"anon@{c.start_point[0] + 1}"
                uid = add("function", f"{prefix}{name}", c)
                found = set()
                _calls(c, found)
                calls.extend((uid, x) for x in found if x)
                body = c.child_by_field_name("body")
                if body is not None:
                    walk(body, f"{prefix}{name}.", False)  # nested functions / methods in Ruby etc.
            elif t in CLASS:
                flush()
                name = _name(c) or f"anon@{c.start_point[0] + 1}"
                add("class", f"{prefix}{name}", c, "class")
                body = c.child_by_field_name("body") or next((x for x in c.children if x.type.endswith("body")
                                                              or x.type == "declaration_list"), None)
                if body is not None:
                    walk(body, f"{prefix}{name}.", False)
            elif top and t not in ("comment", "line_comment", "block_comment", "ERROR", "{", "}", ";") \
                    and c.is_named:
                run.append(c)  # a module-level statement: aliases, constants, registrations
            else:
                flush()
        flush()
        if imports and top:
            add("region", "imports", _span(imports), "region")
    walk(tree.root_node, "", True)
    return units, calls


class _span:
    """A start/end point pair covering several nodes (for regions)."""

    def __init__(self, nodes):
        self.start_point = min(n.start_point for n in nodes)
        self.end_point = max(n.end_point for n in nodes)


def link_calls(units: list[dict], calls: list[tuple]) -> list[dict]:
    """Name-based call edges: a callee name matches function units by their last name segment.
    Same-file matches win; a name shared by more than 3 functions repo-wide is too ambiguous and dropped."""
    by_name = {}
    for u in units:
        if u["kind"] == "function":
            by_name.setdefault(u["symbol"].rsplit(".", 1)[-1], []).append(u)
    path_of = {u["uid"]: u["path"] for u in units}
    lang_of = {u["uid"]: u["lang"] for u in units}
    edges = []
    for src, name in calls:
        cands = [u for u in by_name.get(name, []) if u["lang"] == lang_of[src]]  # never across languages
        same = [u for u in cands if u["path"] == path_of[src]]
        pick = same or (cands if len(cands) <= 3 else [])
        edges += [{"src": src, "dst": u["uid"], "confidence": 0.5 if same else 0.3} for u in pick if u["uid"] != src]
    return edges


def scan_repo(root: Path, repo: str, skip: set[str] = frozenset()) -> tuple[list[dict], list[dict], dict]:
    """All tracked source files not in `skip` (paths handled by the precise Python resolver)."""
    units, calls, langs = [], [], {}
    for rel in tracked_files(root):
        if rel in skip:
            continue
        lang = EXT[Path(rel).suffix.lower()]
        try:
            u, c = extract(rel, (root / rel).read_bytes(), lang, repo)
        except Exception:  # noqa: BLE001  an unparseable file is skipped and counted
            langs.setdefault("_unparsed", 0)
            langs["_unparsed"] += 1
            continue
        units += u
        calls += c
        langs[lang] = langs.get(lang, 0) + 1
    return units, link_calls(units, calls), langs
