# hagasolutions RPi temp logger

A GitHub-ready Raspberry Pi project for logging up to four configured 1-Wire temperature sensors to a local SQLite database, serving a web interface on the LAN, and exporting data as CSV, TXT, and Excel.

This starter is designed around these decisions:

- **Hardware:** Raspberry Pi 3 with two 1-Wire temperature sensors such as DS18B20
- **OS:** Raspberry Pi OS Lite, preferably **64-bit**
- **Runtime:** Docker Engine + optional Portainer
- **Persistence:** local disk mounted into the container at `/data`
- **Network:** **DHCP**, not a static IP on the Pi
- **Remote access:** another PC on the same LAN can use the web UI and JSON HTTP endpoints
- **Live update cadence:** default **10 seconds**, matching a slow 1-Wire sensor workflow

## What the project does

- Reads all detected 1-Wire temperature sensors from Linux sysfs under `/sys/bus/w1/devices`
- Stores logging periods and readings in a local SQLite database
- Lets you create a named logging period with:
  - start time as Unix epoch
  - logging interval in seconds
  - timezone offset such as `+02:00`
- Shows:
  - current sensor values
  - current logging period
  - recent logged rows
  - recent trend chart
  - period history
- Exports each logging period as:
  - `.csv`
  - `.txt`
  - `.xlsx`
- Uses filenames that include:
  - period name
  - start timestamp
  - stop timestamp

---

## Recommended architecture

The Pi host enables the 1-Wire interface. The container does **not** need direct GPIO bit-banging if Linux already exposes the sensors through the `w1` subsystem. The container only needs a **read-only bind mount** of `/sys/bus/w1/devices`, plus a persistent `/data` volume. In normal operation, **privileged mode is not required** for this design because the kernel handles the hardware and the container reads the exported files.

---

## Repository layout

```text
.
├── .github/workflows/docker-image.yml
├── .gitignore
├── .env.example
├── Dockerfile
├── README.md
├── docker-compose.yml
├── portainer-stack.yml
├── stack.env.example
├── certs/
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── database.py
│   ├── exporters.py
│   ├── main.py
│   ├── models.py
│   ├── sensors.py
│   ├── utils.py
│   ├── static/style.css
│   └── templates/index.html
└── requirements.txt
```

---

## 1. Install the OS on the Raspberry Pi

### Use Raspberry Pi Imager

Install the latest Raspberry Pi Imager on your PC and flash **Raspberry Pi OS Lite (64-bit)** to the SD card.

Raspberry Pi documents that the 64-bit version is intended for Raspberry Pi 3, 4, and 5. Docker’s current Raspberry Pi OS installation page specifically targets **32-bit Raspberry Pi OS Bookworm**, and Docker has warned that Raspberry Pi OS **32-bit / armhf** loses new major-version package support after Docker Engine v28, so 64-bit Raspberry Pi OS is the safer choice for a new Pi 3 project. citeturn856959search0turn165041search1turn165041search13

### In Raspberry Pi Imager advanced options

Set these before flashing:

- hostname: `hagasolutions-rpi-logger`
- enable SSH
- create your username and password
- configure Wi-Fi if you use Wi-Fi
- set locale and keyboard

Raspberry Pi’s hostname rules allow lowercase letters, digits, and hyphens. That means `hagasolutionsRPIlogger` should be normalized to something like `hagasolutions-rpi-logger`. Raspberry Pi also notes that a hostname lets you use mDNS, for example `my-pi.local`, instead of chasing a changing IP address. citeturn772687view2

### First boot

Insert the SD card, boot the Pi, and connect over SSH:

```bash
ssh <your-user>@hagasolutions-rpi-logger.local
```

Because you switched from fixed IP to **DHCP**, the Pi should get its address automatically from the router. Raspberry Pi’s current networking docs say DHCP is the default, and they recommend using a **DHCP reservation on the router** if you later want the Pi to consistently receive the same address without configuring a static IP on the device. citeturn772687view2

---

## 2. Enable the 1-Wire interface

Raspberry Pi OS supports enabling 1-Wire through `raspi-config`. Reboot after enabling it. citeturn772687view0turn772687view3

```bash
sudo raspi-config
```

Then:

- `3 Interface Options`
- `I7 1-Wire`
- choose **Yes**
- finish and reboot

Reboot:

```bash
sudo reboot
```

After the Pi comes back up, verify the host sees your sensors:

```bash
ls /sys/bus/w1/devices
cat /sys/bus/w1/devices/28-*/w1_slave
```

Notes:

- DS18B20 sensors use 1-Wire family code `0x28` and are supported by the Linux `w1_therm` driver. citeturn165041search7
- DS18B20 conversion time can be up to **750 ms at 12-bit resolution**, so a **10-second poll interval** is conservative and appropriate. citeturn856959search3turn856959search6
- For two sensors on one 1-Wire bus, use proper wiring and the usual pull-up resistor on the data line.

---

## 3. Install required software on Raspberry Pi OS

Update packages first:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### Install Docker Engine

Follow Docker’s Raspberry Pi OS installation guide for the host. Docker currently publishes Raspberry Pi OS instructions for **32-bit Bookworm**, while 64-bit Pi OS users should follow the Debian ARM64 package path instead of relying on old armhf assumptions. citeturn165041search1turn165041search13

A common current installation path on Raspberry Pi OS / Debian Bookworm is:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker version
```

> The convenience script is widely used on Raspberry Pi. For strict production control, you can also follow Docker’s package-repository instructions from the official docs.

### Install Docker Compose plugin

Check whether it is already available:

```bash
docker compose version
```

If not:

```bash
sudo apt install -y docker-compose-plugin
docker compose version
```

### Install Portainer

Portainer’s official docs install Portainer Server as a Docker container on Linux. citeturn165041search2

```bash
docker volume create portainer_data

docker run -d       -p 8000:8000       -p 9443:9443       --name portainer       --restart=always       -v /var/run/docker.sock:/var/run/docker.sock       -v portainer_data:/data       portainer/portainer-ce:latest
```

Open Portainer from another machine on the LAN:

```text
https://hagasolutions-rpi-logger.local:9443
```

Portainer supports deploying stacks from Compose files and editing stack environment variables from its UI. citeturn650640search4turn650640search7

---

## 4. Create the project directories on the Pi

```bash
sudo mkdir -p /opt/haga-logger/data
sudo mkdir -p /opt/haga-logger/certs
sudo chown -R $USER:$USER /opt/haga-logger
```

Optional: if you want the logs on an external SSD or USB drive for long endurance runs, mount that drive and use it as `/opt/haga-logger/data`.

---

## 5. Push this project to GitHub

On your development PC, create a new GitHub repository and copy these files into it.

Then:

```bash
git init
git add .
git commit -m "Initial Raspberry Pi temperature logger"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

---

## 6. Build method with GitHub Actions

The workflow in `.github/workflows/docker-image.yml` uses Docker’s maintained GitHub Actions to build and push a multi-platform image to **GitHub Container Registry**. Docker documents multi-platform GitHub Actions builds with Buildx, and GitHub documents GHCR as the container registry for repositories and organizations. citeturn650640search3turn650640search0turn650640search1turn650640search9

Before using the workflow:

1. Push the repo to GitHub.
2. Ensure Actions are enabled.
3. Make sure package publishing to GHCR is allowed for the repo.
4. Replace `YOUR_GITHUB_USER_OR_ORG` in the compose files if you want to reference your published image directly.

The workflow currently pushes:

- `ghcr.io/<owner>/haga-rpi-temp-logger:latest`
- `ghcr.io/<owner>/haga-rpi-temp-logger:<commit-sha>`

Supported build targets in the workflow:

- `linux/amd64`
- `linux/arm64`
- `linux/arm/v7`

That gives you a flexible path for local testing and for Raspberry Pi deployments.

---


## Rescue Wi-Fi AP for phones and unknown networks

If you want a reliable way to reach the logger on networks where you do not control DHCP, use the rescue AP setup in `deploy/network/`.

This adds:

- a Wi-Fi access point on `wlan0` for direct phone access
- an easy-to-remember URL such as `http://10.77.0.1:8080`
- a fixed hostname such as `hagasolutions-rpi-logger.local`
- an `eth0` connection profile that can run either **DHCP** or **manual/static** addressing
- a **Wired network settings** card in the logger web UI, so you can change `eth0` between DHCP and a fixed IPv4 address while connected through the rescue AP

Quick start:

```bash
cp deploy/network/fallback-ap.env.example deploy/network/fallback-ap.env
nano deploy/network/fallback-ap.env
sudo bash deploy/network/setup-fallback-ap.sh deploy/network/fallback-ap.env
```

Suggested values for a phone-friendly rescue path:

```dotenv
AP_SSID=hagasolutions-rpi-logger
AP_PASSWORD=ChangeMe1234
AP_ADDRESS_CIDR=10.77.0.1/24
WIRED_MODE=auto
```

If you really need a fixed wired address on a known network, you can either set `WIRED_MODE=manual` in the env file before running the setup script, or open the logger web UI over the rescue AP and use the **Wired network settings** card to switch `eth0` to a fixed IP.

---

## 7. Local development in VS Code

Recommended workflow:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

Then open:

```text
http://127.0.0.1:8080
```

On a non-Pi development machine without 1-Wire hardware, the app will start, but sensor endpoints will report no sensors unless you emulate `/sys/bus/w1/devices`.

---

## 8. Run with Docker Compose on the Pi

### Option A: build on the Pi

```bash
cd /opt/haga-logger
git clone https://github.com/<your-user>/<your-repo>.git .
mkdir -p data certs
cp .env.example .env
docker compose build
docker compose up -d
```

### Option B: pull from GHCR

Edit `docker-compose.yml` and set the image to your published GHCR path, then:

```bash
cd /opt/haga-logger
git clone https://github.com/<your-user>/<your-repo>.git .
mkdir -p data certs
docker compose pull
docker compose up -d
```

### Check container status

```bash
docker compose ps
docker compose logs -f
```

### Open the application

Without TLS certificates:

```text
http://hagasolutions-rpi-logger.local:8080
```

With TLS certificates present:

```text
https://hagasolutions-rpi-logger.local:8080
```

---

## 9. Portainer deployment

In Portainer:

1. Go to **Stacks**
2. Click **Add stack**
3. Name it `haga-rpi-temp-logger`
4. Upload `portainer-stack.yml`
5. Adjust the image name if needed
6. Deploy the stack

The included `portainer-stack.yml` expects:

- `/opt/haga-logger/data` on the host for the SQLite DB and exports
- `/opt/haga-logger/certs` on the host for TLS files
- `/sys/bus/w1/devices` bind-mounted read-only from the host

The included `stack.env.example` shows the runtime environment values you may want to manage in Portainer.

Note: Portainer’s `stack.env` handling is limited to values used under the `environment` section and does not behave like full Compose `.env` substitution for every field. That is why the stack file keeps bind mounts explicit. citeturn650640search2

---

## 10. HTTPS certificates

The app can run plain HTTP or HTTPS on the same port.

If these files exist inside the container:

- `/certs/cert.pem`
- `/certs/key.pem`

and the environment variables point to them, Uvicorn starts TLS.

### Generate a self-signed certificate on the Pi

```bash
cd /opt/haga-logger/certs

openssl req -x509 -nodes -days 825 -newkey rsa:2048       -keyout key.pem       -out cert.pem       -subj "/CN=hagasolutions-rpi-logger.local"
```

Then restart the stack:

```bash
docker compose restart
```

Your browser on the other PC will usually warn about trust until you import the certificate or trust your local CA.

---

## 11. Database handling

The app uses **SQLite** at:

```text
/data/logger.db
```

Why SQLite here:

- simple deployment
- low resource use on a Pi 3
- one file to back up
- good fit for a standalone appliance-style logger

The database is stored outside the container in the mounted host path, so container recreation does not erase your logs.

### Back up the database

```bash
cp /opt/haga-logger/data/logger.db /opt/haga-logger/data/logger-backup-$(date +%F-%H%M%S).db
```

### Export directory

Generated export files are written under:

```text
/data/exports
```

On the host, that is typically:

```text
/opt/haga-logger/data/exports
```

---

## 12. Web UI usage

In the web interface you can:

- create a named logging period
- set start epoch
- see the epoch rendered as local date/time
- choose interval in seconds
- choose timezone offset such as `+02:00`
- stop the current period
- see current temperatures
- see recent trend data
- export by period as CSV, TXT, or XLSX

Export filenames look like:

```text
endurance-test-01_20260423T120000Z_20260423T180000Z.csv
```

---

## 13. HTTP requests for another PC or TestStand

The app exposes JSON endpoints that another PC on the LAN can call.

Base URL examples:

```text
http://hagasolutions-rpi-logger.local:8080
https://hagasolutions-rpi-logger.local:8080
```

### Health

```http
GET /api/health
```

Example:

```bash
curl http://hagasolutions-rpi-logger.local:8080/api/health
```

### Current sensor status

```http
GET /api/status
```

Example:

```bash
curl http://hagasolutions-rpi-logger.local:8080/api/status
```

### List sessions

```http
GET /api/sessions
```

### Read logged rows for one session

```http
GET /api/sessions/{session_id}/readings?limit=200
```

Example:

```bash
curl "http://hagasolutions-rpi-logger.local:8080/api/sessions/1/readings?limit=50"
```

### Start a logging period

```http
POST /api/sessions
Content-Type: application/json
```

Example body:

```json
{
  "name": "teststand-run-001",
  "start_epoch": 1776948000,
  "interval_seconds": 10,
  "timezone_offset": "+02:00"
}
```

Example `curl`:

```bash
curl -X POST http://hagasolutions-rpi-logger.local:8080/api/sessions       -H "Content-Type: application/json"       -d '{
    "name": "teststand-run-001",
    "start_epoch": 1776948000,
    "interval_seconds": 10,
    "timezone_offset": "+02:00"
  }'
```

### Stop a logging period

```http
POST /api/sessions/{session_id}/stop
```

Example:

```bash
curl -X POST http://hagasolutions-rpi-logger.local:8080/api/sessions/1/stop
```

### Download exports

```http
GET /api/sessions/{session_id}/export?format=csv
GET /api/sessions/{session_id}/export?format=txt
GET /api/sessions/{session_id}/export?format=xlsx
```

Example:

```bash
curl -O "http://hagasolutions-rpi-logger.local:8080/api/sessions/1/export?format=csv"
```

These endpoints are suitable for a TestStand integration that can issue HTTP requests and save or parse JSON responses.

---

## 14. Sensor mapping

The app auto-detects supported 1-Wire temperature devices. To give friendly names to your two sensors, set:

```text
SENSOR_LABELS=28-000000000001:Probe A,28-000000000002:Probe B
```

You can find IDs on the Pi host with:

```bash
ls /sys/bus/w1/devices
```

---

## 15. Reliability notes for 1000+ hour operation

This starter already includes a few reliability-oriented choices:

- persistent host storage for DB and exports
- `restart: unless-stopped`
- Docker health check
- SQLite WAL mode
- no dependence on internet access during logging
- simple single-container application path

For a serious endurance deployment, also consider:

- using a good quality power supply
- using a high-endurance SD card or external SSD
- placing `/opt/haga-logger/data` on external storage
- using a UPS if brownouts are possible
- testing reboot recovery before production
- taking periodic backups of `logger.db`

---

## 16. Notes about networking with DHCP

Because you changed the design from fixed IP to DHCP, the cleanest LAN access patterns are:

- use `hagasolutions-rpi-logger.local`
- or use a **DHCP reservation** in the router if you want the IP to stay stable
- keep the Pi itself configured for DHCP

That avoids device-side static network configuration and matches Raspberry Pi’s current guidance. citeturn772687view2

---

## 17. Next improvements you may want

Good follow-on upgrades for this repo:

- authentication for the web UI
- per-sensor calibration offsets
- alarm thresholds and relay output
- NTP status display
- CSV auto-export at session stop
- API token support for external systems
- a second container for reverse proxy and stronger TLS management

---

## Quick deployment summary

```bash
# On the Pi host
sudo raspi-config   # enable 1-Wire, reboot
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
docker volume create portainer_data

# Clone your repo
sudo mkdir -p /opt/haga-logger/data /opt/haga-logger/certs
sudo chown -R $USER:$USER /opt/haga-logger
cd /opt/haga-logger
git clone https://github.com/<your-user>/<your-repo>.git .

# Start the logger
docker compose up -d --build
```

Open from your PC:

```text
http://hagasolutions-rpi-logger.local:8080
```

or, with certs:

```text
https://hagasolutions-rpi-logger.local:8080
```
