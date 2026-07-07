from meshnet_api import MeshNetClient


# First-time setup must already be complete on both nodes.
# The receiving node must currently be running `meshnet slave`.

# Change these values.
CONFIG_FILE = "config.master.yaml"
MESSAGE = "Hello from MeshNet"

# Load MeshNet.
mesh = MeshNetClient(CONFIG_FILE)

# Send to the peer defined in the config file.
delivery = mesh.send_message(MESSAGE)

# Show the result.
print("Delivered:", delivery.ok)
print("Status:", delivery.status)
print("Attempts:", delivery.attempts)
if not delivery.ok:
    print("Problem:", delivery.error_code, delivery.last_error)
    print("Fix:", delivery.action)
