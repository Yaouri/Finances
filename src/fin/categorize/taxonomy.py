"""Hierarchical categories. Paths look like "Housing > Utilities > Electricity" (kind is a column)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

TAXONOMY_FILE = Path(__file__).with_name("taxonomy.yaml")
SEP = " > "


def _walk(node, parents: list[str]):
    """Yield path lists for every node of the YAML tree (parents before children)."""
    if node is None:
        return
    if isinstance(node, str):
        yield [*parents, node]
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, parents)
    elif isinstance(node, dict):
        for name, children in node.items():
            yield [*parents, name]
            yield from _walk(children, [*parents, name])


def seed(con: sqlite3.Connection, tree: dict | None = None) -> None:
    if con.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        return
    tree = tree or yaml.safe_load(TAXONOMY_FILE.read_text(encoding="utf-8"))
    ids: dict[str, int] = {}
    for kind, subtree in tree.items():
        for parts in _walk(subtree, []):
            path = SEP.join(parts)
            parent = ids.get(SEP.join(parts[:-1])) if len(parts) > 1 else None
            ids[path] = con.execute(
                "INSERT INTO categories (parent_id, name, path, kind) VALUES (?,?,?,?)",
                (parent, parts[-1], path, kind),
            ).lastrowid
    con.commit()


def path_ids(con: sqlite3.Connection) -> dict[str, int]:
    return {r["path"]: r["id"] for r in con.execute("SELECT id, path FROM categories")}


def kinds(con: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["kind"] for r in con.execute("SELECT id, kind FROM categories")}


def top_level(path: str) -> str:
    return path.split(SEP, 1)[0]
