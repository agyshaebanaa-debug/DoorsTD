"""Backward-compatible launcher for the rewritten TD multi-bot platform."""

from main import main


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
