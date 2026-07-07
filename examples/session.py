from meshnet_api import MeshNetSession


# First-time setup must already be complete on both nodes.
# The receiving node must currently be running `meshnet slave`.

# Change this value.
CONFIG_FILE = "config.master.yaml"

# Keep one radio connection open for multiple operations.
with MeshNetSession(CONFIG_FILE) as mesh:
    # The destination comes from the config file.
    ping = mesh.ping()
    print("Ping delivered:", ping.ok)
    if not ping.ok:
        print("Problem:", ping.error_code, ping.last_error)
        print("Fix:", ping.action)

    message = mesh.send_message("Hello from MeshNet")
    print("Message delivered:", message.ok)
    if not message.ok:
        print("Problem:", message.error_code, message.last_error)
        print("Fix:", message.action)
