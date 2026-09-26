"""Platform-neutral defaults; explicitly configured locations take precedence."""

from pathlib import Path


def default_export_dir() -> str:
    return str(Path.home() / "Documents" / "ShengWen" / "exports")
