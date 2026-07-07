from meshnet_api import MeshNetClient, MeshNetError


# Change to False after the first successful setup.
SETUP_RADIO = True

# This file contains the slave node and network settings.
CONFIG_FILE = "config.slave.yaml"

try:
    # Load and validate the slave configuration.
    slave = MeshNetClient(CONFIG_FILE)
    ready = True

    # Apply and verify the radio config on first use.
    if SETUP_RADIO:
        print("Setting up slave radio...")
        setup = slave.setup_radio()
        ready = setup.ok
        if not setup.ok:
            print("Setup failed:", setup.error_code)
            print("Problem:", setup.error)
            print("Fix:", setup.action)

    if ready:
        # Listen continuously. Disconnects are retried automatically.
        print("Slave ready. Listening for messages. Press Ctrl+C to stop.")
        slave.run_slave()
except MeshNetError as problem:
    print("MeshNet failed:", problem.code)
    print("Problem:", problem.message)
    print("Fix:", problem.action)
except KeyboardInterrupt:
    print("Slave stopped.")
