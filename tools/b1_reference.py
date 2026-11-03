"""Immutable pre-B1 search implementation, used only by comparison diagnostics."""
import ast
import hashlib
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def reference_module(timer=None):
    directory = ROOT / "tests/reference"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    data = (directory / "search_before_b1.py").read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["reference_sha256"]
    tree = ast.parse(data)
    count = 0

    class Instrument(ast.NodeTransformer):
        def visit_ListComp(self, node):
            nonlocal count
            node = self.generic_visit(node)
            if (len(node.generators) == 1 and isinstance(node.generators[0].iter, ast.Call)
                    and ast.unparse(node.generators[0].iter) == "ranked(dense)"
                    and [ast.unparse(x) for x in node.generators[0].ifs] == ["i in valid"]):
                count += 1
                return ast.copy_location(ast.Call(func=ast.Name(id="_timed_filter", ctx=ast.Load()),
                    args=[ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                                      kw_defaults=[], defaults=[]), body=node)], keywords=[]), node)
            return node

    if timer is not None:
        tree = Instrument().visit(tree)
        assert count == 2, "Reference filter expressions changed"
        ast.fix_missing_locations(tree)
    module = ModuleType("app._b1_reference")
    module.__package__ = "app"
    module.__dict__["_timed_filter"] = timer
    exec(compile(tree, str(directory / "search_before_b1.py"), "exec"), module.__dict__)
    return module


def candidate_module():
    """Retained rejected candidate; diagnostics only, never a serving dependency."""
    directory = ROOT / "tests/reference"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    data = (directory / "search_candidate_b1.py").read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["candidate_sha256"]
    module = ModuleType("app._b1_candidate")
    module.__package__ = "app"
    exec(compile(data, str(directory / "search_candidate_b1.py"), "exec"), module.__dict__)
    return module
