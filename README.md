[![CI](https://github.com/epics-containers/Kodman/actions/workflows/ci.yml/badge.svg)](https://github.com/epics-containers/Kodman/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/epics-containers/Kodman/branch/main/graph/badge.svg)](https://codecov.io/gh/epics-containers/Kodman)
[![PyPI](https://img.shields.io/pypi/v/kodman.svg)](https://pypi.org/project/kodman)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# Kodman

A command-line tool that provides a Docker-like experience with a Kubernetes backend.

An example use case would be to facilitate a single CI script where the runner may sometimes be a host with Docker (possibly run locally) and other times a Kubernetes executor where Docker-in-Docker is not possible (such as a Gitlab runner).

What            | Where
:---:           | :---:
Source          | <https://github.com/epics-containers/Kodman>
PyPI            | `pip install kodman`
Releases        | <https://github.com/epics-containers/Kodman/releases>

## Some examples:

Hello-world:
```
kodman run --rm hello-world
```

Handling exit codes:
```
kodman run --entrypoint bash --rm ubuntu -c "echo Enter; exit 1" && echo "You shall not pass"
```

Add files or directories into the pod filesystem:
```
mkdir demo
echo "Mellon" > demo/token.txt
kodman run -v ./demo:/demo --rm ubuntu bash -c "cat demo/token.txt"
```

Ask for CPU, for work that needs more than the namespace hands out by default:
```
kodman run --cpus 4 --rm ubuntu nproc
```
Note that `nproc` still answers with the node's core count - a container is
shown every core whether or not it may use them - so a build parallelised from
that number will oversubscribe whatever `--cpus` allows.

## Usage:

From outside of the cluster `kodman` will use your current Kubernetes context (the same as your current `kubectl` context).

From inside the cluster `kodman` will use the serviceAccount mounted by default.

## Permissions

A minimal Kubernetes RBAC role definition can be found in `.github/manifests`

# Design decisions

## Why argparse over click/typer?

The docker cli api is not POSIX compliant.

For example: `docker run --network=host imageID dnf -y install java`

Click/Typer does not allow this (and is correct). They would expect: `docker run --network=host imageID -- dnf -y install java`

See Section 12.2 Guideline 10 https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap12.html#tag_12_02
