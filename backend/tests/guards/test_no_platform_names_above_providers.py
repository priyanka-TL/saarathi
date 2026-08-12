"""No platform name may appear in CODE above the provider seam.

The general form of the two import-linter contracts that pin the same rule.
Those catch an `import`; this catches the rest -- a string literal like
`"saathi"` in a comparison, an attribute named `mitra_sessions`, a dict keyed on
a platform. Those are how the coupling actually accumulated: twelve files
outside the integration package named a vendor, and only one of them did it with
an import.

WHY IT PARSES RATHER THAN GREPS. Comments and docstrings are allowed to name a
platform, and should: "Mitra returns `{'status': 200, ...}`" is the sentence
that explains why a workaround exists, and forbidding it would trade real
documentation for a clean grep. What must not exist is a platform name the
program can BRANCH ON. So this walks the AST -- where comments do not appear at
all and docstrings are skipped explicitly -- and inspects identifiers, attribute
names, imports and runtime string constants.

ADDING A PROVIDER DOES NOT EDIT THIS FILE. `PLATFORM_NAMES` is derived from the
registry, so a package that registers itself is covered the moment it exists.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.providers.registry import registered_names

#: Layers that must be able to compile with every provider package deleted.
GUARDED_PACKAGES = (
    "app/domain",
    "app/services",
    "app/routers",
    "app/core",
    "app/exceptions",
    "app/agents",
    "app/repositories",
    "app/dependencies",
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Derived from the registry, so this test needs no edit when a platform is
#: added. Lower-cased; matching is case-insensitive and substring-based, so
#: "mitra" also catches a MitraError class, a mitra_sessions attribute and a
#: MITRA_ENABLED string -- the three shapes the old coupling actually took.
PLATFORM_NAMES = tuple(sorted(registered_names()))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """`id()` of every node that is a docstring, so it can be skipped.

    A docstring is an `ast.Expr` wrapping a string constant, first in the body
    of a module, class or function.
    """
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                skip.add(id(first.value))
    return skip


def _offences(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _docstring_nodes(tree)
    found: list[str] = []

    def hit(text: str) -> str | None:
        lowered = (text or "").lower()
        for name in PLATFORM_NAMES:
            if name in lowered:
                return name
        return None

    for node in ast.walk(tree):
        candidates: list[str] = []

        if isinstance(node, ast.Name):
            candidates.append(node.id)
        elif isinstance(node, ast.Attribute):
            candidates.append(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates.append(node.name)
        elif isinstance(node, ast.arg):
            candidates.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            candidates.append(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            candidates.append(getattr(node, "module", "") or "")
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                candidates.append(node.value)

        for candidate in candidates:
            name = hit(candidate)
            if name is not None:
                line = getattr(node, "lineno", 0)
                found.append(f"{path.relative_to(BACKEND_ROOT)}:{line} names {name!r} in {candidate!r}")
    return found


@pytest.mark.parametrize("package", GUARDED_PACKAGES)
def test_no_platform_name_in_code_above_the_seam(package: str) -> None:
    root = BACKEND_ROOT / package
    assert root.is_dir(), f"{package} does not exist -- update GUARDED_PACKAGES"

    offences: list[str] = []
    for path in sorted(root.rglob("*.py")):
        offences.extend(_offences(path))

    assert not offences, (
        f"{package} names a specific remote platform in code. The core must work "
        f"with generic provider concepts -- move this behind the RemoteProvider "
        f"contract in app/providers/protocol.py.\n  " + "\n  ".join(offences)
    )


def test_platform_names_are_derived_from_the_registry() -> None:
    """If this ever came up empty the test above would pass vacuously."""
    assert PLATFORM_NAMES, "no providers registered -- the guard above proves nothing"
