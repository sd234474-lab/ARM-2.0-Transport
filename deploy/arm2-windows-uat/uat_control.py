"""Only the approved local migration and existing interactive admin bootstrap."""

import sys

from uat_policy import REVISION, readiness, validate_environment


def main():
    validate_environment()
    if sys.argv[1:] == ["migrate"]:
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("/service/alembic.ini"), REVISION)
    elif sys.argv[1:] == ["create-admin"]:
        from app.cli import main as existing_cli
        from app.db import engine

        with engine().connect() as connection:
            readiness(connection)
        sys.argv = ["app.cli", "create-admin", "--login", "uat-admin", "--name", "ARM2 UAT ADMIN"]
        existing_cli()
    else:
        raise SystemExit("Only migrate or create-admin is supported")


if __name__ == "__main__":
    main()
