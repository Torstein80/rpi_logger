# Rescue Wi-Fi AP and wired IP profiles

This folder adds an OS-level network setup for Raspberry Pi OS Bookworm using NetworkManager.

## What it does

- Creates a **rescue Wi-Fi access point** on `wlan0`
- Lets you reach the logger directly from a phone at a fixed AP address, for example `http://10.77.0.1:8080`
- Keeps a predictable hostname such as `hagasolutions-rpi-logger.local`
- Creates a wired `eth0` profile that can run in either:
  - **DHCP** mode, or
  - **manual/static** mode, for example `10.87.0.5/24`
- Exposes the current network status and wired IP settings inside the logger web UI, so you can switch `eth0` between DHCP and a fixed IP while connected through the rescue AP

## Why this design

Instead of trying to guess every possible network failure, this design keeps a **rescue AP available whenever the Pi boots**. That gives you a reliable way to reach the logger from a phone even on a network where you do not control DHCP.

## Files

- `fallback-ap.env.example` – copy and edit this
- `setup-fallback-ap.sh` – creates the NetworkManager profiles

## Setup

```bash
cd /opt/haga-logger
cp deploy/network/fallback-ap.env.example deploy/network/fallback-ap.env
nano deploy/network/fallback-ap.env
sudo bash deploy/network/setup-fallback-ap.sh deploy/network/fallback-ap.env
```

## Recommended profile values

### Rescue AP

- `AP_SSID=hagasolutions-rpi-logger`
- `AP_ADDRESS_CIDR=10.77.0.1/24`

Then on a phone:

- connect to the Wi-Fi SSID
- browse to `http://10.77.0.1:8080`
- if mDNS works on the phone/network, `http://hagasolutions-rpi-logger.local:8080` should also work

### Wired network, DHCP

```dotenv
WIRED_MODE=auto
```

### Wired network, fixed IP

```dotenv
WIRED_MODE=manual
WIRED_IP_CIDR=10.87.0.5/24
WIRED_GATEWAY=10.87.0.1
WIRED_DNS=10.87.0.1 1.1.1.1
```

## Web UI control of the wired IP

After the AP has been set up and the updated container is running, the main logger page includes a **Wired network settings** card. From there you can:

- view the rescue AP URL and current hostname
- see the current wired connection name and active IP
- switch wired `eth0` between **DHCP** and **Fixed IP**
- enter IPv4 / CIDR, gateway, and DNS when Fixed IP is selected

For this to work, the container must have:

- `nmcli` installed
- `/run/dbus/system_bus_socket` mounted from the host
- `/etc/machine-id` mounted read-only from the host

The provided Docker Compose and Portainer stack files already include those mounts.

## Notes

- A static wired IP only works if it matches the wired network you plug into.
- On unknown customer networks, **DHCP is still the safest default**.
- The rescue AP gives you a second path into the logger even if the wired network addressing is unknown.
- The setup script can optionally bring down an existing Wi-Fi client connection on `wlan0`, because the same radio normally cannot stay connected to another Wi-Fi network and host an AP at the same time.
