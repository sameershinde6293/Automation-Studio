"""End-to-end integration flow for modules B.1–B.8 (no full video render)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from modules.audio_processor import AudioProcessor
from modules.file_parser import FileParser
from modules.keyword_analyzer import KeywordAnalyzer
from modules.sfx_engine import SFXEngine
from modules.timeline_engine import TimelineEngine
from modules.transition_engine import TransitionEngine
from modules.tts_engine_manager import TTSEngineManager
from modules.voice_profile_manager import VoiceProfileManager


@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    """Isolated production container for integration test."""
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=project_root,
    )


def test_pipeline_flow_b1_to_b8(
    container: ServiceContainer, project_root: Path, tmp_path: Path
) -> None:
    """Run parse → profiles → keywords → TTS → audio → SFX → timeline → transitions."""
    sample = project_root / "tests" / "fixtures" / "sample_project"
    script_path = sample / "script" / "sample_script.txt"
    images_dir = sample / "images"
    assert script_path.exists()

    # --- B.1 file_parser ---
    parser = FileParser(container)
    parsed = parser.parse_script(script_path)
    assert parsed["success"] is True, parsed.get("error")
    data = parsed["data"]
    assert len(data["scenes"]) >= 2
    assert data["voice_instructions"]

    match = parser.match_images(data["scenes"], images_dir)
    assert match["success"] is True
    assert match["data"]["total"] == len(data["scenes"])

    # --- Create project + persist scenes/lines for later modules ---
    db = container.get("database")
    project_id = db.new_id()
    assert db.create_project(
        {
            "id": project_id,
            "title": data["project_settings"].get("title") or "Integration Project",
            "project_folder_path": str(tmp_path / "project"),
            "genre": data["project_settings"].get("genre") or "dark_history",
        }
    )

    scene_ids = []
    for scene in data["scenes"]:
        scene_id = db.new_id()
        scene_ids.append(scene_id)
        assert db.save_scene(
            {
                "id": scene_id,
                "project_id": project_id,
                "scene_number": int(scene.get("scene_number") or len(scene_ids)),
                "image_filename": scene.get("image") or "",
                "transition_in": scene.get("transition_in") or "crossfade",
                "transition_out": scene.get("transition_out") or "crossfade",
                "animation_type": scene.get("animation") or "ken_burns",
            }
        )
        for index, line in enumerate(scene.get("dialogue") or [], start=1):
            db.db.execute(
                "INSERT INTO dialogue_lines "
                "(id, project_id, scene_id, line_number, character_name, emotion, "
                "text_content, pause_after, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    db.new_id(),
                    project_id,
                    scene_id,
                    index,
                    str(line.get("character") or "NARRATOR").upper(),
                    str(line.get("emotion") or "neutral"),
                    str(line.get("text") or ""),
                    str(line.get("pause_after") or "short"),
                ),
            )

    # --- B.3 voice_profile_manager ---
    vpm = VoiceProfileManager(container)
    profiles = vpm.create_profiles_from_script(data, project_id)
    assert profiles["success"] is True, profiles.get("error")
    created = profiles["data"]["profiles_created"] + profiles["data"].get(
        "profiles_updated", []
    )
    assert len(created) >= 1
    narr = vpm.load_profile(project_id, "NARRATOR")
    assert narr["success"] is True
    assert narr["data"]["profile"] is not None

    # --- B.2 keyword_analyzer ---
    kwa = KeywordAnalyzer(container)
    analyzed = kwa.analyze_all_scenes(project_id)
    assert analyzed["success"] is True, analyzed.get("error")
    assert analyzed["data"]["analyzed"] >= 1
    mood_row = db.db.fetch_one(
        "SELECT keyword_mood FROM scenes WHERE project_id = ? AND scene_number = 1",
        (project_id,),
    )
    assert mood_row is not None
    assert mood_row.get("keyword_mood")

    # --- B.4 tts_engine_manager (synthetic if engines missing) ---
    tts = TTSEngineManager(container)
    assert tts.engines_loaded_in_memory()["kokoro"] is False
    assert tts.engines_loaded_in_memory()["xtts"] is False

    profile = narr["data"]["profile"]
    char_profile = {
        "engine": profile.get("engine") or "piper",
        "voice_model": profile.get("voice_model") or "default",
        "speed": float(profile.get("speed") or 1.0),
        "pitch": float(profile.get("pitch") or 0.0),
        "volume": float(profile.get("volume") or 1.0),
        "default_emotion": profile.get("default_emotion") or "dramatic",
        "reverb_preset": profile.get("reverb_preset") or "none",
        "eq_preset": profile.get("eq_preset") or "flat",
        "breathing_enabled": bool(profile.get("breathing_enabled")),
    }

    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    line_paths = []
    # Limit to first 3 lines for speed while still proving multi-line flow
    line_rows = db.db.fetch_all(
        "SELECT * FROM dialogue_lines WHERE project_id = ? "
        "ORDER BY scene_id ASC, line_number ASC LIMIT 3",
        (project_id,),
    )
    assert line_rows
    for index, row in enumerate(line_rows):
        out = audio_dir / f"line_{index:03d}.wav"
        gen = tts.generate_audio(str(row["text_content"]), char_profile, out)
        assert gen["success"] is True, gen.get("error")
        assert Path(gen["data"]["audio_path"]).exists()
        duration = float(gen["data"].get("duration") or 0.5)
        db.db.execute(
            "UPDATE dialogue_lines SET audio_generated = 1, audio_file_path = ?, "
            "audio_duration = ?, word_timestamps_json = ?, status = 'completed' "
            "WHERE id = ?",
            (
                str(out),
                duration,
                __import__("json").dumps(gen["data"].get("word_timestamps") or []),
                row["id"],
            ),
        )
        line_paths.append(str(out))

    # Still lazy after generation of synthetic/piper path
    assert tts.engines_loaded_in_memory()["xtts"] is False

    # --- B.5 audio_processor ---
    ap = AudioProcessor(container)
    narration_path = audio_dir / "narration.wav"
    built = ap.build_narration_track(
        project_id, line_paths, narration_path, pause_seconds=0.25
    )
    assert built["success"] is True, built.get("error")
    assert Path(built["data"]["audio_path"]).exists()
    assert built["data"]["duration"] > 0

    music_path = audio_dir / "music.wav"
    # simple tone music matching narration length
    import math
    import struct
    import wave

    sr = 48000
    n = int(max(2.0, built["data"]["duration"]) * sr)
    with wave.open(str(music_path), "w") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            val = int(4000 * math.sin(2 * math.pi * 110 * i / sr))
            frames += struct.pack("<hh", val, val)
        handle.writeframes(bytes(frames))

    final_mix = audio_dir / "final_mix.wav"
    mixed = ap.generate_final_mix(
        project_id,
        final_mix,
        {"narration_path": str(narration_path), "music_path": str(music_path)},
    )
    assert mixed["success"] is True, mixed.get("error")
    assert Path(mixed["data"]["audio_path"]).exists()
    assert mixed["data"]["peak_db"] <= 0.0

    # --- B.6 sfx_engine ---
    sfx = SFXEngine(container)
    lib = sfx.load_sfx_library()
    assert lib["success"] is True
    assert lib["data"]["present"] >= 1
    placed = sfx.auto_place_sfx(project_id)
    assert placed["success"] is True, placed.get("error")
    # At least try manual if auto found none (still valid)
    if placed["data"]["count"] == 0:
        manual = sfx.place_sfx_manually(project_id, scene_ids[0], "dramatic_boom", 1.0)
        assert manual["success"] is True, manual.get("error")
    prep = sfx.prepare_sfx_for_mixing(project_id)
    assert prep["success"] is True
    assert prep["data"]["count"] >= 1

    # --- B.7 timeline_engine ---
    tl = TimelineEngine(container)
    timeline_resp = tl.build_timeline(
        project_id,
        narration_path=str(narration_path),
        intro_config={"enabled": True, "duration": 3.0},
        outro_config={"enabled": True, "duration": 3.0},
        save=True,
    )
    assert timeline_resp["success"] is True, timeline_resp.get("error")
    timeline = timeline_resp["data"]["timeline"]
    assert len(timeline["scenes"]) >= 2
    assert timeline["total_duration"] > 0
    assert timeline["intro"] is not None
    assert timeline["outro"] is not None
    assert timeline["youtube_chapters_text"]
    row = db.db.fetch_one(
        "SELECT timeline_json FROM timeline_data WHERE project_id = ?",
        (project_id,),
    )
    assert row is not None and row.get("timeline_json")

    # --- B.8 transition_engine ---
    tr = TransitionEngine(container)
    batch = tr.generate_batch_filters(timeline, use_smart=True)
    assert batch["success"] is True, batch.get("error")
    expected_transitions = max(0, len(timeline["scenes"]) - 1)
    assert batch["data"]["count"] == expected_transitions
    for filt in batch["data"]["filters"]:
        if not filt.get("is_hard_cut"):
            assert filt.get("filter_string")
            assert "xfade=transition=" in filt["filter_string"]

    # Final data-flow assertions
    assert db.db.get_table_row_count("voice_profiles") >= 1
    assert db.db.get_table_row_count("dialogue_lines") >= 1
    assert db.db.get_table_row_count("sfx_placements") >= 1
    assert Path(final_mix).stat().st_size > 1000
