"""PyInstaller entry point for the ReviewSensei standalone executable."""

from review_sensei.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
