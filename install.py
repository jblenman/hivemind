#!/usr/bin/env python
"""
install.py - Add hivemind commands (hm, hmc) to your system PATH.

Usage:
    python install.py           # Add this directory to PATH
    python install.py --remove  # Remove this directory from PATH
    python install.py --check   # Check if already installed

Works on Windows (modifies user PATH via registry) and macOS/Linux
(appends to shell profile). Google Drive sync means the scripts are
always up to date — this just makes them callable from anywhere.
"""

import sys
import os
import argparse
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_shell_profile():
    """Find the active shell profile on macOS/Linux."""
    home = os.path.expanduser("~")
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return os.path.join(home, ".zshrc")
    elif "fish" in shell:
        return os.path.join(home, ".config", "fish", "config.fish")
    # Default to bash
    profile = os.path.join(home, ".bashrc")
    if not os.path.exists(profile):
        profile = os.path.join(home, ".bash_profile")
    return profile


def is_in_path(directory):
    """Check if a directory is already in PATH."""
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    normed = os.path.normcase(os.path.normpath(directory))
    for d in path_dirs:
        if os.path.normcase(os.path.normpath(d)) == normed:
            return True
    return False


# ── Windows ──────────────────────────────────────────────────────────────

def windows_get_user_path():
    """Read user PATH from Windows registry."""
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(key, "Path")
        winreg.CloseKey(key)
        return value
    except FileNotFoundError:
        return ""


def windows_set_user_path(new_path):
    """Write user PATH to Windows registry."""
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
    winreg.CloseKey(key)
    # Broadcast WM_SETTINGCHANGE so running shells pick it up
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            0x0002,  # SMTO_ABORTIFHUNG
            5000, ctypes.byref(ctypes.c_long()))
    except Exception:
        pass


def windows_install(directory):
    """Add directory to user PATH on Windows."""
    current = windows_get_user_path()
    entries = [e.strip() for e in current.split(";") if e.strip()]
    normed = os.path.normcase(os.path.normpath(directory))

    for e in entries:
        if os.path.normcase(os.path.normpath(e)) == normed:
            print("Already in PATH: {}".format(directory))
            return True

    entries.append(directory)
    windows_set_user_path(";".join(entries))
    print("Added to user PATH: {}".format(directory))
    print("Open a new terminal for changes to take effect.")
    return True


def windows_remove(directory):
    """Remove directory from user PATH on Windows."""
    current = windows_get_user_path()
    entries = [e.strip() for e in current.split(";") if e.strip()]
    normed = os.path.normcase(os.path.normpath(directory))

    new_entries = [e for e in entries
                   if os.path.normcase(os.path.normpath(e)) != normed]

    if len(new_entries) == len(entries):
        print("Not in PATH: {}".format(directory))
        return True

    windows_set_user_path(";".join(new_entries))
    print("Removed from user PATH: {}".format(directory))
    print("Open a new terminal for changes to take effect.")
    return True


# ── macOS / Linux ────────────────────────────────────────────────────────

MARKER = "# hivemind PATH"


def unix_install(directory):
    """Add directory to PATH via shell profile on macOS/Linux."""
    profile = get_shell_profile()
    line = 'export PATH="$PATH:{}"  {}'.format(directory, MARKER)

    # Check if already added
    if os.path.exists(profile):
        with open(profile, 'r') as f:
            content = f.read()
        if MARKER in content:
            print("Already in {}: {}".format(profile, directory))
            return True

    with open(profile, 'a') as f:
        f.write("\n{}\n".format(line))

    print("Added to {}: {}".format(profile, directory))
    print("Run: source {} (or open a new terminal)".format(profile))
    return True


def unix_remove(directory):
    """Remove hivemind PATH entry from shell profile."""
    profile = get_shell_profile()
    if not os.path.exists(profile):
        print("Profile not found: {}".format(profile))
        return True

    with open(profile, 'r') as f:
        lines = f.readlines()

    new_lines = [l for l in lines if MARKER not in l]

    if len(new_lines) == len(lines):
        print("Not found in {}.".format(profile))
        return True

    with open(profile, 'w') as f:
        f.writelines(new_lines)

    print("Removed from {}.".format(profile))
    print("Run: source {} (or open a new terminal)".format(profile))
    return True


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Install hivemind commands (hm, hmc) to system PATH")
    parser.add_argument("--remove", action="store_true",
                        help="Remove hivemind from PATH")
    parser.add_argument("--check", action="store_true",
                        help="Check if hivemind is in PATH")
    args = parser.parse_args()

    if args.check:
        if is_in_path(SCRIPT_DIR):
            print("Installed: {} is in PATH".format(SCRIPT_DIR))
            print("  hm  -> hivemind router")
            print("  hmc -> hivemind coding assistant")
        else:
            print("Not installed: {} is not in PATH".format(SCRIPT_DIR))
        return

    if sys.platform == "win32":
        if args.remove:
            windows_remove(SCRIPT_DIR)
        else:
            windows_install(SCRIPT_DIR)
    else:
        if args.remove:
            unix_remove(SCRIPT_DIR)
        else:
            unix_install(SCRIPT_DIR)

    if not args.remove:
        print()
        print("Commands available after restart:")
        print("  hm   \"your prompt\"          # route to best model")
        print("  hm   -i                     # interactive router")
        print("  hm   --status               # fleet status")
        print("  hmc                          # coding assistant")
        print("  hmc  --escalate             # auto-upgrade on hard tasks")
        print("  hmc  --trust \"run tests\"    # auto-approve actions")


if __name__ == "__main__":
    main()
