import os
# Omen Fan Control
# Control your HP Laptop's fans in Linux
# Copyright (C) 2026 arfelious
# Modified 2026-09-18 by Mirco Giorgi (https://github.com/mirco-g01):
#   hybrid temperature source, named curve library, GUI/CLI fixes
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import glob
import json
import re
import struct
import time
import math
import shutil
import subprocess
from pathlib import Path

# Constants
HWMON_PATH_PATTERN = "/sys/devices/platform/hp-wmi/hwmon/*/"
THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"
# Determine config path based on permissions
if os.geteuid() == 0:
    CONFIG_DIR = Path("/etc/omen-fan-control")
else:
    CONFIG_DIR = Path(os.path.expanduser("~/.config/omen-fan-control"))

CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_CALIBRATION_WAIT = 30
DEFAULT_WATCHDOG_INTERVAL = 90
# Which temperature the fan curve is applied to:
#   package   - hottest core (Intel "Package id 0"), upstream behaviour
#   core_mean - average of all core sensors
#   hybrid    - max(core_mean, package sustained for spike_window samples)
TEMP_SOURCES = ("package", "core_mean", "hybrid")
DEFAULT_TEMP_SOURCE = "package"
DEFAULT_SPIKE_WINDOW = 15
DEFAULT_CURVE_NAME = "Default"
# Curve hysteresis: don't rewrite pwm while the fan is within this many RPM
# of the target, but re-apply at least every HYSTERESIS_REAPPLY_SECS.
HYSTERESIS_RPM = 200
HYSTERESIS_REAPPLY_SECS = 60
# Fan cleaning (reverse spin, the OMEN Gaming Hub "Fan cleaning" routine).
# Speeds are in units of 100 RPM, like the WMI fan speed byte.
ACPI_CALL_PATH = Path("/proc/acpi/call")
CLEANER_STATE_FILE = CONFIG_DIR / "cleaner_state.json"
CLEANER_REQUEST_FILE = CONFIG_DIR / "cleaner.request"
CLEANER_STOP_FILE = CONFIG_DIR / "cleaner.stop"
DEFAULT_CLEANER_DURATION = 30
CLEANER_DURATION_RANGE = (10, 90)      # the EC drops user fan control after 120s
DEFAULT_CLEANER_SPEED = 37
CLEANER_SPEED_RANGE = (10, 60)
DEFAULT_CLEANER_INTERVAL_HOURS = 168           # weekly
CLEANER_INTERVAL_PRESETS = (("day", 24), ("3 days", 72), ("week", 168), ("2 weeks", 336), ("month", 720))
# Automatic runs only start between these hours (local time); equal = any time
DEFAULT_CLEANER_WINDOW = (8, 22)
CLEANER_MAX_TEMP = 75                  # °C, refuse/abort above this
CLEANER_SOFT_START_PWM = 35
CLEANER_AUTO_RETRY_SECS = 600          # auto clean postponed (hot / on battery): retry after
FAN_REVERSE_FLAG = 0x80
OMEN_FAN_DIR = Path(__file__).parent.absolute()
CONFIG_VERSION = 1

# Supported Board IDs
SUPPORTED_BOARDS = {
    "84DA", "84DB", "84DC",
    "8572", "8573", "8574", "8575",
    "8600", "8601", "8602", "8603", "8604", "8605", "8606", "8607", "860A",
    "8746", "8747", "8748", "8749", "874A", "8786", "8787", "8788", "878A",
    "878B", "878C", "87B5",
    "886B", "886C", "88C8", "88CB", "88D1", "88D2", "88F4", "88F5", "88F6",
    "88F7", "88FD", "88FE", "88FF",
    "8900", "8901", "8902", "8912", "8917", "8918", "8949", "894A", "89EB",
    "8A15", "8A42", "8BAD", "8E41",
    
    "88F8", "8A25",
    "8BAB", "8BBE", "8BD4", "8BD5", "8C78", "8BCD", "8C99", "8C9C", "8D41"
}

POSSIBLY_SUPPPORTED_OMEN_BOARDS = {
    "84DA", "84DB", "84DC", "8574", "8575", "860A", "87B5", "8572", "8573",
 	"8600", "8601", "8602", "8605", "8606", "8607", "8746", "8747", "8749",
 	"874A", "8603", "8604", "8748", "886B", "886C", "878A", "878B", "878C",
 	"88C8", "88CB", "8786", "8787", "8788", "88D1", "88D2", "88F4", "88FD",
	"88F5", "88F6", "8A13", "8A14", "8A15", "8A16", "88F7", "88FE", "8A17",
	"8A18", "8A19", "8A1A", "8BAD", "8BB0", "88FF", "8900", "8901", "8902",
	"8912", "8917", "8918", "8A97", "8A96", "8D2C", "8949", "8A98", "894A",
	"8B1D", "89EB", "8A4C", "8A4D", "8A4E", "8A40", "8A41", "8A42", "8A43",
	"8A44", "8BA8", "8BA9", "8BAA", "8BAC", "8C76", "8C77", "8C78",
	"8BCA", "8BCB", "8BCF", "8C9B", "8BB3", "8BB4", "8C4D", "8C4E",
	"8C58", "8C75", "8C74", "8C73", "8CC1", "8CC0", "8CF1", "8CF2", "8CF3",
	"8CF4"
}

class FanController:
    def __init__(self, config_path=None):
        self._find_paths()
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = CONFIG_FILE
            
        self.config = self.load_config()

    def check_board_support(self):
        """
        Checks if the current board is in the supported list.
        Returns (status, board_name)
        status: "SUPPORTED", "POSSIBLY_SUPPORTED", "UNSUPPORTED"
        """
        # Return cached if available
        if self.config.get("cached_board_name"):
            board_name = self.config["cached_board_name"]
        else:
            try:
                with open("/sys/class/dmi/id/board_name", "r") as f:
                    board_name = f.read().strip()
                
                self.config["cached_board_name"] = board_name
                self.save_config()
                
            except Exception as e:
                print(f"Error reading board name: {e}")
                return "UNSUPPORTED", "Unknown"

        if board_name in SUPPORTED_BOARDS:
            return "SUPPORTED", board_name
        elif board_name in POSSIBLY_SUPPPORTED_OMEN_BOARDS:
            return "POSSIBLY_SUPPORTED", board_name
        else:
            return "UNSUPPORTED", board_name

    def _find_paths(self):
        """Finds the correct hwmon paths for fan control."""
        # Find CPU temp path independent of HP WMI
        self.cpu_temp_path = self._find_cpu_temp_path()
        
        paths = glob.glob(HWMON_PATH_PATTERN)
        if not paths:
            self.hwmon_path = None
            self.pwm1_enable_path = None
            self.pwm1_path = None
            self.fan1_input_path = None
            self.fan2_input_path = None
            return

        self.hwmon_path = Path(paths[0])
        self.pwm1_enable_path = self.hwmon_path / "pwm1_enable"
        self.pwm1_path = self.hwmon_path / "pwm1"
        self.fan1_input_path = self.hwmon_path / "fan1_input"
        self.fan2_input_path = self.hwmon_path / "fan2_input"
        self.cpu_temp_path = self._find_cpu_temp_path()

    def _find_cpu_temp_path(self):
        """Finds the CPU temperature input file."""
        for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
            try:
                name_path = hwmon / "name"
                if not name_path.exists():
                    continue
                with open(name_path, "r") as f:
                    name = f.read().strip()
                
                if name in ["coretemp", "k10temp"]:
                    temp_path = hwmon / "temp1_input"
                    if temp_path.exists():
                        return temp_path
            except Exception:
                continue
        
        # Fallback to thermal_zone0
        if Path("/sys/class/thermal/thermal_zone0/temp").exists():
             return Path("/sys/class/thermal/thermal_zone0/temp")
             
        return None


    def load_config(self):
        """Loads configuration from JSON file."""
        defaults = {
            "version": CONFIG_VERSION,
            "fan_max": 0,
            "calibration_wait": DEFAULT_CALIBRATION_WAIT,
            "watchdog_interval": DEFAULT_WATCHDOG_INTERVAL,
            "ma_window": 5,
            "temp_source": DEFAULT_TEMP_SOURCE,
            "spike_window": DEFAULT_SPIKE_WINDOW,
            "curve": [],
            "curves": {},
            "active_curve": DEFAULT_CURVE_NAME,
            "bypass_warning": False,
            "mode": "auto",
            "manual_pwm": 0,
            "curve_interpolation": "smooth",
            "bypass_root_warning": False,
            "enable_experimental": False,
            "thermal_profile": "omen",
            "cached_board_name": None,
            "debug_experimental_ui": False,
            "cleaner_auto": False,
            "cleaner_interval_hours": DEFAULT_CLEANER_INTERVAL_HOURS,
            "cleaner_window_start": DEFAULT_CLEANER_WINDOW[0],
            "cleaner_window_end": DEFAULT_CLEANER_WINDOW[1],
            "cleaner_duration": DEFAULT_CLEANER_DURATION,
            "cleaner_speed": DEFAULT_CLEANER_SPEED,
        }
        
        if not self.config_path.exists():
            return defaults
            
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
            
            config = defaults.copy()
            config.update(data)
            self._migrate_curves(config)
            return config
            
        except Exception as e:
            print(f"Error loading config: {e}")
            return defaults

    @staticmethod
    def _migrate_curves(config):
        """Older configs have a single "curve"; keep it as the "Default" entry of
        "curves" and make sure "active_curve" points at something that exists."""
        curves = config.get("curves")
        if not isinstance(curves, dict):
            curves = {}
        if not curves and config.get("curve"):
            curves[DEFAULT_CURVE_NAME] = config["curve"]
        config["curves"] = curves
        if curves and config.get("active_curve") not in curves:
            config["active_curve"] = next(iter(curves))

    # Curve library
    def get_curves(self):
        return self.config.get("curves", {})

    def get_active_curve_name(self):
        return self.config.get("active_curve", DEFAULT_CURVE_NAME)

    def get_active_curve(self):
        """Points [[temp, percent], ...] of the active curve (legacy "curve" as fallback)."""
        curves = self.get_curves()
        name = self.get_active_curve_name()
        if name in curves:
            return curves[name]
        return self.config.get("curve", [])

    def set_active_curve(self, name):
        if name not in self.get_curves():
            raise KeyError(f"No curve named '{name}'")
        self.config["active_curve"] = name

    def save_curve(self, name, points, activate=False):
        name = name.strip()
        if not name:
            raise ValueError("Curve name cannot be empty")
        self.config.setdefault("curves", {})[name] = [[float(t), float(p)] for t, p in points]
        if activate or len(self.config["curves"]) == 1:
            self.config["active_curve"] = name

    def rename_curve(self, old, new):
        new = new.strip()
        curves = self.get_curves()
        if old not in curves:
            raise KeyError(f"No curve named '{old}'")
        if not new:
            raise ValueError("Curve name cannot be empty")
        if new != old and new in curves:
            raise ValueError(f"A curve named '{new}' already exists")
        # rebuild to keep the ordering
        self.config["curves"] = {(new if k == old else k): v for k, v in curves.items()}
        if self.get_active_curve_name() == old:
            self.config["active_curve"] = new

    def delete_curve(self, name):
        curves = self.get_curves()
        if name not in curves:
            raise KeyError(f"No curve named '{name}'")
        if len(curves) == 1:
            raise ValueError("Cannot delete the only curve")
        del curves[name]
        if self.get_active_curve_name() == name:
            self.config["active_curve"] = next(iter(curves))

    def save_config(self):
        """Saves current configuration to JSON file."""
        if self.config_path.parent:
             self.config_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.config["version"] = CONFIG_VERSION
        # keep the legacy single "curve" key in sync with the active curve
        if self.get_curves():
            self.config["curve"] = self.get_active_curve()
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=4)

    def write_sys_file(self, path, value):
        """Helper to write to sysfs files."""
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(str(value))
        except PermissionError:
            print(f"Permission denied writing to {path}. Are you running as root?")
        except Exception as e:
            print(f"Error writing to {path}: {e}")

    def read_sys_file(self, path):
        """Helper to read from sysfs files."""
        if not path or not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return None

    @staticmethod
    def parse_fan_rpm(val):
        """fanN_input is the raw EC speed byte * 100; bit 7 of that byte means
        the fan is spinning backwards (fan cleaning). Returns (rpm, reverse)."""
        try:
            raw = int(val)
        except (TypeError, ValueError):
            return 0, False
        if raw >= FAN_REVERSE_FLAG * 100:
            return ((raw // 100) & (FAN_REVERSE_FLAG - 1)) * 100, True
        return raw, False

    def get_fan_state(self, fan=1):
        """Returns (rpm, reverse) of fan 1 or 2."""
        path = self.fan1_input_path if fan == 1 else self.fan2_input_path
        return self.parse_fan_rpm(self.read_sys_file(path))

    def get_fan_speed(self):
        """Returns current fan speed in RPM (direction stripped)."""
        val = self.read_sys_file(self.fan1_input_path)
        if not val:
            print("Failed to read fan speed")
            return 0
        return self.parse_fan_rpm(val)[0]

    def is_on_ac_power(self):
        """True if a mains adapter reports online (or nothing can be read)."""
        found = False
        for sup in Path("/sys/class/power_supply").glob("*"):
            try:
                if (sup / "type").read_text().strip() != "Mains":
                    continue
                found = True
                if (sup / "online").read_text().strip() == "1":
                    return True
            except Exception:
                continue
        return not found

    def restore_configured_mode(self):
        """Re-applies the mode saved in the config once (used after a fan
        cleaning run when no service is there to do it)."""
        mode = self.config.get("mode", "auto")
        if mode == "curve":
            ctl = self.make_curve_controller()
            ctl.request_reapply()
            ctl.step()
        elif mode == "manual":
            self.set_fan_pwm(self.config.get("manual_pwm", 0))
        elif mode == "max":
            self.set_fan_mode("max")
        else:
            self.set_fan_mode("auto")

    def get_cpu_temp(self):
        """Returns CPU temp in Celsius."""
        if self.cpu_temp_path:
            val = self.read_sys_file(self.cpu_temp_path)
            return int(val) // 1000 if val else 0
        return 0


    def get_core_mean_temp(self):
        """Returns the average of all per-core sensors in Celsius.
        Falls back to the package temp if no core sensors are found."""
        cores = [t for label, t in self.get_all_core_temps() if "Core" in label]
        if not cores:
            return self.get_cpu_temp()
        return sum(cores) / len(cores)

    def get_all_core_temps(self):
        """Returns a list of tuples [(label, temp), ...] sorted by core index."""
        core_temps = []
        package_temps = []
        
        if not self.cpu_temp_path:
             return []
             
        hwmon_dir = self.cpu_temp_path.parent
        
        for f in hwmon_dir.glob("temp*_input"):
            try:
                label_file = f.with_name(f.name.replace("input", "label"))
                if label_file.exists():
                    label = self.read_sys_file(label_file)
                else:
                    label = f.name
                
                val = self.read_sys_file(f)
                if not val: continue
                temp = int(val) // 1000
                
                if "Core" in label:
                    try:
                        idx = int(label.split()[-1])
                        core_temps.append((idx, label, temp))
                    except:
                        core_temps.append((999, label, temp))
                elif "Package" in label:
                    package_temps.append((label, temp))
            except:
                continue
        
        core_temps.sort(key=lambda x: x[0])
        
        params = []
        for p in package_temps:
            params.append(p)
            
        for c in core_temps:
            params.append((c[1], c[2]))
            
        return params

    def set_fan_mode(self, mode):
        """Sets fan mode: 'max', 'auto', or 'manual'."""
        if mode == 'max':
            self.write_sys_file(self.pwm1_enable_path, 0)
        elif mode == 'auto':
            self.write_sys_file(self.pwm1_enable_path, 2)

    def set_fan_pwm(self, value):
        """Sets fan speed (0-255). Ensures manual mode (pwm1_enable=1)."""
        # Ensure we are in manual mode
        current_enable = self.read_sys_file(self.pwm1_enable_path)
        if current_enable != "1":
            self.write_sys_file(self.pwm1_enable_path, 1)
            
        # value should be 0-255
        self.write_sys_file(self.pwm1_path, str(int(value)))

    def calculate_target_pwm(self, current_temp):
        """Calculates target PWM (0-255) based on curve and temperature."""
        curve = self.get_active_curve()
        if not curve: 
            return None
        
        curve = sorted(curve, key=lambda p: p[0])
        
        target_speed_percent = 0
        
        if current_temp <= curve[0][0]:
            target_speed_percent = curve[0][1]
        elif current_temp >= curve[-1][0]:
            target_speed_percent = curve[-1][1]
        else:
            for i in range(len(curve) - 1):
                p1 = curve[i]
                p2 = curve[i+1]
                if p1[0] <= current_temp <= p2[0]:
                    interp_mode = self.config.get("curve_interpolation", "smooth")
                    
                    if interp_mode == "discrete":
                        target_speed_percent = p1[1]
                    else:
                        denom = p2[0] - p1[0]
                        if denom == 0:
                            target_speed_percent = p2[1]
                        else:
                            ratio = (current_temp - p1[0]) / denom
                            target_speed_percent = p1[1] + ratio * (p2[1] - p1[1])
                    break
        
        return int(round(target_speed_percent / 100 * 255))

    def make_curve_controller(self):
        return CurveController(self)

    def make_fan_cleaner(self):
        return FanCleaner(self)

    def calibrate(self):
        """Runs calibration routine. Yields progress (0-100), returns max RPM."""
        print("Starting calibration...")
        
        try:
            prev_enable = self.read_sys_file(self.pwm1_enable_path) or "2"
            prev_pwm = self.read_sys_file(self.pwm1_path) or "0"
        except:
            prev_enable = "2"
            prev_pwm = "0"
            
        self.set_fan_mode('max')
        
        wait_time = self.config.get("calibration_wait", DEFAULT_CALIBRATION_WAIT)
        steps = 10
        for i in range(steps):
             time.sleep(wait_time / steps)
             yield int((i + 1) / steps * 100)
        
        max_rpm = self.get_fan_speed()
        self.config["fan_max"] = max_rpm
        self.save_config()
        
        try:
            if prev_enable:
                self.write_sys_file(self.pwm1_enable_path, prev_enable)
            if prev_pwm and str(prev_enable).strip() == "1":
                self.write_sys_file(self.pwm1_path, prev_pwm)
        except Exception as e:
            print(f"Error restoring fan state: {e}")
            
        return max_rpm

    def _patch_driver_source(self, fan_max):
        """Patches hp-wmi.c with the max rpm value and experimental boards if enabled."""
        orig_file = OMEN_FAN_DIR / "hp-wmi.c.orig"
        target_file = OMEN_FAN_DIR / "hp-wmi.c"
        
        if not orig_file.exists():
            if target_file.exists():
                shutil.copy(target_file, orig_file)
            else:
                return False, "Error: hp-wmi.c not found."

        # Read orig content
        with open(orig_file, "r") as f:
            content = f.read()

        # 1. Patch Max RPM
        max_rpm_val = math.floor(fan_max / 100)
        new_define = f"#define OMEN_MAX_RPM {max_rpm_val}"
        content = content.replace("#define OMEN_MAX_RPM 60", new_define)
        
        # 2. Patch Experimental Support if enabled
        if self.config.get("enable_experimental", False):
            board_name = self.config.get("cached_board_name")
            if not board_name:
                 # Try to get it if not cached
                 _, board_name = self.check_board_support()
            
            if board_name and board_name != "Unknown":
                profile = self.config.get("thermal_profile", "omen")
                
                target_array = "omen_thermal_profile_boards"
                if profile == "victus":
                    target_array = "victus_thermal_profile_boards"
                elif profile == "victus_s":
                    target_array = "victus_s_thermal_profile_boards"
                           
                start_idx = content.find(f"{target_array}[]")
                if start_idx != -1:
                    # Find closing brace after start_idx
                    end_idx = content.find("};", start_idx)
                    if end_idx != -1:
                         # Check if board is already in there
                         segment = content[start_idx:end_idx]
                         if f'"{board_name}"' not in segment:
                             if target_array == "victus_s_thermal_profile_boards":
                                 insertion = f'        {{\n            .matches = {{DMI_MATCH(DMI_BOARD_NAME, "{board_name}")}},\n            .driver_data = (void *)&victus_s_thermal_params,\n        }},\n'
                             else:
                                 insertion = f'    "{board_name}",\n'
                             content = content[:end_idx] + insertion + content[end_idx:]
                         else:
                             print(f"Board {board_name} already in {target_array} in orig file? Skipping append.")
                else:
                    print(f"Warning: Could not find array {target_array} in hp-wmi.c")

        with open(target_file, "w") as f:
            f.write(content)
            
        return True, "Patch applied successfully."

    @staticmethod
    def _format_make_error(stderr: str) -> str:
        """Parse make/build stderr and return a user-friendly message.

        Detects common kernel-module build failures (e.g. missing
        generated/autoconf.h on Debian) and appends actionable fix
        instructions so the user doesn't have to search the web.
        """
        hint = ""
        if "generated/autoconf.h" in stderr:
            hint = (
                "\n\n--- Likely cause ---\n"
                "Your kernel headers are incomplete (generated/autoconf.h is missing).\n"
                "This is a known issue on Debian/Ubuntu where headers are split into two packages.\n\n"
                "Fix:\n"
                "  sudo apt reinstall linux-headers-$(uname -r)\n\n"
                "Diagnostic (check if autoconf.h is present):\n"
                "  ls /usr/src/linux-headers-$(uname -r)/include/generated/"
            )
        elif "No such file or directory" in stderr and "scripts/basic/Makefile" in stderr:
            hint = (
                "\n\n--- Likely cause ---\n"
                "Kernel build scripts (kbuild) are missing.\n"
                "This is a common issue on Debian/Ubuntu where headers are split across packages.\n\n"
                "Fix:\n"
                "  Debian/Ubuntu: sudo apt install \"linux-kbuild-$(uname -r | cut -d. -f1,2,3 | cut -d+ -f1)*\"\n"
            )
        elif "No such file or directory" in stderr and "/lib/modules/" in stderr:
            hint = (
                "\n\n--- Likely cause ---\n"
                "Kernel headers not found for the running kernel.\n\n"
                "Fix:\n"
                "  Debian/Ubuntu : sudo apt install linux-headers-$(uname -r)\n"
                "  Fedora/RHEL   : sudo dnf install kernel-devel-$(uname -r)\n"
                "  Arch Linux    : sudo pacman -S linux-headers"
            )
        return f"Make failed: {stderr}{hint}"

    def install_driver_temp(self, force=False):
        """Installs driver temporarily using insmod. Requires calibration first."""
        if self.pwm1_path and self.pwm1_path.exists():
            bypass = self.config.get("bypass_patch_warning", False)
            if not force and not bypass:
                return False, "PWM_DETECTED"

        fan_max = self.config.get("fan_max", 0)
        if fan_max == 0:
            return False, "Error: Please calibrate first to get Max RPM."

        success, msg = self._patch_driver_source(fan_max)
        if not success:
             return False, msg

        try:
            subprocess.run(["make"], check=True, cwd=OMEN_FAN_DIR, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return False, self._format_make_error(e.stderr)
        
        ko_files = list(OMEN_FAN_DIR.glob("*.ko"))
        if not ko_files:
            return False, "Error: No .ko file found after make."
        
        subprocess.run(["modprobe", "-r", "hp-wmi"], check=False)
        
        try:
            subprocess.run(["modprobe", "sparse_keymap"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return False, f"Modprobe sparse_keymap failed: {e.stderr}"
        
        try:
            subprocess.run(["insmod", str(ko_files[0])], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
             subprocess.run(["modprobe", "hp-wmi"], check=False)
             return False, f"Insmod failed: {e.stderr}\n(Original driver re-loaded attempts)"
        
        self.config["install_type"] = "temporary"
        self.save_config()
             
        return True, "Temporary driver installed successfully."

    def install_driver_perm(self, force=False):
        """Installs driver permanently by patching and running install script."""
        if self.pwm1_path and self.pwm1_path.exists():
            bypass = self.config.get("bypass_patch_warning", False)
            if not force and not bypass:
                return False, "PWM_DETECTED"

        fan_max = self.config.get("fan_max", 0)
        if fan_max == 0:
            return False, "Error: Please calibrate first to get Max RPM."

        success, msg = self._patch_driver_source(fan_max)
        if not success:
            return False, msg

        try:
            subprocess.run(["/bin/bash", "install_driver.sh"], cwd=OMEN_FAN_DIR, check=True)
        except subprocess.CalledProcessError:
            return False, "Install script failed. Check terminal output above for details."
            
        self.config["install_type"] = "permanent"
        self.save_config()
            
        return True, "Permanent driver installed successfully."

    def check_install_type(self):
        """Determines installation type: 'permanent', 'temporary', or None."""
        if not (self.pwm1_path and self.pwm1_path.exists()):
            return None
        
        conf_type = self.config.get("install_type")
        if conf_type in ["permanent", "temporary"]:
            return conf_type
        
        try:
            kernel_ver = subprocess.check_output(["uname", "-r"]).decode().strip()
            hp_driver_dir = Path(f"/lib/modules/{kernel_ver}/kernel/drivers/platform/x86/hp")
            
            if hp_driver_dir.exists():
                if list(hp_driver_dir.glob("*.bak")):
                    return "permanent"
        except Exception:
            pass
        
        return "temporary"

    def start_stress_test(self, duration_sec, core_count=None):
        """Starts a CPU stress test. Duration handled by caller."""
        import os
        import sys
        
        if core_count is None:
            core_count = os.cpu_count() or 4
            
        self.stop_stress_test()
        
        self.stress_processes = []
        cmd = [sys.executable, "-c", "while True: 9999**9999"]
        
        print(f"Starting stress test on {core_count} cores...")
        try:
            for _ in range(core_count):
                p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.stress_processes.append(p)
            return True
        except Exception as e:
            print(f"Error starting stress test: {e}")
            self.stop_stress_test()
            return False

    def stop_stress_test(self):
        """Stops the running stress test."""
        if hasattr(self, 'stress_processes') and self.stress_processes:
            for p in self.stress_processes:
                try:
                    p.terminate()
                except Exception:
                    pass
            
            for p in self.stress_processes:
                try:
                    p.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    p.kill()
            
            self.stress_processes = []
            print("Stopped stress test.")
            
    def set_bios_control(self, enabled):
        """Enables or disables BIOS fan control by writing to EC registers."""
        try:
             subprocess.run(["modprobe", "ec_sys", "write_support=1"], check=True)
        except Exception as e:
            print(f"Failed to load ec_sys: {e}")
            return False

        ECIO_FILE = "/sys/kernel/debug/ec/ec0/io"
        BIOS_OFFSET = 98
        TIMER_OFFSET = 99
        FAN1_OFFSET = 52
        FAN2_OFFSET = 53
        
        try:
            with open(ECIO_FILE, "r+b") as ec:
                if not enabled:
                    ec.seek(BIOS_OFFSET)
                    ec.write(bytes([6]))
                    time.sleep(0.1)
                    ec.seek(TIMER_OFFSET)
                    ec.write(bytes([0]))
                else:
                    ec.seek(BIOS_OFFSET)
                    ec.write(bytes([0]))
                    ec.seek(FAN1_OFFSET)
                    ec.write(bytes([0]))
                    ec.seek(FAN2_OFFSET)
                    ec.write(bytes([0]))
            return True
        except Exception as e:
            print(f"Error setting BIOS control: {e}")
            return False

    # Service Management
    def create_service(self):
        """
        Creates and enables a systemd service to run 'omen_cli.py serve'.
        """
        import sys
        service_content = f"""[Unit]
Description=HP Omen Fan Control Service
After=multi-user.target

[Service]
Type=simple
ExecStart={sys.executable} {str(OMEN_FAN_DIR / 'omen_cli.py')} serve
WorkingDirectory={str(OMEN_FAN_DIR)}
Restart=on-failure
StartLimitBurst=5
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""
        service_path = Path("/etc/systemd/system/omen-fan-control.service")
        
        try:
            with open("omen-fan-control.service", "w") as f:
                f.write(service_content)
                
            subprocess.run(["mv", "omen-fan-control.service", str(service_path)], check=True)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "omen-fan-control.service"], check=True)
            subprocess.run(["systemctl", "start", "omen-fan-control.service"], check=True)
            return True, "Service created and started."
        except Exception as e:
            return False, f"Failed to create service: {e}"

    def remove_service(self):
        """Stops and removes the systemd service."""
        try:
            subprocess.run(["systemctl", "stop", "omen-fan-control.service"], check=False)
            subprocess.run(["systemctl", "disable", "omen-fan-control.service"], check=False)
            
            service_path = Path("/etc/systemd/system/omen-fan-control.service")
            if service_path.exists():
                subprocess.run(["rm", str(service_path)], check=True)
                
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            return True, "Service removed."
        except Exception as e:
            return False, f"Failed to remove service: {e}"

    def restart_service(self):
        """Restarts the systemd service."""
        try:
            subprocess.run(["systemctl", "restart", "omen-fan-control.service"], check=True)
            return True, "Service restarted."
        except Exception as e:
            return False, f"Failed to restart service: {e}"

    def is_service_installed(self):
        """Checks if service file exists."""
        return Path("/etc/systemd/system/omen-fan-control.service").exists()

    def is_service_running(self):
        """Checks if service is actively running."""
        try:
            # Check active state
            res = subprocess.run(["systemctl", "is-active", "omen-fan-control.service"], capture_output=True, text=True)
            return res.stdout.strip() == "active"
        except Exception:
            return False

    def restore_driver(self):
        """Restores the original driver from backup files and removes DKMS/hooks."""
        messages = []
        
        try:
            # 1. Remove DKMS module if installed
            dkms_name = "hp-wmi-omen"
            dkms_version = "1.0"
            try:
                result = subprocess.run(["dkms", "status"], capture_output=True, text=True)
                if dkms_name in result.stdout:
                    subprocess.run(["dkms", "remove", f"{dkms_name}/{dkms_version}", "--all"], check=False)
                    messages.append("Removed DKMS module.")
            except FileNotFoundError:
                pass  # DKMS not installed
            
            # 2. Remove DKMS source directory
            dkms_src = Path(f"/usr/src/{dkms_name}-{dkms_version}")
            if dkms_src.exists() and dkms_name in str(dkms_src) and len(str(dkms_src)) > 10:
                subprocess.run(["rm", "-rf", str(dkms_src)], check=False)
            
            # 3. Remove our kernel hooks source
            hook_src = Path(f"/usr/src/{dkms_name}")
            if hook_src.exists() and dkms_name in str(hook_src) and len(str(hook_src)) > 10:
                subprocess.run(["rm", "-rf", str(hook_src)], check=False)
            
            # 4. Remove distro-specific kernel hooks
            hook_paths = [
                "/etc/pacman.d/hooks/90-hp-wmi-omen.hook",  # Arch
                "/etc/kernel/postinst.d/zz-hp-wmi-omen",   # Debian/Ubuntu
                "/etc/kernel/install.d/99-hp-wmi-omen.install",  # Fedora
            ]
            for hook in hook_paths:
                if Path(hook).exists():
                    subprocess.run(["rm", hook], check=False)
                    messages.append(f"Removed hook: {Path(hook).name}")
            
            # 5. Restore backup files
            kernel_ver = subprocess.check_output(["uname", "-r"]).decode().strip()
            search_paths = [
                Path(f"/lib/modules/{kernel_ver}/kernel/drivers/platform/x86/hp"),
                Path(f"/lib/modules/{kernel_ver}/updates")
            ]
            
            restored_count = 0
            
            for search_dir in search_paths:
                if search_dir.exists():
                    for bak_file in search_dir.rglob("*.bak"): # Recursive search for updates dir
                        target = bak_file.parent / bak_file.stem
                        subprocess.run(["mv", str(bak_file), str(target)], check=True)
                        restored_count += 1
            
            if restored_count == 0 and not messages:
                if self.config.get("install_type") == "temporary":
                     subprocess.run(["modprobe", "-r", "hp-wmi"], check=False)
                     subprocess.run(["modprobe", "hp-wmi"], check=False)
                     self.config.pop("install_type", None)
                     self.save_config()
                     return True, "Temporary driver unloaded. (No backups needed)"
                
                return False, "No backup files (.bak) found to restore."

            subprocess.run(["depmod", "-a"], check=True)
            subprocess.run(["modprobe", "-r", "hp-wmi"], check=False) 
            subprocess.run(["modprobe", "hp-wmi"], check=True)
            
            self.config.pop("install_type", None)
            self.save_config()
            
            if restored_count > 0:
                messages.append(f"Restored {restored_count} driver backup(s).")
            messages.append("Driver reloaded.")
            
            return True, " ".join(messages)
            
        except subprocess.CalledProcessError as e:
            return False, f"Error restoring driver: {e}"
        except Exception as e:
            return False, f"Error: {e}"



class TempEstimator:
    """
    Turns raw sensor readings into the temperature the fan curve is applied to.

    On Intel the "Package id 0" sensor is the hottest core, not an average: a
    single core boosting for a fraction of a second reaches Tjmax while the
    heatsink is still cold, and a moving average only smears that spike over
    the whole window. The "hybrid" source therefore controls on the mean of
    all cores (what actually has to be dissipated) and only lets the package
    reading count once it has stayed high for spike_window consecutive
    samples (a real sustained single-core load).
    """
    def __init__(self, controller):
        self.controller = controller
        self.raw_history = []
        self.pkg_history = []

    def reset(self):
        self.raw_history = []
        self.pkg_history = []

    def update(self):
        """Samples the sensors once and returns a dict with:
        package, core_mean, smoothed, sustained, control, source."""
        cfg = self.controller.config
        source = cfg.get("temp_source", DEFAULT_TEMP_SOURCE)
        if source not in TEMP_SOURCES:
            source = DEFAULT_TEMP_SOURCE
        ma_window = max(1, int(cfg.get("ma_window", 5)))
        spike_window = max(1, int(cfg.get("spike_window", DEFAULT_SPIKE_WINDOW)))

        package = self.controller.get_cpu_temp()
        core_mean = self.controller.get_core_mean_temp() if source != "package" else package
        raw = package if source == "package" else core_mean

        self.raw_history.append(raw)
        del self.raw_history[:-ma_window]
        smoothed = sum(self.raw_history) / len(self.raw_history)

        self.pkg_history.append(package)
        del self.pkg_history[:-spike_window]

        sustained = None
        control = smoothed
        if source == "hybrid":
            # min over the window: only counts if the package stayed hot for
            # the whole window, so short bursts are rejected entirely.
            if len(self.pkg_history) >= spike_window:
                sustained = min(self.pkg_history)
                control = max(smoothed, sustained)

        return {
            "package": package,
            "core_mean": core_mean,
            "smoothed": smoothed,
            "sustained": sustained,
            "control": control,
            "source": source,
        }


class CurveController:
    """One step of curve mode: estimate temperature, apply curve, write pwm
    with hysteresis. Shared by the daemon and the GUI's local loop."""
    def __init__(self, controller):
        self.controller = controller
        self.estimator = TempEstimator(controller)
        self.hysteresis_start_time = None
        self.force_apply = False

    def reset(self):
        self.estimator.reset()
        self.hysteresis_start_time = None

    def request_reapply(self):
        """Write pwm on the next step even if within the hysteresis band
        (the fan state was changed behind our back, e.g. by fan cleaning)."""
        self.force_apply = True

    def step(self):
        """Returns (estimate_dict, target_pwm, applied)."""
        est = self.estimator.update()
        target_pwm = self.controller.calculate_target_pwm(est["control"])
        if target_pwm is None:
            return est, None, False

        should_apply = True
        max_rpm = self.controller.config.get("fan_max", 0)
        if max_rpm > 0:
            current_rpm = self.controller.get_fan_speed()
            target_rpm = (target_pwm / 255) * max_rpm
            if abs(target_rpm - current_rpm) <= HYSTERESIS_RPM:
                if self.hysteresis_start_time is None:
                    self.hysteresis_start_time = time.time()
                should_apply = time.time() - self.hysteresis_start_time > HYSTERESIS_REAPPLY_SECS
            else:
                self.hysteresis_start_time = None

        if self.force_apply:
            should_apply = True
            self.force_apply = False

        if should_apply:
            self.controller.set_fan_pwm(target_pwm)
            self.hysteresis_start_time = None
        return est, target_pwm, should_apply


class FanCleaner:
    """
    Reverse-spin fan cleaning, the routine OMEN Gaming Hub runs on Windows to
    blow dust out of the heatsinks.

    The EC takes the same WMI command the hp-wmi driver uses for a manual fan
    speed (GM command 0x2E: one byte per fan, in units of 100 RPM); setting
    bit 7 of a byte spins that fan backwards. The driver does not expose that
    bit, so the command is sent through the acpi_call module instead. A run
    is: hand the fans to the EC, brake (reverse bit, speed 0) until they stop,
    spin backwards for `duration` seconds, decelerate, release the override
    and soft-start forward. Older boards use a flag in a 4 byte EC record
    ("legacy", untested here).

    One run at a time: the state file tells other processes (GUI, CLI) what
    the daemon is doing, the request file asks the daemon to run, the stop
    file aborts whichever process is running.
    """
    WMI_GM = 0x20008
    FAN_STATE_QUERY = 0x2C     # GM read: [cpu, gpu, fan3, ...,  caps at byte 8]
    FAN_SPEED_SET = 0x2E       # GM write: [cpu, gpu, fan3]
    FAN_COUNT_QUERY = 0x10     # GM read, keeps the EC in user-defined fan state
    LEGACY_QUERY = 0x2C        # READ/WRITE 4 byte record, bit 0x20 of byte 0 = supported
    BRAKE_TIMEOUT = 7.0
    STOPPED_RPM = 300

    def __init__(self, controller):
        self.controller = controller
        self._caps = None

    # --- acpi_call plumbing -------------------------------------------------
    @staticmethod
    def acpi_call_available():
        return ACPI_CALL_PATH.exists()

    def _wmi(self, command, ctype, data, outsize, datasize=None):
        """Runs \\_SB.WMID.WMAA with an HP bios_args buffer (same layout the
        driver builds in hp_wmi_perform_query: the data area is always at
        least 128 bytes). Returns the bytes after the 8 byte (signature,
        return code) header; raises on any WMI error."""
        if not self.acpi_call_available():
            raise RuntimeError("acpi_call module not loaded (/proc/acpi/call missing)")
        # method id encodes the expected output size, see hp-wmi.c
        method = 1 if outsize == 0 else 2 if outsize <= 4 else 3
        if datasize is None:
            datasize = max(len(data), outsize)
        buf = struct.pack("<4sIII", b"SECU", command, ctype, datasize) + bytes(data).ljust(max(datasize, 128), b"\0")
        with open(ACPI_CALL_PATH, "w") as f:
            f.write(f"\\_SB.WMID.WMAA 0 {method} b{buf.hex()}")
        with open(ACPI_CALL_PATH) as f:
            resp = f.read().strip().replace("\x00", "")
        if not resp or resp.startswith("Error"):
            raise RuntimeError(f"acpi_call failed: {resp or 'empty response'}")
        tokens = re.findall(r"0x[0-9a-fA-F]+", resp)
        raw = bytes(int(t, 16) & 0xFF for t in tokens) if tokens else bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", resp))
        if len(raw) < 8:
            raise RuntimeError(f"WMI response too short: {resp[:60]}")
        sig, code = raw[:4], struct.unpack("<I", raw[4:8])[0]
        if sig != b"PASS" or code != 0:
            raise RuntimeError(f"WMI {ctype:#x} returned {sig.decode(errors='ignore')} code {code}")
        return raw[8:]

    def _read_state(self):
        return self._wmi(self.WMI_GM, self.FAN_STATE_QUERY, b"", 128)

    def _write_speeds(self, cpu, gpu, fan3=0):
        self._wmi(self.WMI_GM, self.FAN_SPEED_SET, bytes([cpu, gpu, fan3]), 128)

    def _write_reverse(self, speed, fan3):
        """speed 0 = brake in reverse; fan3 only if the board reports one."""
        b = FAN_REVERSE_FLAG | speed
        self._write_speeds(b, b, b if fan3 else 0)

    # --- capabilities -------------------------------------------------------
    def capabilities(self, refresh=False):
        """{"mode": "modern"|"legacy"|None, "cpu", "gpu", "fan3", "error"}"""
        if self._caps is not None and not refresh:
            return self._caps
        caps = {"mode": None, "cpu": False, "gpu": False, "fan3": False, "error": None}
        if not self.acpi_call_available():
            caps["error"] = "acpi_call module not loaded"
            self._caps = caps
            return caps
        try:
            data = self._read_state()
            if len(data) > 8:
                caps["cpu"], caps["gpu"], caps["fan3"] = bool(data[8] & 1), bool(data[8] & 2), bool(data[8] & 4)
                if caps["cpu"] or caps["gpu"] or caps["fan3"]:
                    caps["mode"] = "modern"
        except Exception as e:
            caps["error"] = str(e)
        if caps["mode"] is None:
            try:
                data = self._wmi(1, self.LEGACY_QUERY, b"", 4)   # HPWMI_READ
                if data and data[0] & 0x20:
                    caps["mode"] = "legacy"
                    caps["error"] = None
            except Exception as e:
                caps["error"] = caps["error"] or str(e)
        self._caps = caps
        return caps

    def is_supported(self):
        return self.capabilities()["mode"] is not None

    def is_reversing(self):
        return self.controller.get_fan_state(1)[1] or self.controller.get_fan_state(2)[1]

    # --- state / request files ---------------------------------------------
    @staticmethod
    def read_state():
        try:
            with open(CLEANER_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def _update_state(cls, **fields):
        state = cls.read_state()
        state.update(fields)
        try:
            CLEANER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CLEANER_STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, CLEANER_STATE_FILE)
        except Exception as e:
            print(f"Error writing cleaner state: {e}")
        return state

    @staticmethod
    def is_running():
        """True while some process is in a run. A "running" state left behind
        by a crashed process (dead pid, or far past its duration) is ignored."""
        state = FanCleaner.read_state()
        if state.get("state") != "running":
            return False
        if time.time() - state.get("started", 0) > state.get("duration", 0) + 60:
            return False
        pid = state.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                pass
        return True

    @staticmethod
    def request(duration=None, speed=None):
        """Asks the running daemon to clean (it polls for this file)."""
        CLEANER_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CLEANER_REQUEST_FILE, "w") as f:
            json.dump({"ts": time.time(), "duration": duration, "speed": speed}, f)

    @staticmethod
    def take_request():
        """Daemon side: returns and removes a pending request, or None."""
        if not CLEANER_REQUEST_FILE.exists():
            return None
        try:
            with open(CLEANER_REQUEST_FILE) as f:
                req = json.load(f)
        except Exception:
            req = {}
        try:
            CLEANER_REQUEST_FILE.unlink()
        except Exception:
            pass
        return req

    @staticmethod
    def request_stop():
        CLEANER_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLEANER_STOP_FILE.touch()

    @staticmethod
    def _stop_requested():
        if CLEANER_STOP_FILE.exists():
            try:
                CLEANER_STOP_FILE.unlink()
            except Exception:
                pass
            return True
        return False

    # --- automatic schedule ------------------------------------------------
    @staticmethod
    def parse_interval(text):
        """'36' / '36h' / '7d' / '2w' / '1m' -> hours (float), or None."""
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hdwm]?)\s*", str(text).lower())
        if not m:
            return None
        n, unit = float(m.group(1)), m.group(2)
        return n * {"": 1, "h": 1, "d": 24, "w": 168, "m": 720}[unit]

    @staticmethod
    def format_interval(hours):
        for label, h in CLEANER_INTERVAL_PRESETS:
            if abs(hours - h) < 1e-6:
                return f"every {label}"
        if hours % 24 == 0:
            return f"every {int(hours // 24)} days"
        return f"every {hours:g} hours"

    def _window(self):
        cfg = self.controller.config
        try:
            a = int(cfg.get("cleaner_window_start", DEFAULT_CLEANER_WINDOW[0])) % 24
            b = int(cfg.get("cleaner_window_end", DEFAULT_CLEANER_WINDOW[1])) % 24
        except (TypeError, ValueError):
            a, b = DEFAULT_CLEANER_WINDOW
        return a, b

    def in_window(self, ts=None):
        """True if the allowed-hours window contains ts (local time). A window
        like 22-6 wraps past midnight; start == end means no restriction."""
        a, b = self._window()
        if a == b:
            return True
        h = time.localtime(ts if ts is not None else time.time()).tm_hour
        return a <= h < b if a < b else (h >= a or h < b)

    def next_window_start(self, ts):
        """Earliest time >= ts inside the window (ts itself if already inside)."""
        if self.in_window(ts):
            return ts
        a, _ = self._window()
        lt = time.localtime(ts)
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, a, 0, 0, 0, 0, -1))
        return day_start if day_start > ts else day_start + 86400

    def next_auto_time(self):
        """When the next automatic run would start, or None if auto is off /
        not armed yet. Postponements (hot, battery) are not predictable."""
        cfg = self.controller.config
        if not cfg.get("cleaner_auto", False):
            return None
        state = self.read_state()
        base = state.get("last_run") or state.get("auto_armed")
        if not base:
            return None
        interval = max(1, float(cfg.get("cleaner_interval_hours", DEFAULT_CLEANER_INTERVAL_HOURS))) * 3600
        return self.next_window_start(max(base + interval, time.time()))

    def auto_due(self):
        """Daemon side: True when the periodic cleaning should run now. The
        first check after enabling auto only starts the countdown."""
        cfg = self.controller.config
        if not cfg.get("cleaner_auto", False):
            return False
        interval = max(1, float(cfg.get("cleaner_interval_hours", DEFAULT_CLEANER_INTERVAL_HOURS))) * 3600
        state = self.read_state()
        last = state.get("last_run") or state.get("auto_armed")
        if not last:
            self._update_state(auto_armed=time.time())
            return False
        if time.time() - last < interval:
            return False
        if not self.in_window():
            return False
        if time.time() - state.get("auto_postponed", 0) < CLEANER_AUTO_RETRY_SECS:
            return False
        if not self.controller.is_on_ac_power() or self.controller.get_core_mean_temp() > CLEANER_MAX_TEMP:
            self._update_state(auto_postponed=time.time())
            return False
        return True

    # --- the run ------------------------------------------------------------
    def run_blocking(self, duration=None, speed=None):
        gen = self.run(duration, speed)
        try:
            while True:
                next(gen)
        except StopIteration as e:
            return e.value

    def run(self, duration=None, speed=None):
        """Generator: yields progress 0-100, returns (success, message).
        On return the fans are spinning forward in driver manual mode at a low
        speed; the caller restores the configured mode."""
        cfg = self.controller.config
        lo, hi = CLEANER_DURATION_RANGE
        duration = int(min(max(int(duration or cfg.get("cleaner_duration", DEFAULT_CLEANER_DURATION)), lo), hi))
        lo, hi = CLEANER_SPEED_RANGE
        speed = int(min(max(int(speed or cfg.get("cleaner_speed", DEFAULT_CLEANER_SPEED)), lo), hi))

        if self.is_running():
            return False, "A fan cleaning run is already in progress."
        caps = self.capabilities(refresh=True)
        if caps["mode"] is None:
            return False, f"Fan cleaning not available: {caps['error'] or 'not supported by this board'}"
        if not (self.controller.pwm1_path and self.controller.pwm1_path.exists()):
            return False, "Fan cleaning needs the patched hp-wmi driver (fan speed readback)."
        temp = self.controller.get_core_mean_temp()
        if temp > CLEANER_MAX_TEMP:
            msg = f"CPU too hot for fan cleaning ({temp:.0f}°C > {CLEANER_MAX_TEMP}°C)."
            self._update_state(last_status=msg, last_status_ts=time.time(), last_ok=False)
            return False, msg

        self._stop_requested()   # clear a stale stop file
        started = time.time()
        self._update_state(state="running", phase="starting", progress=0, started=started,
                           duration=duration, speed=speed, pid=os.getpid())
        print(f"Fan cleaning: {duration}s at {speed * 100} RPM reverse ({caps['mode']})")
        ok, msg = False, "Fan cleaning failed."
        try:
            if caps["mode"] == "legacy":
                gen = self._run_legacy(duration)
            else:
                gen = self._run_modern(duration, speed, caps["fan3"])
            try:
                while True:
                    yield next(gen)
            except StopIteration as e:
                ok, msg = e.value
        except Exception as e:
            ok, msg = False, f"Fan cleaning aborted: {e}"
        finally:
            # whatever happened, leave the EC in charge and the fans forward
            try:
                self._release(caps)
            except Exception as e:
                print(f"Fan cleaning: release failed: {e}")
            self._update_state(state="idle", phase="done", progress=100,
                               last_run=started, last_status=msg, last_status_ts=time.time(), last_ok=ok)
            print(f"Fan cleaning: {msg}")
        return ok, msg

    def _phase(self, phase, progress):
        self._update_state(phase=phase, progress=int(progress))
        return int(progress)

    def _both_fans(self):
        r1, rev1 = self.controller.get_fan_state(1)
        r2, rev2 = self.controller.get_fan_state(2)
        return r1, rev1, r2, rev2

    def _run_modern(self, duration, speed, fan3):
        ctl = self.controller
        # The daemon/GUI is paused while we run; put the driver in auto so its
        # keep-alive worker cannot re-send a forward speed mid-run.
        ctl.set_fan_mode("auto")
        time.sleep(0.3)
        self._wmi(self.WMI_GM, self.FAN_COUNT_QUERY, bytes([0]), 4, datasize=1)

        # 1. brake: reverse bit with speed 0, wait for the fans to stop
        yield self._phase("braking", 2)
        self._write_reverse(0, fan3)
        t0 = time.time()
        while True:
            r1, _, r2, _ = self._both_fans()
            if r1 < self.STOPPED_RPM and r2 < self.STOPPED_RPM:
                break
            if time.time() - t0 > self.BRAKE_TIMEOUT:
                return False, f"Fans did not stop within {self.BRAKE_TIMEOUT:.0f}s ({r1}/{r2} RPM), reversal cancelled."
            if self._stop_requested():
                return False, "Fan cleaning stopped by user."
            time.sleep(0.3)
        time.sleep(0.3)

        # 2. reverse for `duration`
        self._write_reverse(speed, fan3)
        t0 = time.time()
        temps = []
        engaged = False
        while True:
            elapsed = time.time() - t0
            if elapsed >= duration:
                break
            yield self._phase("reversing", 5 + 85 * elapsed / duration)
            if self._stop_requested():
                return False, "Fan cleaning stopped by user."
            r1, rev1, r2, rev2 = self._both_fans()
            if rev1 or rev2:
                engaged = True
            elif elapsed > 4.0 and not engaged:
                return False, "The EC did not engage reverse rotation; this board may not support fan cleaning."
            temps.append(ctl.get_core_mean_temp())
            del temps[:-3]
            if len(temps) == 3 and sum(temps) / 3 > CLEANER_MAX_TEMP:
                return False, f"Stopped early: CPU reached {sum(temps) / 3:.0f}°C."
            time.sleep(0.5)
        return True, f"Fan cleaning completed ({duration}s)."

    def _run_legacy(self, duration):
        rec = bytearray(self._wmi(1, self.LEGACY_QUERY, b"", 4)[:4])
        rec[3] |= 0x82
        self._wmi(2, self.LEGACY_QUERY, bytes(rec), 0)      # HPWMI_WRITE
        t0 = time.time()
        while time.time() - t0 < duration:
            yield self._phase("reversing", 5 + 85 * (time.time() - t0) / duration)
            if self._stop_requested():
                return False, "Fan cleaning stopped by user."
            if self.controller.get_core_mean_temp() > CLEANER_MAX_TEMP:
                return False, "Stopped early: CPU too hot."
            time.sleep(0.5)
        return True, f"Fan cleaning completed ({duration}s)."

    def _release(self, caps):
        """Decelerate in reverse, give the fans back to the EC, wait for them
        to stop, then soft-start forward through the driver."""
        ctl = self.controller
        self._phase("stopping", 92)
        if caps["mode"] == "legacy":
            rec = bytearray(self._wmi(1, self.LEGACY_QUERY, b"", 4)[:4])
            rec[3] = (rec[3] | 0x02) & 0x7F
            self._wmi(2, self.LEGACY_QUERY, bytes(rec), 0)
        else:
            current = 0
            try:
                data = self._read_state()
                if data[0] & FAN_REVERSE_FLAG:
                    current = data[0] & (FAN_REVERSE_FLAG - 1)
            except Exception:
                pass
            for s in list(range(current, 0, -5)) + [0]:
                self._write_reverse(s, caps["fan3"])
                time.sleep(0.15)
            self._write_speeds(0, 0, 0)     # back to EC automatic control
        t0 = time.time()
        while time.time() - t0 < 4.0:
            r1, rev1, r2, rev2 = self._both_fans()
            if not rev1 and not rev2 and r1 < self.STOPPED_RPM and r2 < self.STOPPED_RPM:
                break
            time.sleep(0.25)
        self._phase("soft start", 96)
        ctl.set_fan_pwm(CLEANER_SOFT_START_PWM)
        time.sleep(1.0)
