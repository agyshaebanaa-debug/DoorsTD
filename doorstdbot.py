from __future__ import annotations

"""Compatibility entry point containing the full TD/BaaS application assembly."""

import asyncio

from main import main


if __name__ == "__main__":
    asyncio.run(main())
