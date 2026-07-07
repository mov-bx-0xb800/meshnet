from meshnet_api import MeshNetClient, MeshNetError


# Change to False after the first successful setup.
SETUP_RADIO = True

# This file contains the master node and network settings.
CONFIG_FILE = "config.master.yaml"

try:
    # Load and validate the master configuration.
    master = MeshNetClient(CONFIG_FILE)
    ready = True

    # Apply and verify the radio config on first use.
    if SETUP_RADIO:
        print("Setting up master radio...")
        setup = master.setup_radio()
        ready = setup.ok
        if not setup.ok:
            print("Setup failed:", setup.error_code)
            print("Problem:", setup.error)
            print("Fix:", setup.action)

    if ready:
        # Find and verify the configured slave before sending.
        print("Looking for the slave...")
        discovery = master.discover_report()
        ready = discovery.ok
        if discovery.ok:
            print("Slave found after", discovery.attempts, "attempt(s).")
        else:
            print("Discovery failed:", discovery.error_code)
            print("Problem:", discovery.last_error)
            print("Fix:", discovery.action)

    if ready:
        print("Master ready. Type a message, or type quit to stop.")
        while True:
            message = input("Message: ").strip()

            if message.lower() == "quit":
                break
            if not message:
                continue

            # SEND MESSAGE!!!!
            # Send to the slave defined in config.master.yaml.
            delivery = master.send_message(message)
            # SEND MESSAGE!!!!
            print("Delivered:", delivery.ok)
            print("Status:", delivery.status)
            print("Attempts:", delivery.attempts)
            if not delivery.ok:
                print("Problem:", delivery.error_code, delivery.last_error)
                print("Fix:", delivery.action)
except MeshNetError as problem:
    print("MeshNet failed:", problem.code)
    print("Problem:", problem.message)
    print("Fix:", problem.action)
except KeyboardInterrupt:
    print("Master stopped.")
