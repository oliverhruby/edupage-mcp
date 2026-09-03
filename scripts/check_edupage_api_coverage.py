import ast
import inspect
import json
import sys
from pathlib import Path

from edupage_api import Edupage


ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = ROOT / "src" / "edupage_mcp" / "__init__.py"
IGNORE_FILE = ROOT / "scripts" / "edupage_api_ignored_methods.json"


class ClientMethodCallVisitor(ast.NodeVisitor):
    def __init__(self):
        self.client_vars = set()
        self.called_methods = set()

    def _mark_client_target(self, target):
        if isinstance(target, ast.Name):
            self.client_vars.add(target.id)

    def _is_client_constructor(self, node):
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in {"_require_client", "Edupage"}
        return False

    def visit_Assign(self, node):
        if self._is_client_constructor(node.value):
            for target in node.targets:
                self._mark_client_target(target)
        elif isinstance(node.value, ast.Name) and node.value.id in self.client_vars:
            for target in node.targets:
                self._mark_client_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if self._is_client_constructor(node.value):
            self._mark_client_target(node.target)
        elif isinstance(node.value, ast.Name) and node.value.id in self.client_vars:
            self._mark_client_target(node.target)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id in self.client_vars:
                self.called_methods.add(func.attr)
            elif (
                isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "_require_client"
            ):
                self.called_methods.add(func.attr)
        self.generic_visit(node)


def get_upstream_public_methods():
    methods = set()
    for name, member in inspect.getmembers(Edupage):
        if name.startswith("_"):
            continue
        if callable(member):
            methods.add(name)
    return methods


def get_called_client_methods():
    module = ast.parse(SERVER_FILE.read_text(encoding="utf-8"))
    visitor = ClientMethodCallVisitor()
    visitor.visit(module)
    return visitor.called_methods


def load_ignored_methods():
    if not IGNORE_FILE.exists():
        return {}, set()
    data = json.loads(IGNORE_FILE.read_text(encoding="utf-8"))
    ignored = data.get("ignored_methods", {})
    extra_covered = set(data.get("extra_covered_methods", []))
    if not isinstance(ignored, dict):
        raise ValueError("ignored_methods must be an object of method -> reason")
    if not isinstance(data.get("extra_covered_methods", []), list):
        raise ValueError("extra_covered_methods must be a list of method names")
    return ignored, extra_covered


def main():
    upstream = get_upstream_public_methods()
    covered = get_called_client_methods()
    ignored, extra_covered = load_ignored_methods()
    covered |= extra_covered

    covered_upstream = covered & upstream
    unknown_ignored = sorted(set(ignored) - upstream)
    overlapping = sorted(set(ignored) & covered_upstream)
    uncovered = sorted(upstream - covered_upstream - set(ignored))

    print(f"Edupage public methods: {len(upstream)}")
    print(f"Covered upstream methods: {len(covered_upstream)}")
    print(f"Ignored by manifest: {len(ignored)}")
    print(f"Uncovered methods: {len(uncovered)}")

    if unknown_ignored:
        print("\nERROR: ignored methods not present upstream anymore:")
        for name in unknown_ignored:
            print(f"- {name}")

    if overlapping:
        print("\nERROR: methods are both covered and ignored:")
        for name in overlapping:
            print(f"- {name}")

    if uncovered:
        print("\nERROR: uncovered upstream methods:")
        for name in uncovered:
            print(f"- {name}")

    if unknown_ignored or overlapping or uncovered:
        return 1

    print("\nOK: all upstream methods are covered or intentionally ignored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
