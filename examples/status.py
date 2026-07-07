from meshnet_api import MeshNetClient


# Change this if your config file is elsewhere.
CONFIG_FILE = "config.master.yaml"

# Load MeshNet. This does not connect to the radio.
mesh = MeshNetClient(CONFIG_FILE)

# Show configuration, status, and known nodes.
print("Config:", mesh.config_summary())
print("Status:", mesh.status())
print("Known nodes:", mesh.registry())
