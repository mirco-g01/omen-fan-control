# HP Omen Fan Control (Linux)

> **Fork notice.** This is a fork of [arfelious/omen-fan-control](https://github.com/arfelious/omen-fan-control)
> by [arfelious](https://github.com/arfelious), who did the original work (driver patch backport, calibration,
> curve editor, GUI and CLI). All credit for the base tool goes to them. The original code is
> Copyright © 2026 arfelious and is released under the GNU GPL v3; this fork keeps the same
> license (see [LICENSE.md](LICENSE.md)). Changes made in this fork (Copyright © 2026 Mirco Giorgi) are
> listed in [Changes in this fork](#changes-in-this-fork).
>
> This fork is based on upstream commit `0ba2bc6` (2026-03-07). Upstream has since moved on to
> v2.0.0 with a new `src/` package layout, GPU-temperature curves and packaging; those changes are
> **not** included here. Fan cleaning is implemented here independently, using the same WMI
> protocol upstream found.

This tool provides fan control for HP Omen Max, Victus and Omen laptops on Linux. It includes installer for a kernel driver patch (`hp-wmi`) to expose PWM controls and a userspace utility to manage fan curves, create watchdog that sets the fan configuration periodically and a simple stress test tool to see the fan curve in effect.

## Context

This tool includes a backported `hp-wmi` driver patch from the upcoming Linux 6.20 kernel, which introduces native fan control support for many devices from the following models:
1.  **HP Omen Max**
2.  **HP Victus**
3.  **HP Omen**

The patch can be installed on versions before `6.20`.

**Reference Kernel Commit:**
[platform/x86: hp-wmi: add manual fan control for Victus S models](https://git.kernel.org/pub/scm/linux/kernel/git/pdx86/platform-drivers-x86.git/commit/?h=for-next&id=46be1453e6e61884b4840a768d1e8ffaf01a4c1c)

This program also includes a modification that sets the max speed according to calibration if the query to get the Max RPM fails for your device.
## Tested Hardware

*   **Model:** HP OMEN MAX 16-AH0001NT (8D41)
*   **OS:** Arch Linux 6.18.6

## Installation

### Clone the repository
Clone the repository or download the latest source code from the [Releases](https://github.com/arfelious/omen-fan-control/releases) page.

```bash
git clone https://github.com/arfelious/omen-fan-control.git
cd omen-fan-control
```

### Dependencies

**1. System Dependencies**
You must install kernel headers and build tools for the driver patch to compile.
*   **Arch:** `pacman -S linux-headers base-devel`
*   **Debian/Ubuntu:** `apt install linux-headers-$(uname -r) linux-headers-$(uname -r | sed 's/-[^-]*$/-common/') "linux-kbuild-$(uname -r | cut -d. -f1,2,3 | cut -d+ -f1)*" build-essential`

    > **Note:** Debian and Ubuntu split kernel headers into multiple packages: an arch-specific one (`linux-headers-<version>-amd64`), a common one (`linux-headers-<version>-common`), and a build scripts package (`linux-kbuild-<version>`). All must be installed for the module build to succeed. 
    >
    >If you get a similar after installing the headers, try `sudo apt reinstall linux-headers-$(uname -r) linux-headers-$(uname -r | sed 's/-[^-]*$/-common/') "linux-kbuild-$(uname -r | cut -d. -f1,2,3 | cut -d+ -f1)*" build-essential` and reboot.


*   **CachyOS / Clang Kernels:** The Makefile detects if your kernel was built with Clang/LLVM and passes the correct `LLVM=1` flags to the build system. No extra configuration is needed.

**2. Python Dependencies**
You can install the required Python packages (`click`, `PyQt6`) via your package manager OR pip, preferably with a virtual environment.

*   **Option A: Package Manager**
    *   **Arch:** `pacman -S python-click python-pyqt6`
    *   **Debian/Ubuntu:** `apt install python3-click python3-pyqt6`

*   **Option B: Pip**
    ```bash
    pip install -r requirements.txt
    ```

### Install Driver Patch
You can install the modified driver temporarily (current session) or permanently (DKMS-style patch).

```bash
# Permanent Installation (Recommended)
sudo python3 omen_cli.py install-patch permanent

# Temporary Installation (Until Reboot)
sudo python3 omen_cli.py install-patch temporary
```

### Install Background Service
For proper curve control and watchdog operation, install the background service:

```bash
sudo python3 omen_cli.py service install
```

You can also install it from the settings page in the graphical interface.
## Usage
### GUI
A graphical interface is available for simpler configuration.
```bash
sudo python3 omen_gui.py
```
**GUI Fan Curve**

|<img width="400" height="300" alt="Omen Fan Control GUI" src="https://github.com/user-attachments/assets/57f1a966-d7a6-4c5f-8090-a1e947349bd9" />|
|---|


### CLI
The `omen_cli.py` script manages everything.

**Check Status:**
```bash
sudo python3 omen_cli.py status
```
**Possible Settings**
```bash
python omen_cli.py settings --help
```
**Current Settings Configuration**
```bash
python omen_cli.py settings
```

**Set Settings (Moving Average Window, etc.):**
```bash
sudo python3 omen_cli.py options --ma-window 10 --curve-interpolation smooth
```

**Curve Temperature Source (`--temp-source`):**

On Intel CPUs the `Package id 0` sensor is the *hottest* core, not an average. A single core
boosting for a fraction of a second (e.g. a file indexer) hits Tjmax immediately, and a moving
average of that reading keeps the fans spinning even though the CPU is barely loaded.
```bash
# package   - hottest core, the original behaviour (default)
# core_mean - average of all core sensors
# hybrid    - core_mean, plus the package reading once it has stayed hot for
#             --spike-window consecutive samples (2s each); short bursts are ignored
sudo python3 omen_cli.py options --temp-source hybrid --spike-window 15
```
The same setting is available in the GUI under *Options → Curve Temperature*.

**Manual Fan Control:**
```bash
# Set specific speed
sudo python3 omen_cli.py fan-control --mode manual --value 80%

# Set Curve Mode (requires service)
sudo python3 omen_cli.py fan-control --mode curve

# Set Auto (Default)
sudo python3 omen_cli.py fan-control --mode auto
```

**Using Custom Curves:**
```bash
sudo python3 omen_cli.py fan-control --curve-csv my_curve.csv --curve-name Quiet
```
Where the csv file has values in `temp, percent` order. Without `--curve-name` the CSV replaces the active curve.

**Curve library:** several named curves can be kept and switched at any time (the service
picks the change up within a couple of seconds). The GUI has the same controls above the curve editor.
```bash
sudo python3 omen_cli.py curves list            # * marks the active curve
sudo python3 omen_cli.py curves use Quiet
sudo python3 omen_cli.py curves rename Quiet Silent
sudo python3 omen_cli.py curves export Silent --csv silent.csv
sudo python3 omen_cli.py curves delete Silent
```
Note: *Auto* mode is not a curve of this program: it hands the fans back to the firmware's own
fan table (the one BIOS/Windows use), which cannot be read or edited.

<br>

**Fan Cleaning (reverse spin):**

The OMEN Gaming Hub "Fan cleaning" routine: the fans stop, spin *backwards* for a while to blow dust
out of the heatsinks, then are handed back to the firmware. Available from the GUI (*Fan Cleaning* page)
and the CLI:
```bash
sudo python3 omen_cli.py clean status                     # support, settings, last / next run
sudo python3 omen_cli.py clean run [--duration 30] [--speed 37]
sudo python3 omen_cli.py clean stop                       # abort a run in progress
sudo python3 omen_cli.py options --cleaner-auto on --cleaner-interval 1w   # periodic (service): 36h, 7d, 2w, 1m
sudo python3 omen_cli.py options --cleaner-window 8-22                     # only start between these hours ('any' to disable)
sudo python3 omen_cli.py options --cleaner-duration 30 --cleaner-speed 37  # speed in x100 RPM
```
- Needs the `acpi_call` kernel module (`sudo modprobe acpi_call`; Arch: `acpi_call-dkms`,
  Debian/Ubuntu: `acpi-call-dkms`) and the patched driver (for RPM readback).
- When the background service is running it performs the cleaning itself (GUI and CLI just ask it),
  so nothing else touches the fans meanwhile; otherwise the GUI/CLI run it directly.
- Safety: refused above 75 °C (core mean) and aborted if that is reached during the run, if the fans
  do not stop within 7 s, or if the EC does not actually reverse.
- Automatic runs (GUI: *every day / 3 days / week / 2 weeks / month / custom*, default weekly) only
  start inside the allowed hours (default 08:00–22:00, `22-6` wraps midnight), on AC power and with a
  cool CPU; otherwise they are retried 10 minutes later. The first run happens one interval after
  enabling; `clean status` and the GUI show the next scheduled time.
- Duration 10–90 s (the EC drops user fan control after 120 s). The default reverse speed is
  3700 RPM, the value OMEN Gaming Hub was observed to use; the firmware may cap higher requests.

<br>

**Detailed Information**

Commands provide detailed information when `--help` is passed with the command
```bash
python omen_cli.py fan-control --help
```

## Changes in this fork

Tested on an HP Omen Max 16 (board `8D41`, Intel Core Ultra 7 255HX) running Arch Linux.

- **Hybrid curve temperature** (`--temp-source`, GUI *Options → Curve Temperature*). On Intel the
  `Package id 0` sensor is the hottest core: a single core boosting for a fraction of a second
  (e.g. a file indexer) reaches Tjmax and a moving average of that reading kept the fans at 4000+ RPM
  on an almost idle machine. `hybrid` controls on the mean of all cores and only lets the package
  reading count once it has stayed hot for `--spike-window` samples. Same load: 600–900 RPM.
- The header temperature in the GUI is now the temperature the curve is actually applied to.
- **Named curve library**: several curves, switchable from the GUI (combo above the editor) or with
  `omen_cli.py curves list|use|rename|delete|export`; the service follows the change within 2 s.
- **Fan cleaning** (GUI *Fan Cleaning* page, `omen_cli.py clean run|stop|status`, `--cleaner-*`
  options): reverse-spin dust removal as in OMEN Gaming Hub, on demand or every N hours via the
  service. Same EC command the driver uses for manual speed (WMI `0x2E`) with the reverse bit set,
  sent through `acpi_call`. Braking, decelerated release and thermal/timeout aborts included.
- The curve step (temperature estimate + hysteresis) is shared between the daemon and the GUI's
  local loop instead of being implemented twice.
- Fixes: CLI options given without a value (`--bypass-warning`, `--curve-interpolation`, …) crashed
  with *'show' is not one of…*; `--enable-experimental` / `--thermal-profile` were ignored; the
  GUI showed the *Experimental Support* dialog on every start (`debug_experimental_ui` defaulted
  to `true`); the Mode combo and manual speed did not reflect the saved config; missing window icon;
  timers not stopped on close.
- `hp-wmi.c`: don't abort module load when another driver (e.g. `omen-rgb-keyboard`) already owns
  the WMI hotkey event handler; BIOS/fan/thermal features are independent of it.

## Uninstallation

To remove the service and restore the original kernel driver:

1.  **Remove Service:**
    ```bash
    sudo python3 omen_cli.py service remove
    ```

2.  **Restore Driver:**
    ```bash
    sudo python3 omen_cli.py install-patch restore
    ```
    This restores the original `.ko` files from the backups created during installation.

## Disclaimer

**USE AT YOUR OWN RISK.**
Modifying kernel drivers and manipulating thermal control systems can potentially damage your hardware or cause instability. This software is provided "as is" without warranty of any kind. This was tested on my personal hardware, and the used `hp-wmi.c` is a patched version of the one in the upcoming `6.20` kernel, so your mileage may vary.

<details>
<summary>Acknowledgements</summary>
<br>

**Probes:**
- https://github.com/alou-S/omen-fan/blob/main/docs/probes.md

**Fan cleaning WMI protocol (reverse bit, capability query):**
- https://github.com/arfelious/omen-fan-control (v2.0.0, `_cleaner.py`)

**Linux 6.20 Kernel HP-WMI Driver:**
- https://git.kernel.org/pub/scm/linux/kernel/git/pdx86/platform-drivers-x86.git/commit/?h=for-next&id=46be1453e6e61884b4840a768d1e8ffaf01a4c1c

</details>
