# wisperflow

Push-to-talk dictation for Windows. Hold a hotkey, speak, release — polished
text gets pasted into whatever app has focus. Everything runs locally:
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech-to-text
and [Ollama](https://ollama.com) for LLM rewriting. No API keys, no cloud, no
per-token cost.

## Install on Windows

Requires Windows 10/11 with `winget` (ships by default on current Windows).

```powershell
git clone https://github.com/nucleoid/wisperflow.git wisperflow
cd wisperflow
.\install.ps1
```

The installer:

1. Installs Python 3.12 and Ollama via `winget` (skipped if already present)
2. Creates a `.venv` and installs `requirements.txt`
3. Starts the Ollama server and pulls `qwen2.5:3b` (~2 GB)
4. Writes a starter `.env`
5. Creates a launcher (`wisperflow.cmd`) and a Start Menu shortcut
6. Optionally adds wisperflow to Windows startup

It's idempotent — re-run it any time to repair a broken setup or upgrade deps.

### Installer flags

```powershell
.\install.ps1 -Model llama3.2:3b     # use a different Ollama model
.\install.ps1 -Autostart             # add to startup without prompting
.\install.ps1 -SkipAutostart         # skip the autostart prompt
```

### After install: pick a mic

```powershell
.\wisperflow.cmd --list-devices      # see every input device
.\wisperflow.cmd --mic-test 5        # live level meter (talk into mic)
```

Set `WISPERFLOW_INPUT_DEVICE` in `.env` to the index or a substring of the
device name (e.g. `HyperX`, `realtek`, `oculus`). Leave blank for the system
default.

## Daily use

Launch from the Start Menu or run `.\wisperflow.cmd`. Then:

- **Hold `Ctrl+Alt+Space`** → speak → release. Polished text is pasted into
  the focused app.
- A small pill in the bottom-right corner shows `listening` → `transcribing`
  → `polishing`.
- `Ctrl+C` in the terminal to quit.

## Configuration

All knobs live in `.env`. The most common ones:

| Variable                     | Purpose                                                      |
|------------------------------|--------------------------------------------------------------|
| `WISPERFLOW_HOTKEY`          | Push-to-talk hotkey (default `<ctrl>+<alt>+<space>`)         |
| `WISPERFLOW_MODEL`           | Whisper model (`base.en` default, `small.en` for accuracy)   |
| `WISPERFLOW_OLLAMA_MODEL`    | Ollama model for rewriting (default `qwen2.5:3b`)            |
| `WISPERFLOW_INPUT_DEVICE`    | Mic index or name substring                                  |
| `WISPERFLOW_INPUT_GAIN`      | Sample multiplier for a quiet mic (`1.0` = no change)        |
| `WISPERFLOW_INDICATOR`       | Floating pill overlay (`1` = on, `0` = off)                  |
| `WISPERFLOW_BEEP`            | State-change beeps (`1` = on, `0` = off)                     |

## Troubleshooting

- **Mic seems dead** — run `.\wisperflow.cmd --mic-test 5`. If `peak=0.000`,
  check Settings → Privacy & security → Microphone (both the "Microphone
  access" and "Let desktop apps access your microphone" toggles must be on),
  then confirm the mic isn't muted under Sound settings → Input.
- **"Invalid sample rate"** — some WASAPI devices only accept 48 kHz. The
  recorder auto-negotiates, but if you see this error, open an issue with the
  output of `--list-devices`.
- **Ctrl+C doesn't quit** — make sure you're on the current code; the main
  loop polls for signals every 500ms.
- **LLM answers questions instead of transcribing them** — you're on an old
  rewriter. Pull the latest code or switch `WISPERFLOW_OLLAMA_MODEL` to
  `qwen2.5:3b`.

## Uninstall

```powershell
# Remove the repo
Remove-Item -Recurse -Force <path-to-wisperflow>

# Remove the Start Menu + autostart shortcuts
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Wisperflow.lnk" -ErrorAction SilentlyContinue
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Wisperflow.lnk" -ErrorAction SilentlyContinue

# Optional: uninstall Ollama and its models
winget uninstall Ollama.Ollama
Remove-Item -Recurse -Force "$env:USERPROFILE\.ollama"
```
