# Autopilot File Inventory — Checkpoint B.8

Total files: **112**
Total size: **8,314,477 bytes**

| File path | Size (bytes) | Lines | Purpose |
|---|---:|---:|---|
| `Autopilot_Backup_B8.zip` | 1254205 | - | Checkpoint backup archive (B.8) |
| `PROJECT_STATE.md` | 776 | 26 | Live project recovery state |
| `README.md` | 1171 | 50 | Project overview |
| `assets/sfx/ambient/city_1900s.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/ambient/clock_ticking.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/ambient/forest_ambience.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/ambient/ominous_drone.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/ambient/wind_howling.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/atmospheric/church_bell.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/atmospheric/crowd_murmur.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/crowd/crowd_gasp.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/dramatic/heavy_thud.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/dramatic/low_brass_boom.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/emotional/heartbeat_fast.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/emotional/heartbeat_slow.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/historical/battlefield.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/historical/marching_footsteps.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/horror/door_creaking.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/horror/horror_piano_chord.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/horror/ominous_string_sting.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/mechanical/radio_static.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/mechanical/typewriter.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/nature/distant_thunder.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/nature/water_dripping.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/dark_whoosh_left.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/dark_whoosh_right.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/dramatic_boom.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/film_reel.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/page_turn.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/static_burst.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `assets/sfx/transitions/subtle_swoosh.wav` | 67244 | - | Placeholder SFX WAV for offline tests |
| `build.bat` | 307 | 6 | Dev/setup/run script |
| `build_manifest.json` | 602 | 28 | Phase build status manifest |
| `channel_profiles/default_profile.json` | 682 | 25 | Default channel profile JSON |
| `config/animation_presets.json` | 747 | 36 | Application/config JSON defaults |
| `config/app_settings.json` | 1184 | 43 | Application/config JSON defaults |
| `config/color_grade_presets.json` | 2729 | 96 | Application/config JSON defaults |
| `config/default_channel_profile.json` | 682 | 25 | Application/config JSON defaults |
| `config/documentary_genres.json` | 1254 | 55 | Application/config JSON defaults |
| `config/export_presets.json` | 1299 | 59 | Application/config JSON defaults |
| `config/ffmpeg_commands.json` | 592 | 28 | Application/config JSON defaults |
| `config/keyboard_shortcuts.json` | 382 | 16 | Application/config JSON defaults |
| `config/keyword_emotion_map.json` | 10979 | 621 | Emotion/SFX/animation keyword maps |
| `config/modules_config.json` | 2324 | 119 | Module enable/priority registry |
| `config/sfx_config.json` | 4509 | 149 | SFX catalog + file_map + triggers |
| `config/subtitle_style_presets.json` | 709 | 29 | Application/config JSON defaults |
| `config/transition_presets.json` | 8066 | 278 | Transition catalog with FFmpeg mappings |
| `config/voice_store_catalog.json` | 432 | 22 | Application/config JSON defaults |
| `core/__init__.py` | 151 | 5 | Core service/infrastructure module |
| `core/cache_service.py` | 9153 | 294 | Disk cache with TTL |
| `core/config_service.py` | 8227 | 252 | JSON config loader |
| `core/correlation.py` | 2080 | 77 | Render correlation IDs |
| `core/database_service.py` | 19904 | 529 | SQLite abstraction + domain DB helpers |
| `core/errors.py` | 6565 | 233 | Error hierarchy |
| `core/event_bus.py` | 3724 | 114 | Pub/sub event bus |
| `core/hardware_service.py` | 9721 | 303 | Platform/FFmpeg/RAM helpers |
| `core/log_service.py` | 4523 | 137 | Structured logging + correlation filter |
| `core/render_state_machine.py` | 8935 | 269 | 12-state render FSM |
| `core/service_container.py` | 8097 | 242 | DI container + BaseModule |
| `core/time_helper.py` | 1235 | 49 | UTC timestamp helpers |
| `database/autopilot.db` | 393216 | - | Initialized SQLite database file |
| `database/schema.sql` | 40299 | 890 | 25 product tables + migrations schema |
| `docs/IMAGE_MATCHING_PRIORITIES.md` | 526 | 15 | Developer documentation note |
| `engines/piper/models/fake_model.onnx` | 16 | - | Engine binary/model placeholder path |
| `modules/__init__.py` | 114 | 4 | Project file |
| `modules/_file_parser_monolith.py` | 51595 | 1350 | Backup monolith copy of file_parser (dev artifact) |
| `modules/audio_processor.py` | 31288 | 839 | Narration join, ducking, mix, limiter, LUFS |
| `modules/file_parser.py` | 51595 | 1350 | Parse scripts (TXT/JSON/CSV/DOCX/PDF) and match images |
| `modules/keyword_analyzer.py` | 18621 | 473 | Mood/SFX/transition/animation keyword scoring (Module 25) |
| `modules/sfx_engine.py` | 22923 | 598 | SFX library load, auto/manual placement, mix prep |
| `modules/timeline_engine.py` | 29210 | 741 | Scene timing, chapters, intro/outro, validation |
| `modules/transition_engine.py` | 18159 | 524 | FFmpeg xfade filters + smart transition selection |
| `modules/tts_engine_manager.py` | 47409 | 1259 | Lazy TTS generation, pauses, effects, install |
| `modules/tts_presets.py` | 6047 | 162 | 28 emotion presets, pause/reverb/EQ constants |
| `modules/voice_profile_manager.py` | 32147 | 783 | Auto-create/manage character voice profiles |
| `pytest.ini` | 173 | 8 | Pytest configuration |
| `quality_check.bat` | 334 | 11 | Dev/setup/run script |
| `requirements.txt` | 2732 | 89 | Python dependency list |
| `run.bat` | 144 | 7 | Dev/setup/run script |
| `scripts/quality_check.sh` | 311 | 12 | Dev/setup/run script |
| `scripts/setup.sh` | 910 | 28 | Dev/setup/run script |
| `scripts/test.sh` | 205 | 9 | Dev/setup/run script |
| `setup.bat` | 746 | 21 | Dev/setup/run script |
| `test.bat` | 117 | 4 | Dev/setup/run script |
| `tests/conftest.py` | 2303 | 86 | Project file |
| `tests/fixtures/generate_fixtures.py` | 10951 | 360 | Sample project fixture asset |
| `tests/fixtures/sample_project/audio/sample_music.wav` | 2304044 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/audio/sample_narration.wav` | 1536044 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/ancient_map_europe.jpg` | 33761 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/black_death_ships.jpg` | 33747 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/church_interior_dark.jpg` | 33756 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/dark_castle_night.jpg` | 33711 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/doctor_plague_mask.jpg` | 33784 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/europe_aftermath.jpg` | 33703 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/mass_grave_field.jpg` | 33657 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/medieval_city_street.jpg` | 33698 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/plague_victims_medieval.jpg` | 33844 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/images/rat_infestation.jpg` | 33612 | - | Sample project fixture asset |
| `tests/fixtures/sample_project/script/sample_script.csv` | 625 | 4 | Sample project fixture asset |
| `tests/fixtures/sample_project/script/sample_script.json` | 1372 | 55 | Sample project fixture asset |
| `tests/fixtures/sample_project/script/sample_script.txt` | 4530 | 189 | Sample project fixture asset |
| `tests/integration/test_pipeline_flow.py` | 10543 | 264 | B.1–B.8 integration pipeline test |
| `tests/unit/test_audio_processor.py` | 12633 | 344 | Unit tests for corresponding module |
| `tests/unit/test_core_services.py` | 7947 | 233 | Unit tests for corresponding module |
| `tests/unit/test_file_parser.py` | 7732 | 192 | Unit tests for corresponding module |
| `tests/unit/test_keyword_analyzer.py` | 12434 | 284 | Unit tests for corresponding module |
| `tests/unit/test_sfx_engine.py` | 10827 | 295 | Unit tests for corresponding module |
| `tests/unit/test_timeline_engine.py` | 13389 | 355 | Unit tests for corresponding module |
| `tests/unit/test_transition_engine.py` | 9345 | 247 | Unit tests for corresponding module |
| `tests/unit/test_tts_engine_manager.py` | 12117 | 308 | Unit tests for corresponding module |
| `tests/unit/test_voice_profile_manager.py` | 12700 | 337 | Unit tests for corresponding module |
| `ui/__init__.py` | 53 | 1 | UI package placeholder (Phase C) |
