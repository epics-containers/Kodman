help_screen = """usage: kodman"""

hello_world = """Hello from Docker!"""

mount = "test data"

failed_image = "Failed to pull image"

# Trailing wording after "$PATH" varies by container runtime (podman appends
# ": unknown", docker/runc does not), so match only the stable core message.
failed_command = 'exec: "bash": executable file not found in $PATH'
