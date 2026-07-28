# Windows Known Issues — Autopilot Checkpoint B.8

Use this while running tests on Windows. Most items are **expected** and have workarounds until FFmpeg/TTS engines are installed.

## Severity legend

| Level | Meaning |
|-------|---------|
| **Blocker** | Must fix before trusting B.17 smoke / real renders |
| **Major** | Feature degraded; tests may still pass with fallbacks |
| **Minor** | Warning / cosmetic / later phase |
| **Info** | Expected behavior |

---

## 1. FFmpeg missing

| | |
|--|--|
| **Severity** | Major (Blocker for real video export) |
| **Symptom** | `RuntimeWarning: Couldn't find ffmpeg or avconv` from pydub; `apply_voice_effects` / `normalize_to_lufs` use fallbacks |
| **What still works** | WAV I/O via stdlib/wave + numpy; ducking; mix; limiter; synthetic TTS; filter **string** generation (transitions) |
| **What fails / degrades** | True two-pass LUFS (`loudnorm`); full EQ/reverb FFmpeg chains; later zoompan/xfade/export |
| **Workaround** | Install FFmpeg essentials; place at `engines\ffmpeg\ffmpeg.exe` and `ffprobe.exe`, **or** add to PATH |
| **Tests** | Unit/integration should still **pass** with warnings |

---

## 2. TTS engines not installed (Piper / Kokoro / XTTS)

| | |
|--|--|
| **Severity** | Major for product audio quality; Minor for current automated tests |
| **Symptom** | `generate_audio` succeeds with `"synthetic": true` and a warning |
| **What still works** | Full pipeline data flow; pause/emotion logic; file outputs exist |
| **What fails** | Real voice quality; word timestamps are approximate |
| **Workaround** | Accept synthetic for B.8; later install Piper under `engines\piper\piper.exe` + models |
| **Tests** | Piper/Kokoro/XTTS real-engine tests are **skipped** if missing |

---

## 3. Path handling

| | |
|--|--|
| **Severity** | Minor if you stay on `pathlib`; Major if something hardcodes `/` |
| **Design** | Code uses `pathlib.Path` and config-driven paths |
| **Verify on Windows** | Relative paths resolve from project root; `engines\`, `config\`, `projects\` work when CWD is project root |
| **Set** | `set PYTHONPATH=%CD%` before pytest |
| **Watch for** | Running tests from a different drive/CWD; spaces in path (usually OK with pathlib) |

---

## 4. Encoding / text files

| | |
|--|--|
| **Severity** | Minor |
| **Design** | Script load uses chardet + UTF-8 fallback |
| **Watch for** | Windows-1252 / ANSI scripts; Notepad “Unicode” UTF-16 files |
| **Workaround** | Save scripts as UTF-8; sample fixtures are UTF-8 |

---

## 5. Dependencies that may fail on Windows

| Package | Risk | Notes |
|---------|------|-------|
| `PyQt6` | Medium | Not needed for B.1–B.8 tests; Phase C |
| `TTS` (Coqui) | High | Large; may fail on some Python versions |
| `kokoro-onnx` / `onnxruntime` | Medium | Optional for B.8 |
| `wmi` | Windows-only | Intentionally not required yet (license Phase C) |
| `python-Levenshtein` | Low | Needs C++ build tools sometimes; `thefuzz` may work without speedup |
| `pydub` | Low | Needs FFmpeg for non-WAV; WAV OK |
| `Pillow` / `numpy` / `pytest` | Low | Standard wheels |

**Recommended minimal pip set for B.8 tests** (see `docs/WINDOWS_SETUP_B8.md`):

```text
pytest pytest-cov pytest-mock Pillow numpy psutil thefuzz python-Levenshtein
python-docx pdfplumber pydub chardet cryptography requests
```

---

## 6. Subprocess / executable names

| | |
|--|--|
| **Severity** | Major for real render |
| **Issue** | Code looks for `ffmpeg`, `ffmpeg.exe`, `piper`, `piper.exe` |
| **Windows expectation** | Prefer `engines\ffmpeg\ffmpeg.exe` and `engines\piper\piper.exe` |
| **Verify** | After install, `engines\ffmpeg\ffmpeg.exe -version` works |

---

## 7. Console / pytest quirks

| | |
|--|--|
| **Severity** | Info |
| **Watch for** | Unicode in console output (usually fine on Win10+); PowerShell vs cmd activation of venv |
| **venv activate** | `venv\Scripts\activate` (cmd) or `venv\Scripts\Activate.ps1` (PowerShell; may need execution policy) |

---

## 8. Database / SQLite

| | |
|--|--|
| **Severity** | Low |
| **Design** | SQLite + WAL; should work on Windows |
| **Watch for** | Antivirus locking `database\autopilot.db`; run tests with temp DB via fixtures (default) |

---

## 9. Sample assets / SFX

| | |
|--|--|
| **Severity** | Info |
| **Note** | SFX WAVs under `assets\sfx\` are **placeholders** (tones), not production audio |
| **Tests** | Designed to pass with placeholders |

---

## 10. Backup ZIP note

| | |
|--|--|
| **`Autopilot_Backup_B8.zip`** | Created **before** DEBT-B1 FileParser split |
| **`Autopilot_Backup_B8_final.zip`** | Includes DEBT-B1 split + docs; **use this as safety net** |

---

## 11. What “green” looks like on Windows without FFmpeg/TTS (B.12.1+)

```text
pytest tests -q --tb=line
# Expect:
# 272 passed, 5 skipped   (Windows)
# 273 passed, 4 skipped   (POSIX)
# Possible pydub FFmpeg RuntimeWarning
```

Skipped tests typically include real Piper/Kokoro/XTTS generation and PDF
generation without reportlab. On Windows there is one additional skip:
`test_bash_and_python_variants_behave_identically` skips itself because bash
fakes only execute on POSIX. Both totals above are valid green runs.

---

## 12. If something fails

Send back:

1. `python --version`
2. Full pytest summary line
3. Failed test node ids + first traceback lines
4. Whether FFmpeg / Piper were installed

Do **not** start B.9 until those results are reviewed if failures appear.


---

## 13. Cache filenames with colons (FIXED)

| | |
|--|--|
| **Severity** | Was **Blocker** on Windows — **FIXED** in post-B.8 hotfix |
| **Symptom** | `OSError: [Errno 22] Invalid argument` writing `kw:p:s:....json` |
| **Cause** | `CacheService._entry_path` used raw keys containing `:` |
| **Fix** | `CacheService.sanitize_filename()` replaces `<>:"/\|?*` with `_` for on-disk names only |
| **Logical keys** | Still may contain `:` (e.g. keyword analyzer); index lookup unchanged |
| **Tests** | `TestCacheWindowsFilenames` in `tests/unit/test_core_services.py` |


---

## 14. Fake ffmpeg test doubles — WinError 193 (FIXED in B.12.1)

| | |
|--|--|
| **Severity** | Was **Blocker** for B.10–B.12 Windows verification — **FIXED** |
| **Symptom** | 18 failures, all `OSError: [WinError 193] %1 is not a valid Win32 application` in `test_export_engine.py`, `test_color_grade_engine.py`, `test_subtitle_engine.py` |
| **Cause** | Fake ffmpeg/ffprobe test doubles were bash scripts; Windows `CreateProcess` cannot execute scripts directly |
| **Fix** | Cross-platform doubles in `tests/conftest.py`: file names stay `ffmpeg`/`ffprobe` everywhere (HardwareService resolution untouched); content is bash on POSIX, sentinel-marked Python on Windows; a test-only shim routes sentinel fakes through `sys.executable` in `subprocess.run`/`Popen` |
| **Why not .bat** | Batch files also fail CreateProcess with `shell=False` (same WinError 193), and cmd parsing corrupts args containing `&` (ASS colours) and `(` `)` (filter expressions) |
| **Simulation** | POSIX dev machines can exercise the exact Windows path: `AUTOPILOT_TEST_WINDOWS_FAKES=1 pytest tests` |
| **Tests** | `tests/unit/test_fake_binary_helpers.py` (10 tests; bash/python parity, env switches, special-char passthrough, shim selectivity) |
| **Production code** | Unchanged — engines were never the problem |
