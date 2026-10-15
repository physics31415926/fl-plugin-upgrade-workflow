"""Convenient entry point: configure or start the dashboard."""
import argparse
import importlib

def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure or start the monitoring dashboard")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", add_help=False, help="Generate project configuration")
    commands.add_parser("start", add_help=False, help="Start a configured dashboard")
    args, remaining = parser.parse_known_args(argv)
    module = "configure" if args.command == "configure" else "start_dashboard"
    return importlib.import_module("fl_dashboard.deployment." + module).main(remaining)

if __name__ == "__main__":
    raise SystemExit(main())
