import sys


def run() -> None:
    from .__main__ import main

    raise SystemExit(main())
