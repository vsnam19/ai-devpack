"""Browse pack runs in the terminal or capture dashboard screenshots."""

import argparse
import asyncio
import math
from pathlib import Path


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Show fixture runs")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--runs-root")
    parser.add_argument("--state-dir")
    parser.add_argument("--project-config")
    parser.add_argument("--dashboard-config")
    parser.add_argument("--poll-interval", type=float, metavar="SECONDS",
                        help="Active refresh interval (default 2 seconds)")
    parser.add_argument("--screenshots", type=Path, metavar="OUT_DIR")
    args = parser.parse_args(argv)
    try:
        from .app import Dashboard
    except ModuleNotFoundError as error:
        if error.name == "textual":
            parser.exit(1, "Textual is unavailable. Use python3 runner/devpack_dash.py.\n")
        raise

    if args.poll_interval is not None and (not math.isfinite(args.poll_interval) or args.poll_interval <= 0):
        parser.error("--poll-interval must be a positive finite number")

    def dashboard(*, live=True):
        if args.demo:
            return Dashboard(live=live)
        from runner.dash.files import Files, JSON_LIMIT
        from runner.dash.reader import Reader
        from runner.devpack_dash import config
        from runner.workspace_scope import parse_toml_with_arrays

        from .source import RunSource

        try:
            settings = config(args)
            interval = args.poll_interval
            if interval is None and args.dashboard_config:
                raw = Files().read(Path(args.dashboard_config).absolute(), JSON_LIMIT)
                dashboard_options = parse_toml_with_arrays(raw.decode("utf-8")).get("dashboard", {})
                if not isinstance(dashboard_options, dict):
                    raise ValueError("invalid dashboard settings")
                if "active_poll_s" in dashboard_options:
                    interval = settings.active_poll_s
        except (OSError, ValueError, RecursionError):
            parser.error("invalid arguments or configuration")
        return Dashboard(live=live, source=RunSource(Reader(settings), interval=interval or 2))

    if args.screenshots is None:
        dashboard().run()
    else:
        asyncio.run(screenshots(dashboard, args.screenshots, demo=args.demo))
        print("Wrote 7 SVG screenshots.")


if __name__ == "__main__":
    main()
