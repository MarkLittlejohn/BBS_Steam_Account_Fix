#!/usr/bin/env python3
r"""
BBS Steam Account Fix Script
- Modifies HKCU\Software\KLab\BleachBraveSouls to replace a specific value
- Supports restore points and Volume Shadow Copy snapshots prior to 2025-10-04 09:10:16 UTC
- Supports optional .reg file input
- Creates backups of live registry, snapshot/VSS, and fixed version
- Supports simulation (dry-run) and live merge modes via --mode argument
- Suppresses “The operation completed successfully.” messages
"""

import os
import subprocess
import re
import glob
import winreg
import ctypes
import datetime
import time
import argparse

# ---------------- GLOBAL VARIABLES ----------------
SIMULATION_MODE = True  # default simulation mode

TEMP_MOUNT_NAME = r"TempHive"
TARGET_REL_PATH = r"Software\KLab\BleachBraveSouls"
OUTPUT_REG_FILE = os.path.abspath(r"BleachBraveSouls_from_VSS_PostUpdate_Fix.reg")
LIVE_BACKUP_FILE = os.path.abspath(r"BleachBraveSouls_PostUpdate_Original.reg")
SNAPSHOT_EXPORT_FILE = os.path.abspath(r"BleachBraveSouls_FromVSS.reg")
OLD_VALUE_NAME = r"224515408_h90860828"
NEW_VALUE_NAME = r"-907038497_h2948477015"
SLEEP_AFTER_LOAD = 0.15

# Cutoff datetime (UTC)
CUTOFF_DATETIME = datetime.datetime(2025, 10, 4, 9, 10, 16, tzinfo=datetime.timezone.utc)
# ----------------------------------------

# ---------------- UTILITY FUNCTIONS ----------------

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def get_user_sid():
    """Return current user SID."""
    try:
        proc = subprocess.run(["whoami", "/user"], capture_output=True, text=True, check=True)
        out = proc.stdout
        m = re.search(r"(S-1-[0-9]-[0-9]+(?:-[0-9]+)+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    raise RuntimeError("Could not determine user SID.")

# ---------------- BACKUP LIVE HKCU KEY ----------------

def backup_live_hkcu_key():
    """Backup the live HKCU\Software\KLab\BleachBraveSouls key."""
    try:
        full_key = r"HKCU\Software\KLab\BleachBraveSouls"
        subprocess.run(["reg", "export", full_key, LIVE_BACKUP_FILE, "/y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print(f"Live registry key backed up to {LIVE_BACKUP_FILE}")
    except subprocess.CalledProcessError as e:
        print("Failed to backup live key:", e)

# ---------------- REG FILE HANDLING ----------------

def process_regfile(regfile_path):
    if not os.path.isfile(regfile_path):
        print(f"REG file {regfile_path} does not exist.")
        return False

    try:
        with open(regfile_path, "r", encoding="utf-16") as f:
            data = f.read()
    except Exception:
        print(f"Failed to read {regfile_path} as UTF-16. Trying UTF-8...")
        with open(regfile_path, "r", encoding="utf-8") as f:
            data = f.read()

    target_key_line = f"[HKEY_CURRENT_USER\\{TARGET_REL_PATH}]"
    if target_key_line not in data:
        print(f"{target_key_line} not found in {regfile_path}. Proceeding to restore point/VSS search.")
        return False

    # Backup live HKCU key
    backup_live_hkcu_key()

    # Replace old value name with new value name
    data_fixed = data.replace(OLD_VALUE_NAME, NEW_VALUE_NAME)
    output_file = os.path.abspath("BleachBraveSouls_from_REGBackup_File_PostUpdate_Fix.reg")
    with open(output_file, "w", encoding="utf-16") as f:
        f.write(data_fixed)
    print(f"Fixed REG exported to {output_file}")

    # Merge if live
    if not SIMULATION_MODE:
        try:
            subprocess.run(["reg", "import", output_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            print(f"Merged modified REG into live registry: {output_file}")
        except subprocess.CalledProcessError as e:
            print("Failed to merge registry:", e)
    return True

# ---------------- RESTORE POINTS ----------------

def find_restore_point_snapshot_dirs():
    base = r"C:\System Volume Information"
    candidates = []
    pattern = os.path.join(base, "**", "RP*")
    for rp_dir in glob.iglob(pattern, recursive=True):
        if os.path.isdir(rp_dir):
            snapshot_dir = os.path.join(rp_dir, "snapshot")
            if os.path.isdir(snapshot_dir):
                candidates.append((rp_dir, snapshot_dir))
    return candidates

def rp_is_before_cutoff(rp_dir):
    mtime = os.path.getmtime(rp_dir)
    rp_dt = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
    if rp_dt >= CUTOFF_DATETIME:
        print(f"Skipping restore point {rp_dir} (datetime {rp_dt} >= cutoff {CUTOFF_DATETIME})")
        return False
    return True

def sort_rps_newest_first(rp_list):
    return sorted(rp_list, key=lambda t: os.path.getmtime(t[0]), reverse=True)

def find_user_hive_in_snapshot(snapshot_dir, sid):
    for entry in os.listdir(snapshot_dir):
        if sid.lower() in entry.lower():
            full = os.path.join(snapshot_dir, entry)
            if os.path.isfile(full):
                return full
    return None

# ---------------- VSS ----------------

def list_vss_snapshots():
    result = subprocess.run(["vssadmin", "list", "shadows"], capture_output=True, text=True)
    shadows = []
    for line in result.stdout.splitlines():
        if "Shadow Copy Volume" in line:
            path = line.split(":")[-1].strip()
            shadows.append(path)
    return shadows

def vss_is_before_cutoff(shadow_path):
    try:
        ctime = os.path.getctime(shadow_path)
        dt = datetime.datetime.fromtimestamp(ctime, datetime.timezone.utc)
        if dt == datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc):
            print(f"Skipping VSS snapshot {shadow_path}: invalid timestamp")
            return False
        if dt >= CUTOFF_DATETIME:
            print(f"Skipping VSS snapshot {shadow_path} (datetime {dt} >= cutoff {CUTOFF_DATETIME})")
            return False
        return True
    except Exception:
        print(f"Could not read creation time for VSS snapshot {shadow_path}, skipping")
        return False

def list_vss_snapshots_before_cutoff():
    """Return a list of VSS shadow copy paths created before the cutoff datetime."""
    result = subprocess.run(["vssadmin", "list", "shadows"], capture_output=True, text=True)
    shadows = []
    current = {}

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Shadow Copy ID:"):
            current["id"] = line.split(":")[-1].strip()
        elif "at creation time:" in line:
            # Example: "Contained 1 shadow copies at creation time: 10/1/2025 12:11:45 PM"
            date_str = line.split("at creation time:")[-1].strip()
            try:
                local_time = datetime.datetime.strptime(date_str, "%m/%d/%Y %I:%M:%S %p")
                # Convert local time to UTC
                local_time = local_time.astimezone(datetime.timezone.utc)
                current["ctime"] = local_time
            except Exception as e:
                print(f"Warning: Could not parse VSS time '{date_str}': {e}")
                current["ctime"] = None
        elif line.startswith("Shadow Copy Volume:"):
            path = line.split(":")[-1].strip()
            current["path"] = path
            if "ctime" in current and current["ctime"]:
                before_cutoff = current["ctime"] < CUTOFF_DATETIME
                print(f"Debug: VSS {path}, created {current['ctime']} UTC, before cutoff? {before_cutoff}")
                if before_cutoff:
                    shadows.append(path)
            current = {}

    if not shadows:
        print("No valid VSS snapshots found before cutoff datetime.")
    return shadows

# ---------------- REGISTRY HIVE FUNCTIONS ----------------

def reg_load(hive_path, mount_name):
    subprocess.run(["reg", "load", rf"HKU\{mount_name}", hive_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    time.sleep(SLEEP_AFTER_LOAD)

def reg_unload(mount_name):
    subprocess.run(["reg", "unload", rf"HKU\{mount_name}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def modify_hive_value(hive_mount_name):
    root = winreg.HKEY_USERS
    key_path = rf"{hive_mount_name}\{TARGET_REL_PATH}"
    try:
        with winreg.OpenKey(root, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            val, vtype = winreg.QueryValueEx(key, OLD_VALUE_NAME)
            if not SIMULATION_MODE:
                #winreg.DeleteValue(key, OLD_VALUE_NAME)
                winreg.SetValueEx(key, NEW_VALUE_NAME, 0, vtype, val)
            return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print("Error modifying hive:", e)
        return False

def export_and_save(hive_mount_name):
    """Export snapshot/VSS hive and create fixed copy with HKEY_CURRENT_USER root."""
    full_key = rf"HKU\{hive_mount_name}\{TARGET_REL_PATH}"
    try:
        subprocess.run(["reg", "export", full_key, SNAPSHOT_EXPORT_FILE, "/y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError as e:
        print("Failed to export snapshot/VSS hive:", e)
        return False

    # Correct header line and value replacement
    try:
        with open(SNAPSHOT_EXPORT_FILE, "r", encoding="utf-16") as f:
            data = f.read()
        data_fixed = data.replace(
            rf"[HKEY_USERS\{hive_mount_name}\{TARGET_REL_PATH}]",
            rf"[HKEY_CURRENT_USER\{TARGET_REL_PATH}]"
        )
        # Duplicate OLD_VALUE_NAME line as NEW_VALUE_NAME
        import re
        lines = data_fixed.splitlines()
        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            new_lines.append(line)

            if line.strip().startswith(f"\"{OLD_VALUE_NAME}\"="):
                # Capture the full block (handles hex multi-line continuation with '\')
                block_lines = [line]
                j = i + 1
                while j < len(lines) and lines[j].strip().endswith("\\"):
                    block_lines.append(lines[j])
                    new_lines.append(lines[j])
                    j += 1

                # Duplicate the entire block, replacing only the value name
                block_text = "\n".join(block_lines)
                new_block = block_text.replace(OLD_VALUE_NAME, NEW_VALUE_NAME)
                new_lines.append(new_block)
                i = j - 1  # jump forward to the last continuation line
            i += 1

        data_fixed = "\n".join(new_lines)

        with open(OUTPUT_REG_FILE, "w", encoding="utf-16") as f:
            f.write(data_fixed)

        print(f"Fixed REG file created: {OUTPUT_REG_FILE}")
        if not SIMULATION_MODE:
            subprocess.run(["reg", "import", OUTPUT_REG_FILE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            print(f"Merged modified key into live registry: {OUTPUT_REG_FILE}")
        return True
    except Exception as e:
        print("Error creating fixed export:", e)
        return False

# ---------------- SEARCH FUNCTIONS ----------------

def search_old_restore_points(sid):
    rps = find_restore_point_snapshot_dirs()
    rps_filtered = [t for t in rps if rp_is_before_cutoff(t[0])]
    rps_sorted = sort_rps_newest_first(rps_filtered)
    if not rps_sorted:
        print("No restore points before cutoff datetime.")
        return False
    for rp_dir, snapshot_dir in rps_sorted:
        hive_file = find_user_hive_in_snapshot(snapshot_dir, sid)
        if hive_file:
            try:
                reg_load(hive_file, TEMP_MOUNT_NAME)
                modified = modify_hive_value(TEMP_MOUNT_NAME)
                if modified:
                    export_and_save(TEMP_MOUNT_NAME)
                    reg_unload(TEMP_MOUNT_NAME)
                    return True
                reg_unload(TEMP_MOUNT_NAME)
            except:
                try: reg_unload(TEMP_MOUNT_NAME)
                except: pass
    print("No suitable restore point found with the key before cutoff.")
    return False

def search_vss_snapshots():
    """Search through VSS snapshots for NTUSER.DAT before cutoff datetime."""
    shadows = list_vss_snapshots_before_cutoff()
    if not shadows:
        return False

    username = os.environ.get("USERNAME")
    found = False

    for shadow in shadows:
        ntuser_path = os.path.join(shadow, r"Users", username, "NTUSER.DAT")
        if os.path.exists(ntuser_path):
            print(f"Found candidate NTUSER.DAT in VSS snapshot: {ntuser_path}")
            try:
                reg_load(ntuser_path, TEMP_MOUNT_NAME)
                if modify_hive_value(TEMP_MOUNT_NAME):
                    export_and_save(TEMP_MOUNT_NAME)
                    found = True
                reg_unload(TEMP_MOUNT_NAME)
                if found:
                    break
            except Exception as e:
                print(f"Error accessing snapshot {shadow}: {e}")
                try:
                    reg_unload(TEMP_MOUNT_NAME)
                except Exception:
                    pass

    if not found:
        print("No suitable VSS snapshot found with the target key before cutoff.")
    return found

# ---------------- MAIN ----------------

def main():
    global SIMULATION_MODE

    if os.name != "nt":
        print("This script is intended for Windows.")
        return
    if not is_admin():
        print("Run as Administrator!")
        return

    # Clean up leftover REG files
    for f in [SNAPSHOT_EXPORT_FILE, OUTPUT_REG_FILE]:
        if os.path.exists(f):
            os.remove(f)

    parser = argparse.ArgumentParser(description="BBS registry fixer")
    parser.add_argument('--mode', choices=['simulation','live'], default='simulation',
                        help='Mode to run: simulation (default) or live')
    parser.add_argument('--regfile', type=str, help='Optional .reg file to use instead of restore points/VSS')
    args = parser.parse_args()

    if args.mode == 'live':
        SIMULATION_MODE = False
    print(f"Running in {'LIVE' if not SIMULATION_MODE else 'SIMULATION'} mode")

    try:
        sid = get_user_sid()
    except RuntimeError as e:
        print("ERROR:", e)
        return

    if args.regfile:
        used_regfile = process_regfile(args.regfile)
        if used_regfile:
            return  # Skip RP/VSS search

    # Otherwise proceed to RP/VSS search
    backup_live_hkcu_key()
    found = search_old_restore_points(sid)
    if not found:
        found = search_vss_snapshots()

    if not found:
        print("Key not found in any restore point or VSS snapshot before cutoff.")

if __name__ == "__main__":
    main()
