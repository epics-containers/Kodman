import argparse

from . import __version__
from .backend import Backend, DeleteOptions, RunOptions
from .engine import ArgparseEngine, Command


class KodmanEngine(ArgparseEngine):
    def __init__(self):
        if debug := self.get_env("KODMAN_DEBUG", bool):
            super().__init__(debug=debug)
        else:
            super().__init__()

        self.get_env("KODMAN_SERVICE_ACCOUNT", str)
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
        parser_run.add_argument("image")
        parser_run.add_argument("command", nargs="?")
        parser_run.add_argument("args", nargs=argparse.REMAINDER, default=[])

    def do(self, args, ctx, env, log):
        ctx.connect()
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
        )

        try:
            ctx.run(options)
            self.exit_code = ctx.return_code
        finally:
            # Clean up even when run() raised part way through, otherwise a
            # half-launched pod is left behind to be restarted/alerted on.
            # ctx.pod_name is set by run() as soon as the name is known.
            if args.rm and ctx.pod_name:
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
