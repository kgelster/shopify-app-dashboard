"""Entrypoint: apply pending migrations. Run as `python -m partner_dashboard.migrate`."""

from partner_dashboard.db import connect, run_migrations


def main() -> None:
    run_migrations(connect())


if __name__ == "__main__":
    main()
