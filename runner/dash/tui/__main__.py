"""Run the fixture dashboard or capture the design review screens."""

import argparse
import asyncio
from pathlib import Path


async def screenshots(dashboard, destination):
    destination.mkdir(parents=True, exist_ok=True)
    app = dashboard(live=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app.save_screenshot("01-overview.svg", path=str(destination))

        await pilot.press("2", "j", "j", "3", "]")
        for _ in range(5):
            app.tick()
        await pilot.pause()
        app.save_screenshot("02-task-agents-log.svg", path=str(destination))

        await pilot.press("2", "j", "3", "k", "k", "k", "]")
        await pilot.pause()
        app.save_screenshot("03-gate-failure.svg", path=str(destination))

        await pilot.press("1", "]")
        await pilot.pause()
        app.save_screenshot("04-plan-graph.svg", path=str(destination))

        await pilot.press("a")
        await pilot.pause()
        app.save_screenshot("05-approvals.svg", path=str(destination))

        await pilot.press("?")
        await pilot.pause()
        app.save_screenshot("06-help.svg", path=str(destination))

    app = dashboard(live=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("2", "j", "j", "]")
        await pilot.pause()
        app.save_screenshot("07-80x24.svg", path=str(destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", required=True)
    parser.add_argument("--screenshots", type=Path, metavar="OUT_DIR")
    args = parser.parse_args()
    try:
        from .app import Dashboard
    except ModuleNotFoundError as error:
        if error.name == "textual":
            parser.exit(1, "The dashboard requires Textual. Use the demo interpreter.\n")
        raise
    if args.screenshots is None:
        Dashboard().run()
    else:
        asyncio.run(screenshots(Dashboard, args.screenshots))
        print(f"Wrote 7 SVG screenshots to {args.screenshots}")


if __name__ == "__main__":
    main()
