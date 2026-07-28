# Windows Setup Guide — Autopilot Checkpoint B.8

## 1. Get the project

Backup ZIP locations (from the Arena workspace):

- `/home/user/Autopilot_Backup_B8.zip`
- `/home/user/Autopilot/Autopilot_Backup_B8.zip`

Size: **1.2 MB**

There is no public HTTP download URL from this sandbox. Download the ZIP from the workspace file browser / download UI, then extract on Windows, e.g.:

```text
C:\Users\<you>\Documents\Autopilot\
```

## 2. Minimum Windows environment (~80% of current tests)

| Component | Requirement |
|-----------|-------------|
| OS | Windows 10/11 64-bit |
| Python | **3.10 or 3.11** (3.12 usually OK; avoid experimental) from python.org |
| pip / venv | Included with Python |
| RAM | 8 GB recommended (4 GB may work for tests without real TTS) |
| Disk | ~2 GB free for venv + deps (more if installing XTTS later) |
| FFmpeg | **Optional for 80% of unit tests**; required for full audio effects / later video |
| Piper/Kokoro/XTTS | Optional for now (synthetic TTS fallback exists) |

### Install Python

1. Install Python 3.11, check **Add python.exe to PATH**.
2. Open **cmd** or **PowerShell**.

### Create venv and install deps

```bat
cd C:\Users\<you>\Documents\Autopilot
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install pytest pytest-cov pytest-mock Pillow numpy psutil thefuzz python-Levenshtein python-docx pdfplumber pydub chardet cryptography requests
```

For a fuller install (may be heavy):

```bat
pip install -r requirements.txt

# Coqui XTTS is OPTIONAL and not in the default requirements
# (it conflicts with numpy>=1.24). See requirements_tts_optional.txt
```

If `TTS` / `kokoro-onnx` / `onnxruntime` fail, skip them for B.8 testing — generation falls back to synthetic audio.

### Run tests

```bat
set PYTHONPATH=%CD%
pytest tests -q --tb=short
```

Or use:

```bat
test.bat
```

(after venv activated)

## 3. Tests that should pass WITHOUT FFmpeg

These do not require a real `ffmpeg` binary:

- All **core** unit tests (`tests/unit/test_core_services.py`)
- **file_parser** (except PDF generation skip if no reportlab)
- **keyword_analyzer**
- **voice_profile_manager**
- **sfx_engine**
- **timeline_engine**
- **transition_engine** (only builds filter **strings**)
- **tts_engine_manager** tests that use synthetic fallback / skip real engines
- **integration** `test_pipeline_flow` (may warn about missing FFmpeg via pydub, but should still **PASS**)

Expect **~170 passed, ~4 skipped** similar to Linux.

## 4. Tests / features that NEED FFmpeg

Install FFmpeg essentials and place:

```text
engines\ffmpeg\ffmpeg.exe
engines\ffmpeg\ffprobe.exe
```

Or put `ffmpeg` on PATH.

Needs FFmpeg for full behavior (may still pass with fallbacks/warnings):

- Full `audio_processor` loudnorm two-pass (`normalize_to_lufs`)
- Full TTS voice effect chains (`apply_voice_effects`)
- Pitch shift via FFmpeg
- Future video: transitions/animations/export (B.8–B.12+)

Without FFmpeg you get:

- RMS loudness approximation
- Volume-only effect fallback
- pydub RuntimeWarning

## 5. Optional: real TTS later

| Engine | Notes |
|--------|-------|
| Piper | Download Windows release; put `piper.exe` + models under `engines\piper\` |
| Kokoro | `pip install kokoro-onnx onnxruntime` + models under `engines\kokoro\models\` |
| XTTS | Heavy; `pip install TTS` + model download |

## 6. What to report back

Please send:

1. `python --version`
2. `pytest tests -q --tb=line` full summary line
3. Any **FAILED** test names + first error
4. Whether FFmpeg was installed

Then we can approve B.9 safely.
