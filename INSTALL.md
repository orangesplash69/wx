# Installing `wx` as a command

Goal: type `wx tokyo` in any terminal, from any directory.

It works the same way on every platform:

1. Keep the project in a **permanent folder**.
2. Give it its own **virtual environment** (so it never touches your system Python).
3. Put a tiny **launcher** named `wx` on your `PATH`. It runs the venv's Python
   directly, so you never have to activate anything.

| | Debian / Ubuntu | Arch | Windows 10/11 |
| --- | --- | --- | --- |
| Python package | `python3 python3-venv python3-pip` | `python python-pip` | Python 3.10+ (`winget`) |
| Launcher location | `~/.local/bin/wx` | `~/.local/bin/wx` | `%USERPROFILE%\bin\wx.cmd` |
| `PATH` step needed | usually no | yes | yes (script does it) |

Requires **Python 3.10 or newer**. Check with `python3 --version` (Windows: `py --version`).

> **Run every step from inside the project folder**, the one containing `main.py`.
> The launcher remembers that folder's location, so move the project *before*
> installing, or redo the launcher step afterwards.

---

## Debian / Ubuntu / Mint

```bash
# 1. Python and venv support (python3-venv is a separate package on Debian)
sudo apt update
sudo apt install python3 python3-venv python3-pip

# 2. Environment and dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Launcher
mkdir -p ~/.local/bin
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/main.py" "$@"\n' "$PWD" "$PWD" > ~/.local/bin/wx
chmod +x ~/.local/bin/wx
```

Debian-based systems add `~/.local/bin` to `PATH` at login once the folder exists.
Log out and back in, or for the current shell only:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Arch / Manjaro / EndeavourOS

```bash
# 1. Python (venv support is included)
sudo pacman -S python python-pip

# 2. Environment and dependencies
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Launcher
mkdir -p ~/.local/bin
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/main.py" "$@"\n' "$PWD" "$PWD" > ~/.local/bin/wx
chmod +x ~/.local/bin/wx
```

Arch does not add `~/.local/bin` to `PATH`. Add this line to `~/.bashrc` (or
`~/.zshrc`), then open a new terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Prefer a system-wide command? Write the same launcher to `/usr/local/bin/wx`
(for example with `sudo tee`) instead. No `PATH` change is needed then.

## Windows 10 / 11

Use [Windows Terminal](https://aka.ms/terminal) (the default on Windows 11). The old
console host garbles the block characters and colors.

Run this in **PowerShell**, from inside the project folder:

```powershell
# 1. Python (skip if you already have 3.10+). Then open a NEW PowerShell window
#    so that `py` is on PATH, and cd back into the project folder.
winget install Python.Python.3.13

# 2. Environment and dependencies
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 3. Launcher: a wx.cmd in a folder on your user PATH
$bin = "$env:USERPROFILE\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
@"
@echo off
set PYTHONUTF8=1
"$PWD\.venv\Scripts\python.exe" "$PWD\main.py" %*
"@ | Set-Content -Encoding ASCII "$bin\wx.cmd"

# 4. Add that folder to your user PATH (skipped if it is already there)
$path = [Environment]::GetEnvironmentVariable("Path", "User")
if (($path -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", "$path;$bin", "User")
}
```

Open a **new** terminal window so the `PATH` change takes effect. Don't use `setx`
for step 4: it silently truncates long values.

---

## Check it works

```bash
wx --help
wx "san francisco" -d 3
```

Quote city names that contain spaces.

## Configuration (optional)

`wx` reads a `.env` file from the **project folder**, not from where you run it, so
one file applies everywhere:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

| Variable | Purpose |
| --- | --- |
| `WX_CITY` | Default city when you don't pass one |
| `LATLNG_API_KEY` | Only needed for `--geo latlng` |
| `WX_CACHE_DIR`, `WX_CONFIG` | Move the cache or config file |

Variables set in your real environment override `.env`.

## Updating

From the project folder, after pulling or copying in new code:

```bash
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip ...
```

The launcher itself doesn't change.

## Uninstalling

Delete the launcher (`~/.local/bin/wx`, or `%USERPROFILE%\bin\wx.cmd` on Windows)
and the project folder. On Windows you can also remove `%USERPROFILE%\bin` from your
user `PATH`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `wx: command not found` / `'wx' is not recognized` | The launcher folder isn't on `PATH`. Open a new terminal; on Arch and Windows, redo the `PATH` step. |
| `ensurepip is not available` when creating the venv (Debian) | `sudo apt install python3-venv`, delete the half-made `.venv`, retry. |
| `No such file or directory` or `ModuleNotFoundError` after moving the project | The launcher still points at the old location. Recreate `.venv` and redo the launcher step from the new folder. |
| Boxes and blocks look like `?` or garbage | Use a terminal with UTF-8 and a modern font (Windows Terminal on Windows). |
| `py` is not recognized (Windows) | Open a new PowerShell window after installing Python, or use `python` instead of `py`. |
| Python is older than 3.10 | Install a newer Python and use it to create the venv (`python3.12 -m venv .venv`). |
