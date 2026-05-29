import io
import logging
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kubernetes import client, config, watch
from kubernetes.client.models.core_v1_event_list import CoreV1EventList
from kubernetes.client.models.v1_pod import V1Pod
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream


@dataclass(frozen=True)
class RunOptions:
    image: str
    command: list[str] = field(default_factory=lambda: [])
    args: list[str] = field(default_factory=lambda: [])
    volumes: list[str] = field(default_factory=lambda: [])
    service_account: str = field(default_factory=lambda: "")

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

    def run(self, options: RunOptions) -> str:
        unique_pod_name = f"kodman-run-{hash(options)}"
        init_container_name = "wait-for-signal"
        namespace = self._context["namespace"]
        pod_manifest: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": unique_pod_name,
            },
            "spec": {
                "initContainers": [
                    {
                        "name": init_container_name,
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
            self._log.debug(f"Using serviceAccountNam: '{options.service_account}'")
            pod_manifest["spec"]["serviceAccountName"] = options.service_account

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
                self._log.info(f"Mount: {src} to {dst}")
                if src.is_dir():
                    self._log.debug(f"Volume target {src} is a directory")
                    dst_mount = dst
                else:
                    self._log.debug(f"Volume target {src} is a file")
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

        # Attach to pod logging
        self._log.info("Try attach to pod logs")
        w = watch.Watch()
        for e in w.stream(
            self._client.read_namespaced_pod_log,
            name=unique_pod_name,
            namespace=namespace,
            follow=True,
        ):
            print(e)
        self._log.info("Execution complete")
        w.stop()

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
