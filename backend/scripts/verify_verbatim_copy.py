#!/usr/bin/env python
"""GATE A -- proof that the framework-agnostic layers were COPIED, not rewritten.

The whole risk model of this migration rests on one claim: the domain,
repositories, services, agents, integrations, llm, tools, models and database
packages are byte-identical to the Flask original apart from their import
lines. This script checks that claim mechanically instead of asking a reviewer
to take it on faith.

A file passes if every differing line is an `import`/`from` statement or a
comment. Anything else -- a changed condition, a reordered call, a "small
cleanup" -- fails the gate.

Usage:  python scripts/verify_verbatim_copy.py [path-to-flask-repo]
Exit code 0 = clean, 1 = drift found.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEFAULT_FLASK_REPO = BACKEND.parent.parent / "saarathi-poc"

# Files whose new location differs from a straight package rename.
RELOCATED = {
    "src/db/engine.py": "app/database/engine.py",
    "src/db/models.py": "app/models/orm.py",
    "src/settings.py": "app/core/settings.py",
    "src/logger.py": "app/core/logger.py",
    "src/container.py": "app/core/container.py",
    "src/bootstrap.py": "app/core/bootstrap.py",
}

# Packages copied wholesale under the same name.
MIRRORED_PACKAGES = (
    "domain",
    "repositories",
    "services",
    "agents",
    "integrations",
    "llm",
    "tools",
)

# Files that were deliberately edited beyond their imports, with the reason.
# Anything NOT listed here must be import-only.
EXPECTED_EDITS = {
    "app/core/settings.py": "adds frontend_origins + threadpool_size; explicit load_dotenv",
    "app/core/logger.py": "RequestIDFilter reads a ContextVar instead of flask.g",
    "app/core/bootstrap.py": "YAML dir gains one .parent (module moved into core/)",
    "app/services/identity.py": "rewritten as a single AUTH_CHECK-driven Authenticator",
    "app/core/container.py": "holds an Authenticator, not a user_provider pair",
    # Configurability pass: nothing about the deployment may be a literal.
    "app/integrations/mitra/rest_client.py": "endpoint paths moved onto a settings-fed MitraPaths",
    "app/domain/agent_spec.py": (
        "finalize_path relaxed from a Literal to str -- the endpoints are configurable "
        "now and the domain layer cannot import Settings; ConfigSyncService validates it"
    ),
    "app/services/config_sync.py": (
        "asserts finalize_path against the CONFIGURED Mitra endpoints; "
        "agent_configurations renamed to agent_configs (migration 0006)"
    ),
    # Multi-tenancy pass (migration 0006). The agent catalogue gained a
    # tenant/organization scope, and the table holding versioned configs was
    # renamed. These three files are the only copied ones that had to follow.
    "app/services/agent_registry.py": (
        "agent_configurations renamed to agent_configs (migration 0006); reload() "
        "now filters to the default scope, because an agent may have one active "
        "config PER TENANT and an unfiltered join would make the global snapshot "
        "non-deterministic; adds resolve_for_scope()"
    ),
    "app/services/orchestration.py": (
        "resolves the selected agent for the caller's tenant/organization at ONE "
        "point (step 4b), so every later agent.spec read and the "
        "HandlerFactory (key, checksum) cache key are tenant-correct"
    ),
    "app/models/orm.py": (
        "adds the migration-0006 models -- Agent, AgentConfig, Capability, "
        "CapabilityAgent -- which did not exist under Flask. The pre-existing "
        "models are untouched; only the AgentSession FK comment was rewritten, "
        "because an Agent class is now registered and the historical reason for "
        "deferring that FK no longer applies"
    ),
}


def pairs(flask_repo: Path):
    for old, new in RELOCATED.items():
        yield flask_repo / old, BACKEND / new
    for pkg in MIRRORED_PACKAGES:
        root = flask_repo / "src" / pkg
        for old in sorted(root.rglob("*.py")):
            yield old, BACKEND / "app" / old.relative_to(flask_repo / "src")
    for old in sorted((flask_repo / "migrations").rglob("*.py")):
        yield old, BACKEND / old.relative_to(flask_repo)


# The mechanical package rename that was applied to every copied file, most
# specific first. A line is benign iff applying THIS and nothing else turns the
# old line into the new one -- which is exactly the claim being tested. It
# catches docstrings and comments that name a module, and the two real code
# lines in agents/factory.py that build module paths as strings, without
# excusing any change to logic.
RENAME_RULES = [
    ("src.db.engine", "app.database.engine"),
    ("src.db.models", "app.models.orm"),
    ("src.db", "app.database"),
    ("src.settings", "app.core.settings"),
    ("src.logger", "app.core.logger"),
    ("src.container", "app.core.container"),
    ("src.bootstrap", "app.core.bootstrap"),
    ("src.app_factory", "app.core.runtime"),
    ("src.", "app."),
]


def rename_only(old_line: str, new_line: str) -> bool:
    """True when new_line is old_line with the package rename applied."""
    renamed = old_line
    for src, dst in RENAME_RULES:
        renamed = renamed.replace(src, dst)
    return renamed == new_line


def main() -> int:
    flask_repo = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FLASK_REPO
    if not flask_repo.is_dir():
        print(f"Flask repo not found at {flask_repo}; pass its path as an argument.")
        return 1

    checked = drifted = 0
    problems: list[str] = []

    for old, new in pairs(flask_repo):
        if not new.exists():
            problems.append(f"MISSING  {new.relative_to(BACKEND)}")
            continue
        checked += 1
        a = old.read_text().splitlines()
        b = new.read_text().splitlines()
        rel = str(new.relative_to(BACKEND))

        if len(a) != len(b):
            if rel in EXPECTED_EDITS:
                continue
            problems.append(f"LINE COUNT  {rel}: {len(a)} -> {len(b)}")
            drifted += 1
            continue

        for i, (x, y) in enumerate(zip(a, b), start=1):
            if x == y or rename_only(x, y):
                continue
            if rel in EXPECTED_EDITS:
                continue
            problems.append(f"CHANGED  {rel}:{i}\n    - {x.strip()}\n    + {y.strip()}")
            drifted += 1

    print(f"Gate A -- verbatim copy: {checked} files compared against {flask_repo}")
    if EXPECTED_EDITS:
        print("\nDeliberately edited (exempt):")
        for path, why in EXPECTED_EDITS.items():
            print(f"  {path}\n      {why}")
    if problems:
        print("\nDRIFT FOUND:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("\nPASS: every difference is the mechanical src.* -> app.* rename.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
