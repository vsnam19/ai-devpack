"""Browse pack runs in the terminal or capture dashboard screenshots."""

import asyncio
import math
from pathlib import Path

from runner.devpack_dash import config, parser as dashboard_parser


async def screenshots(dashboard, destination, *, demo):
    destination.mkdir(parents=True, exist_ok=True)
    app = dashboard(live=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot("01-overview.svg", path=str(destination))

        await pilot.press("2", "j", "j", "3", "]")
        if demo:
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
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("2", "j", "j", "]")
        await pilot.pause()
        app.save_screenshot("07-80x24.svg", path=str(destination))


def parser():
    cli = dashboard_parser(description=__doc__)
    cli.add_argument("--demo", action="store_true", help="Show fixture runs")
    cli.add_argument("--poll-interval", type=float, metavar="SECONDS",
                     help="Active refresh interval (default 2 seconds)")
    cli.add_argument("--screenshots", type=Path, metavar="OUT_DIR")
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        from .app import Dashboard
    except ModuleNotFoundError as error:
        if error.name == "textual":
            cli.exit(1, "Textual is unavailable. Use python3 runner/devpack_dash.py.\n")
        raise

    if args.poll_interval is not None and (not math.isfinite(args.poll_interval) or args.poll_interval <= 0):
        cli.error("--poll-interval must be a positive finite number")

    def dashboard(*, live=True):
        if args.demo:
            return Dashboard(live=live)
        from runner.dash.reader import Reader

        from .source import RunSource

        try:
            settings = config(args, active_poll_default=2)
        except (OSError, ValueError, RecursionError):
            cli.error("invalid arguments or configuration")
        interval = args.poll_interval if args.poll_interval is not None else settings.active_poll_s
        return Dashboard(live=live, source=RunSource(Reader(settings), interval=interval))

    if args.screenshots is None:
        dashboard().run()
    else:
        asyncio.run(screenshots(dashboard, args.screenshots, demo=args.demo))
        print("Wrote 7 SVG screenshots.")


if __name__ == "__main__":
    main()
