import io
import logging
import sys
import tarfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from kubernetes import client, config
from kubernetes.client.models.core_v1_event_list import CoreV1EventList
from kubernetes.client.models.v1_pod import V1Pod
from kubernetes.client.models.v1_pod_list import V1PodList
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from urllib3 import HTTPResponse


@dataclass(frozen=True)
class RunOptions:
    image: str
    command: list[str] = field(default_factory=lambda: [])
    args: list[str] = field(default_factory=lambda: [])
    volumes: list[str] = field(default_factory=lambda: [])
    service_account: str = field(default_factory=lambda: "")
    cpus: str = field(default_factory=lambda: "")

    def __hash__(self):
        hash_candidates = (
            self.image,
            self.command,
            self.args,
            self.volumes,
            time.time(),  # Add timestamp
        )

        to_hash = []
        for item in hash_candidates:
            if not item:  # Skip unhashable falsy items
                pass
            elif type(item) is list:  # Make hashable
                to_hash.append(tuple(item))
            else:
                to_hash.append(item)

        _hash = hash(tuple(to_hash))
        _hash += sys.maxsize + 1  # Ensure always positive
        return _hash


@dataclass(frozen=True)
class DeleteOptions:
    name: str


@dataclass(frozen=True)
class SweepOptions:
    ttl_seconds: int


INIT_CONTAINER_NAME = "wait-for-signal"

# Marks every pod kodman creates, so a later run can find the ones an earlier
# run left behind. Pods made before this label existed are invisible to the
# sweep and have to be deleted by hand.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "kodman"
MANAGED_BY_SELECTOR = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"

# Phases in which a pod is finished with: nothing is running, nothing will run
# again (restartPolicy is Never), and no kodman is still attached to its logs.
# Anything else may belong to a run happening right now, so is never touched.
TERMINAL_PHASES = ("Succeeded", "Failed")

# How long a finished pod is kept for post-mortem inspection before the next
# run reaps it. Without --rm a pod otherwise lives forever: a bare Pod has no
# equivalent of a Job's ttlSecondsAfterFinished.
DEFAULT_POD_TTL_SECONDS = 3600


def build_pod_manifest(
    options: RunOptions, log: logging.Logger
) -> tuple[str, dict[str, Any], list[dict[str, Path]]]:
    """Build the one-shot pod manifest for a ``kodman run``.

    Returns ``(unique_pod_name, pod_manifest, volumes)`` where ``volumes`` is
    the cache of ``{"src", "dst"}`` mappings the caller later streams into the
    init container with :func:`cp_k8s`.

    The pod is labelled ``app.kubernetes.io/managed-by=kodman`` so that
    :meth:`Backend.sweep` can find and reap it once it is finished with.

    ``restartPolicy`` is forced to ``"Never"``. kodman pods are run-to-completion
    (docker-``run`` style), but the Pod default is ``restartPolicy=Always``, so a
    failed launch would be restarted indefinitely by the kubelet — a
    CrashLoopBackOff that boot-loops and spams alerts.
    """
    unique_pod_name = f"kodman-run-{hash(options)}"
    pod_manifest: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": unique_pod_name,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
        },
        "spec": {
            "restartPolicy": "Never",
            "initContainers": [
                {
                    "name": INIT_CONTAINER_NAME,
                    "image": "busybox",
                    "command": [
                        "sh",
                        "-c",
                        "until [ -f /tmp/trigger ];"
                        'do echo "Waiting for trigger...";'
                        "sleep 1;"
                        "done;"
                        'echo "Trigger file found!"',
                    ],
                    "volumeMounts": [],
                },
            ],
            "containers": [
                {
                    "image": options.image,
                    "name": "kodman-exec",
                    "volumeMounts": [],
                }
            ],
            "volumes": [],
        },
    }

    if options.command:
        container = pod_manifest["spec"]["containers"][0]
        container["command"] = options.command

    if options.args:
        pod_manifest["spec"]["containers"][0]["args"] = options.args

    if options.service_account:
        log.debug(f"Using serviceAccountNam: '{options.service_account}'")
        pod_manifest["spec"]["serviceAccountName"] = options.service_account

    if options.cpus:
        # docker's --cpus is a ceiling on how much CPU the container may use,
        # which k8s spells limits.cpu. Requesting the same amount rather than
        # leaving the request to a LimitRange default keeps the pod off the
        # throttle for work it has been promised, and makes it cost the
        # cluster what it can actually use.
        log.debug(f"Requesting cpu: '{options.cpus}'")
        pod_manifest["spec"]["containers"][0]["resources"] = {
            "requests": {"cpu": options.cpus},
            "limits": {"cpu": options.cpus},
        }

    volumes: list[dict[str, Path]] = []
    if options.volumes:
        for i, options_volume in enumerate(options.volumes):
            process = options_volume.split(":")
            src = Path(process[0]).resolve()
            if not src.exists():
                raise FileNotFoundError(f"{src} does not exist")
            dst = src  # In case no dst, set same as src
            try:
                dst = Path(process[1])
            except IndexError:
                pass
            if not dst.is_absolute():
                raise ValueError("Destination path must be absolute")
            log.info(f"Mount: {src} to {dst}")
            if src.is_dir():
                log.debug(f"Volume target {src} is a directory")
                dst_mount = dst
            else:
                log.debug(f"Volume target {src} is a file")
                dst_mount = dst.parent
                if dst_mount == Path("/"):
                    raise NotImplementedError(
                        "Root mounting of files not supported by k8s 'emptyDir'"
                    )

            volumes.append({"src": src, "dst": dst})  # cache for later

            pod_manifest["spec"]["initContainers"][0]["volumeMounts"].append(
                {"name": f"shared-data-{i}", "mountPath": str(dst_mount)}
            )
            pod_manifest["spec"]["containers"][0]["volumeMounts"].append(
                {"name": f"shared-data-{i}", "mountPath": str(dst_mount)}
            )
            pod_manifest["spec"]["volumes"].append(
                {
                    "name": f"shared-data-{i}",
                    "emptyDir": {},
                }
            )

    return unique_pod_name, pod_manifest, volumes


def _iter_log_lines(resp):
    """Yield decoded log lines from a streamed (``_preload_content=False``)
    read_namespaced_pod_log response.

    Mirrors the newline-splitting behaviour of kubernetes.watch's internal
    iterator so output matches the previous Watch-based implementation, while
    sidestepping the version-fragile follow/watch argument detection (issue
    #51).
    """
    buffer = bytearray()
    for segment in resp.stream(amt=None, decode_content=False):
        if isinstance(segment, str):
            segment = segment.encode("utf-8")
        buffer.extend(segment)

        next_newline = buffer.find(b"\n")
        while next_newline != -1:
            line = buffer[:next_newline].decode("utf-8", errors="replace")
            del buffer[: next_newline + 1]
            yield line
            next_newline = buffer.find(b"\n")

    if buffer:  # Trailing data with no final newline
        yield buffer.decode("utf-8", errors="replace")


def cp_k8s(
    kube_conn: client.CoreV1Api,
    namespace: str,
    pod_name: str,
    container: str,
    source_path: Path,
    dest_path: Path,
    log: logging.Logger,
):
    log.info(f"Transferring {source_path} to {dest_path}")
    buf = io.BytesIO()

    log.debug(f"Compressing {source_path}")
    with tarfile.open(fileobj=buf, mode="w:tar") as tar:  # To compress set 'w:gz'
        tar.add(source_path, arcname=dest_path)
    buf.seek(0)
    compressed_size = buf.getbuffer().nbytes
    log.debug(f"Compressed to {compressed_size} bytes")

    exec_command = ["tar", "xvf", "-", "-C", "/"]  # To decompress set 'xzvf'
    resp = stream(
        kube_conn.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        container=container,
        command=exec_command,
        stderr=True,
        stdin=True,
        stdout=True,
        tty=False,
        _preload_content=False,
    )

    chunk_size = 10 * 1024 * 1024
    steps = -(compressed_size // -chunk_size)  # Ceiling division
    log.debug(f"Transferring {steps} chunks")
    counter = 0
    while resp.is_open():
        resp.update(timeout=1)
        if read := buf.read(chunk_size):
            resp.write_stdin(read)
        else:
            log.debug("Empty buffer")
            break
        log.info(f"Transfer {counter * 100 // steps}% completed")
        counter += 1
    resp.close()
    log.info("Transfer done")


def get_incluster_context():
    ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    context = {}
    with open(ns_path) as f:
        context["namespace"] = f.read().strip()
    context["cluster"] = "default"
    context["user"] = "default"
    return context


class Backend:
    def __init__(self, log):
        self.return_code = 0
        # Name of the pod created by the most recent run(). Set as early as
        # possible so callers can still clean up (e.g. `--rm`) when run()
        # raises part way through.
        self.pod_name = ""
        self._log = log
        self._polling_freq = 1
        self._grace_period = 2  # Is this too aggressive?

    def connect(self):
        # Load config for user/serviceaccount
        # https://github.com/kubernetes-client/python/issues/1005
        try:
            self._log.info(
                "Loading kube config for user interaction from outside of cluster"
            )
            config.load_kube_config()
            self._log.info("Loaded kube config successfully")
            self._context = config.list_kube_config_contexts()[1]["context"]
        except config.config_exception.ConfigException:
            self._log.info("Failed to load kube config, trying in-cluster config")
            config.load_incluster_config()
            self._log.info("Loaded in-cluster config successfully")
            self._context = get_incluster_context()

        self._client = client.CoreV1Api()
        self._log.debug("The current context is:")
        self._log.debug(f"  Cluster: {self._context['cluster']}")
        self._log.debug(f"  Namespace: {self._context['namespace']}")
        self._log.debug(f"  User: {self._context['user']}")

    def sweep(self, options: SweepOptions) -> list[str]:
        """Delete finished kodman pods left behind by earlier runs.

        A pod outlives the kodman that created it whenever ``--rm`` was not
        given, or the client was killed before it could clean up. Nothing in
        Kubernetes collects a bare Pod, so they accumulate in the namespace
        until somebody notices. Each run therefore reaps the leftovers of the
        runs before it.

        Only pods in a terminal phase older than ``ttl_seconds`` are removed:
        a Pending or Running pod may belong to a kodman running right now.
        A negative ttl disables the sweep. Returns the names deleted.

        Failure here is never fatal - a namespace we cannot list or delete in
        is a reason to warn and get on with the run, not to abandon it.
        """
        if options.ttl_seconds < 0:
            self._log.debug("Pod sweep disabled")
            return []

        namespace = self._context["namespace"]
        try:
            pod_list = self._client.list_namespaced_pod(
                namespace=namespace,
                label_selector=MANAGED_BY_SELECTOR,
            )
        except ApiException as e:
            self._log.warning(f"Could not list pods to sweep: {e}")
            return []
        if not isinstance(pod_list, V1PodList):  # Runtime type checking
            # Warn rather than raise: a client whose response type moved under
            # us must not take down every run, which is how the pod log stream
            # broke on kubernetes 36.x (issue #51).
            self._log.warning(f"Unexpected response type to sweep: {type(pod_list)}")
            return []

        now = datetime.now(UTC)
        deleted = []
        for pod in pod_list.items or []:
            name = pod.metadata.name if pod.metadata else None
            phase = pod.status.phase if pod.status else None
            if not name or phase not in TERMINAL_PHASES:
                continue

            created = pod.metadata.creation_timestamp
            age = (now - created).total_seconds() if created else 0
            if age < options.ttl_seconds:
                self._log.debug(f"Keeping {name}: {phase} for only {age:.0f}s")
                continue

            self._log.info(f"Sweeping {name}: {phase} for {age:.0f}s")
            try:
                self._client.delete_namespaced_pod(
                    name=name,
                    namespace=namespace,
                    grace_period_seconds=self._grace_period,
                )
                deleted.append(name)
            except ApiException as e:
                if e.status != 404:  # Somebody else got there first
                    self._log.warning(f"Could not sweep pod {name}: {e}")

        if deleted:
            self._log.info(f"Swept {len(deleted)} finished pod(s)")
        return deleted

    def run(self, options: RunOptions) -> str:
        namespace = self._context["namespace"]
        unique_pod_name, pod_manifest, volumes = build_pod_manifest(options, self._log)
        init_container_name = INIT_CONTAINER_NAME
        # Record the name immediately so a failed run() is still cleanable.
        self.pod_name = unique_pod_name

        self._log.debug(f"Pod manifest = {pod_manifest}")

        # Schedule pod and block until ready
        self._log.info(f"Creating pod: {unique_pod_name}")
        self._client.create_namespaced_pod(body=pod_manifest, namespace=namespace)
        while True:
            read_resp = self._client.read_namespaced_pod(
                name=unique_pod_name, namespace=namespace
            )
            # Runtime type checking
            if isinstance(read_resp, V1Pod):
                if not read_resp.status:
                    raise ValueError("Empty pod status")
                if read_resp.status.init_container_statuses:
                    init_status = read_resp.status.init_container_statuses[0]
                    if init_status.state.running:
                        self._log.info("Init container is running")
                        break
            else:
                raise TypeError("Unexpected response type")

            self._log.info("Awaiting init container...")
            time.sleep(1 / self._polling_freq)

        # Fill volumes
        for volume in volumes:
            cp_k8s(
                self._client,
                namespace,
                unique_pod_name,
                init_container_name,
                volume["src"],
                volume["dst"],
                log=self._log,
            )

        # Start execution
        self._log.info("Execution start")
        exec_command = [
            "/bin/sh",
            "-c",
            "touch /tmp/trigger",
        ]
        _ = stream(
            self._client.connect_get_namespaced_pod_exec,
            unique_pod_name,
            namespace,
            container=init_container_name,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        while True:
            read_resp = self._client.read_namespaced_pod(
                name=unique_pod_name, namespace=namespace
            )
            if isinstance(read_resp, V1Pod):  # Runtime type checking
                if not read_resp.status:
                    raise ValueError("Empty pod status")
                elif read_resp.status.phase != "Pending":
                    self._log.info(f"Pod status: {read_resp.status.phase}")
                    break
                self._log.info(f"Pod status: {read_resp.status.phase}")
                time.sleep(1 / self._polling_freq)
                events = self._client.list_namespaced_event(
                    namespace=namespace,
                    field_selector=f"involvedObject.name={unique_pod_name}",
                )
                if not isinstance(events, CoreV1EventList):  # Runtime type checking
                    raise TypeError("Unexpected response type")
                for event in events.items or []:
                    if event.type == "Warning":
                        self.return_code = 1
                        reason = event.type
                        message = event.message
                        self._log.debug(f"{reason}: {message}")
                        print(message, file=sys.stderr)
                        return unique_pod_name
            else:
                raise TypeError("Unexpected response type")

        # Attach to pod logging.
        #
        # We deliberately do not use kubernetes.watch.Watch here. Watch.stream
        # inspects the API method's docstring to decide whether to pass
        # follow=True or watch=True, and as of the kubernetes 36.x client the
        # docstring format for read_namespaced_pod_log changed so that
        # detection fails and an invalid watch=True is sent, raising
        # ApiTypeError (see issue #51). Streaming the log directly avoids that
        # fragility entirely.
        self._log.info("Try attach to pod logs")
        # With _preload_content=False the client returns the raw urllib3
        # response, but the generated stubs still type it as the deserialized
        # body, so narrow it here.
        resp = cast(
            HTTPResponse,
            self._client.read_namespaced_pod_log(
                name=unique_pod_name,
                namespace=namespace,
                follow=True,
                _preload_content=False,
            ),
        )
        try:
            for line in _iter_log_lines(resp):
                print(line)
        finally:
            resp.close()
            resp.release_conn()
        self._log.info("Execution complete")

        # Check exit codes
        final_pod = self._client.read_namespaced_pod(
            name=unique_pod_name,
            namespace=namespace,
        )
        if isinstance(final_pod, V1Pod):  # Runtime type checking
            if not final_pod.status:
                raise ValueError("Empty pod status")
            container_status = final_pod.status.container_statuses[0]
            while not container_status.state.terminated:
                # Exit early if container didnt even start
                if not container_status.started:
                    self._log.info("Container failed to start")
                    self.return_code = 1
                    reason = container_status.state.waiting.reason
                    message = container_status.state.waiting.message
                    self._log.debug(f"{reason}: {message}")
                    print(message, file=sys.stderr)
                    return unique_pod_name

                self._log.info("Awaiting pod termination...")
                time.sleep(1 / self._polling_freq)
                final_pod = self._client.read_namespaced_pod(
                    name=unique_pod_name,
                    namespace=namespace,
                )
                container_status = final_pod.status.container_statuses[0]  # type: ignore
            self.return_code = container_status.state.terminated.exit_code

        return unique_pod_name

    def delete(self, options: DeleteOptions):
        namespace = self._context["namespace"]
        try:
            exists_resp = self._client.read_namespaced_pod(
                name=options.name,
                namespace=namespace,
            )
            self._client.delete_namespaced_pod(
                name=options.name,
                namespace=namespace,
                grace_period_seconds=self._grace_period,
            )
            while exists_resp:
                self._log.info("Awaiting pod cleanup...")
                try:
                    exists_resp = self._client.read_namespaced_pod(
                        name=options.name,
                        namespace=namespace,
                    )
                    time.sleep(1 / self._polling_freq)
                except ApiException as e:
                    if e.status == 404:
                        self._log.info(f"Pod {options.name} deleted successfully")
                        break
                    else:
                        raise e

        except ApiException as e:
            self._log.info(f"Error deleting pod: {e}")
