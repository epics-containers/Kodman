import argparse
import signal
import sys

from . import __version__
from .backend import (
    DEFAULT_POD_TTL_SECONDS,
    Backend,
    DeleteOptions,
    RunOptions,
    SweepOptions,
)
from .engine import ArgparseEngine, Command


class RunInterruptedError(Exception):
    """Raised in the main thread when a run is cut short by a signal."""

    def __init__(self, signum: int):
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum


class KodmanEngine(ArgparseEngine):
    def __init__(self):
        if debug := self.get_env("KODMAN_DEBUG", bool):
            super().__init__(debug=debug)
        else:
            super().__init__()

        self.get_env("KODMAN_SERVICE_ACCOUNT", str)
        self.get_env("KODMAN_POD_TTL", int)
        self._parser.add_argument(
            "-v",
            "--version",
            action="version",
            version=__version__,
        )
        self._ctx = Backend(self._log)


engine = KodmanEngine()


@engine.add_command
class Run(Command):
    def add(self, parser):
        parser_run = parser.add_parser("run", help="Run a command in a new container")
        parser_run.add_argument(
            "--entrypoint",
            type=str,
            help="Overwrite the default ENTRYPOINT of the image",
        )
        parser_run.add_argument(
            "--rm",
            help="Remove the container after exit",
            action="store_true",
        )
        parser_run.add_argument(
            "--volume",
            "-v",
            type=str,
            action="append",
            help="Bind mount a volume into the container",
        )
        parser_run.add_argument(
            "--cpus",
            type=str,
            help="CPU the container may use, e.g. 4 or 500m. Without this the "
            "pod takes whatever the namespace defaults to - often far less "
            "than the node has, while the container still sees every core",
        )
        parser_run.add_argument("image")
        parser_run.add_argument("command", nargs="?")
        parser_run.add_argument("args", nargs=argparse.REMAINDER, default=[])

    def do(self, args, ctx, env, log):
        ctx.connect()

        # Reap what earlier runs left behind before adding to the pile.
        ttl = env.get("KODMAN_POD_TTL")
        ctx.sweep(SweepOptions(DEFAULT_POD_TTL_SECONDS if ttl is None else ttl))

        log.debug(f"Image: {args.image}")
        k8s_command = []
        k8s_args = []
        if args.entrypoint:
            k8s_command = [args.entrypoint]
        if args.command:
            k8s_args.append(args.command)
        if args.args:
            k8s_args += args.args

        log.debug(f"Command: {k8s_command}")
        log.debug(f"Args: {k8s_args}")

        service_a = env["KODMAN_SERVICE_ACCOUNT"]
        options = RunOptions(
            image=args.image,
            command=k8s_command,
            args=k8s_args,
            volumes=args.volume,
            service_account=service_a if service_a else "",
            cpus=args.cpus if args.cpus else "",
        )

        def _on_signal(signum, _frame):
            raise RunInterruptedError(signum)

        # Without this the pod outlives an interrupted client - a cancelled CI
        # job or a Ctrl-C leaves it running, burning the CPU it was given until
        # it finishes on its own. Kubernetes has no way to stop a pod short of
        # deleting it, so an interrupted run removes its pod whether or not
        # --rm was asked for.
        previous = {
            sig: signal.signal(sig, _on_signal)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        interrupted = False

        try:
            ctx.run(options)
            self.exit_code = ctx.return_code
        except RunInterruptedError as interrupt:
            interrupted = True
            self.exit_code = 128 + interrupt.signum  # Shell convention
            print(f"Interrupted, removing pod {ctx.pod_name}", file=sys.stderr)
        finally:
            # Restore first: a second Ctrl-C during cleanup should kill kodman
            # outright rather than re-enter this handler.
            for sig, handler in previous.items():
                signal.signal(sig, handler)

            # Clean up even when run() raised part way through, otherwise a
            # half-launched pod is left behind to be restarted/alerted on.
            # ctx.pod_name is set by run() as soon as the name is known.
            if (args.rm or interrupted) and ctx.pod_name:
                ctx.delete(DeleteOptions(ctx.pod_name))


@engine.add_command
class Version(Command):
    def add(self, parser):
        parser.add_parser("version", help="Display the kodman version information")

    def do(self, args, ctx, env, log):
        print(__version__)


def cli():
    engine.launch()


if __name__ == "__main__":
    cli()
