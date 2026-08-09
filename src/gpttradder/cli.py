from __future__ import annotations

import argparse
import asyncio
import logging

from .config import get_settings
from .factory import build_orchestrator
from .runtime import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="gpttradder")
    parser.add_argument("command", choices=["run", "once", "smoke"])
    parser.add_argument("--mock-decision", action="store_true", help="Use a WAIT-only local provider")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()

    async def _main():
        if args.command == "run":
            await run(settings, use_mock_decision=args.mock_decision)
            return
        orchestrator = build_orchestrator(settings, use_mock_decision=args.mock_decision or args.command == "smoke")
        await orchestrator.broker.connect()
        await orchestrator.broker.assert_demo()
        result = await orchestrator.run_cycle(trigger="POLL", reason=args.command)
        print("NO_EXECUTION" if result is None else result.model_dump_json(indent=2))

    asyncio.run(_main())


if __name__ == "__main__":
    main()
