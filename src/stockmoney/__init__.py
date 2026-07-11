def main() -> None:
    """Convenience entry: apply data-layer migrations to the default DuckDB."""
    from stockmoney.data.db import migrate_cli

    migrate_cli()
