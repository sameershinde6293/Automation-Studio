"""Headless pins for the 1.0 SHIP features from ui_specification.txt.

Splash (logo/bar/version), License Screen (HWID display + gating),
5 import zones, fullscreen + inspector actions, window memory,
icons-everywhere — every decision lives in UiViewModel so it is
verifiable without importing PyQt6.
"""

from __future__ import annotations

from pathlib import Path

from ui.viewmodel import (
    ACTION_DEFS,
    ACTION_ICONS,
    DEFAULT_SHORTCUTS,
    FALLBACK_TRANSITIONS,
    MENU_LAYOUT,
    SUBTITLE_FONTS,
    VOICE_EMOTIONS,
    WORKSPACES,
    UiViewModel,
)


def _vm(**ctx) -> UiViewModel:
    return UiViewModel(ctx)


class _Cfg:
    """In-memory config stub (no real files touched)."""

    def __init__(self) -> None:
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _Container:
    def __init__(self) -> None:
        self._cfg = _Cfg()

    def get(self, name):
        if name == "config":
            return self._cfg
        raise KeyError(name)


# ------------------------------------------------------------------
# §1 Splash: logo + version + steps (the bar fill is a shell formula
# (index + 1) / len(steps) — pinned indirectly via the model here)
# ------------------------------------------------------------------
def test_splash_model_has_logo_version_and_steps() -> None:
    model = _vm().splash_model()
    assert model["logo"] == "▶"
    assert model["version"] == "3.1.0"
    assert model["title"] == "AUTOPILOT"
    assert model["subtitle"]
    assert len(model["steps"]) >= 5


# ------------------------------------------------------------------
# §2 License Screen: HWID visible, gated on missing license only
# ------------------------------------------------------------------
class _LicManager:
    def generate_hwid(self) -> str:
        return "HWID-1234-ABCD"


def test_license_screen_model_shows_hwid() -> None:
    vm = UiViewModel({
        "license": _LicManager(),
        "license_data": {"status": {"status": "active"}},
    })
    model = vm.license_screen_model()
    assert model["hwid"] == "HWID-1234-ABCD"
    assert model["status"] == "active"
    assert vm.license_screen_needed() is False


def test_license_screen_needed_without_valid_license() -> None:
    vm = _vm(license_data={"status": {"status": "expired"}})
    assert vm.license_screen_needed() is True
    # manager absent -> HWID falls back to empty, never raises
    assert vm.license_screen_model()["hwid"] == ""


def test_license_screen_skipped_for_trial_and_active() -> None:
    for state in ("trial", "active"):
        vm = _vm(license_data={"status": {"status": state}})
        assert vm.license_screen_needed() is False


# ------------------------------------------------------------------
# §Import: exactly 5 drop zones (music + voice share the audio kind)
# ------------------------------------------------------------------
def test_five_import_zones_in_spec_order() -> None:
    zones = _vm().import_zones()
    assert len(zones) == 5
    titles = " | ".join(z["title"] for z in zones)
    for word in ("Script", "Images", "Music", "Voice", "Video"):
        assert word in titles
    assert zones[-1]["kind"] == "video"


# ------------------------------------------------------------------
# §View: fullscreen + inspector menu actions, F11 shortcut
# ------------------------------------------------------------------
def test_fullscreen_and_inspector_actions_registered() -> None:
    flat = {a for _m, _t, ids in MENU_LAYOUT for a in ids if a != "|"}
    assert "toggle_fullscreen" in flat
    assert "toggle_inspector" in flat
    view = next(m for m in MENU_LAYOUT if m[0] == "view")
    assert "toggle_fullscreen" in view[2]
    assert DEFAULT_SHORTCUTS["toggle_fullscreen"] == "F11"


# ------------------------------------------------------------------
# §Window memory: geometry/state round-trip, safe without services
# ------------------------------------------------------------------
def test_window_state_roundtrip() -> None:
    vm = UiViewModel({"container": _Container()})
    assert vm.window_state_load() == {"geometry": "", "state": ""}
    vm.window_state_save("R0VPTQ==", "U1RBVEU=")
    assert vm.window_state_load() == {
        "geometry": "R0VPTQ==",
        "state": "U1RBVEU=",
    }


def test_window_state_safe_without_container() -> None:
    vm = _vm()
    vm.window_state_save("x", "y")  # must not raise
    assert vm.window_state_load() == {"geometry": "", "state": ""}


# ------------------------------------------------------------------
# §Visual: icons everywhere — every menu + toolbar row carries one
# ------------------------------------------------------------------
def test_every_action_def_has_an_icon() -> None:
    ids = {a["id"] for a in ACTION_DEFS}
    missing = sorted(ids - set(ACTION_ICONS))
    assert missing == []


def test_toolbar_and_menu_models_carry_icons() -> None:
    vm = _vm()
    for row in vm.toolbar_model():
        assert row["icon"], f"toolbar action {row['id']} has no icon"
    items = [
        i for m in vm.menu_model() for i in m["items"]
        if not i.get("separator") and not i.get("submenu")
    ]
    children = [
        c for m in vm.menu_model() for i in m["items"]
        if i.get("submenu") for c in i["items"]
    ]
    assert all(i.get("icon") for i in items)
    assert all(c.get("icon") for c in children)  # submenu items too
    assert len(items) + len(children) >= 30  # full §5 menu bar


# ------------------------------------------------------------------
# Deep-dive fixes (round 2): status bar, inspector stats, toolbar
# completion, shortcut coverage, setup wizard entry point
# ------------------------------------------------------------------
def test_system_status_model_keys_and_honesty() -> None:
    model = _vm().system_status_model()
    assert set(model) == {"ram", "cpu", "ffmpeg"}
    assert model["ram"].startswith("RAM")
    assert model["cpu"].startswith("CPU")
    assert model["ffmpeg"].startswith("FFmpeg:")


def test_inspector_stats_model_has_app_facts() -> None:
    lines = _vm().inspector_stats_model()
    text = "\n".join(lines)
    assert "Autopilot 3.1.0" in text
    assert "License:" in text
    assert "FFmpeg:" in text
    assert len(lines) >= 8


def test_toolbar_completed_with_spec_buttons() -> None:
    ids = [row["id"] for row in _vm().toolbar_model()]
    for wanted in ("new_project", "import_files", "start_render",
                   "cancel_render", "pause_render", "batch_render",
                   "open_settings", "user_guide"):
        assert wanted in ids


def test_pause_render_disabled_but_visible_with_reason() -> None:
    pause = next(a for a in ACTION_DEFS if a["id"] == "pause_render")
    assert pause["toolbar"] is True
    assert pause["enabled"] is False
    assert "not supported" in pause["reason"]


def test_edit_and_tools_shortcuts_present() -> None:
    keys = DEFAULT_SHORTCUTS
    assert keys["copy_scene"] == "Ctrl+C"
    assert keys["paste_scene"] == "Ctrl+V"
    assert keys["delete_scene"] == "Delete"
    assert keys["batch_render"] == "Ctrl+Shift+B"
    assert keys["user_guide"] == "F1"


def test_setup_wizard_registered_in_tools_menu() -> None:
    tools = next(m for m in MENU_LAYOUT if m[0] == "tools")
    assert "setup_wizard" in tools[2]
    assert "setup_wizard" in ACTION_ICONS


def test_shortcuts_config_file_covers_new_keys() -> None:
    import json
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2]
            / "config" / "keyboard_shortcuts.json")
    keys = json.loads(path.read_text(encoding="utf-8"))["shortcuts"]
    for wanted in ("copy_scene", "paste_scene", "delete_scene",
                   "batch_render", "user_guide", "toggle_fullscreen"):
        assert wanted in keys


# ------------------------------------------------------------------
# Round 3: audio fades (21) + partial-workflow exports (35-39)
# ------------------------------------------------------------------
def test_audio_fade_defaults_and_keys() -> None:
    model = _vm().audio_settings("p1")
    assert model["fade_in_seconds"] == 1.5
    assert model["fade_out_seconds"] == 2.0


def test_export_menu_model_and_file_submenu() -> None:
    vm = _vm()
    ids = {m["id"] for m in vm.export_menu_model()}
    assert ids == {
        "export_audio_only", "export_audio_mix", "burn_subtitles",
        "export_thumbnails", "export_storyboard_pdf"}
    file_menu = next(m for m in vm.menu_model() if m["menu"] == "file")
    subs = [i for i in file_menu["items"] if i.get("submenu")]
    assert len(subs) == 1 and subs[0]["submenu"] == "&Export"
    assert {c["id"] for c in subs[0]["items"]} == ids


def test_run_ffmpeg_honest_without_binary() -> None:
    vm = _vm()
    vm.ffmpeg_path = lambda: None
    ok, message, payload = vm.run_ffmpeg(["-version"])
    assert ok is False
    assert "FFmpeg not found" in message
    assert payload["cmd"] == ""


def test_export_audio_mix_command(tmp_path) -> None:
    narration = tmp_path / "n.wav"
    narration.write_bytes(b"RIFFfake")
    music = tmp_path / "m.wav"
    music.write_bytes(b"RIFFfake")
    vm = _vm()
    vm.ffmpeg_path = lambda: "/usr/bin/ffmpeg"
    seen = {}

    def runner(cmd):
        seen["cmd"] = cmd
        return 0, "ok"

    ok, _message, payload = vm.export_audio_mix(
        str(narration), str(music), "", str(tmp_path / "mix.wav"),
        runner=runner)
    assert ok is True
    assert seen["cmd"][0] == "/usr/bin/ffmpeg"  # exe first (RULE 4)
    assert "amix=inputs=2" in payload["cmd"]
    assert "volume=0.35" in payload["cmd"]
    assert "pcm_s16le" in payload["cmd"]


def test_export_audio_mix_mp3_codec(tmp_path) -> None:
    narration = tmp_path / "n.wav"
    narration.write_bytes(b"RIFFfake")
    vm = _vm()
    vm.ffmpeg_path = lambda: "ff"
    ok, _msg, payload = vm.export_audio_mix(
        str(narration), "", "", str(tmp_path / "mix.mp3"),
        runner=lambda _c: (0, ""))
    assert ok and "libmp3lame" in payload["cmd"]


def test_burn_subtitles_escapes_and_copies_audio(tmp_path) -> None:
    video = tmp_path / "in.mp4"
    video.write_bytes(b"00")
    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8")
    vm = _vm()
    vm.ffmpeg_path = lambda: "C:/tools/ffmpeg.exe"
    ok, _msg, payload = vm.burn_subtitles(
        str(video), str(srt), str(tmp_path / "out.mp4"),
        runner=lambda _c: (0, ""))
    assert ok is True
    assert "subtitles=" in payload["cmd"]
    assert "-c:a copy" in payload["cmd"]


def test_burn_subtitles_missing_inputs(tmp_path) -> None:
    vm = _vm()
    ok, message, _p = vm.burn_subtitles(
        str(tmp_path / "no.mp4"), str(tmp_path / "no.srt"), "out.mp4")
    assert ok is False and "not found" in message


def test_audio_only_honest_without_tts_seam() -> None:
    ok, message, payload = _vm().export_audio_only(
        "Once upon a time.", "/tmp/narr.wav")
    assert ok is False
    assert "full render" in message
    assert payload["available"] is False


def test_audio_only_with_fake_tts_module(tmp_path) -> None:
    class _TTS:
        def synthesize_text(self, text, output_path):
            assert text and output_path
            return {"message": "spoken"}

    class _Engine:
        def module(self, name):
            return _TTS() if name == "tts_engine_manager" else None

    vm = _vm(engine=_Engine())
    out = tmp_path / "narr.wav"
    ok, message, _p = vm.export_audio_only("hi there", str(out))
    assert ok is True and message == "spoken"


def test_thumbnail_jobs_graceful_without_scenes() -> None:
    ok, message, _p = _vm().export_thumbnail_jobs("p1", "/tmp/thumbs")
    assert ok is False and "No scenes" in message


def test_storyboard_pdf_writer_paginates(tmp_path) -> None:
    from ui.viewmodel import write_storyboard_pdf

    scenes = [
        {"number": i, "title": f"Scene {i}", "duration": 3.0,
         "chapter": "Intro" if i == 1 else "", "thumb_path": ""}
        for i in range(1, 8)
    ]
    out = tmp_path / "board.pdf"
    ok, message = write_storyboard_pdf(str(out), "Demo Doc", scenes)
    assert ok is True and "2 page(s)" in message
    data = out.read_bytes()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    assert b"/Count 2" in data  # 7 scenes, 5 per page


def test_storyboard_pdf_embeds_jpeg_thumb(tmp_path) -> None:
    from ui.viewmodel import write_storyboard_pdf

    jpg = tmp_path / "thumb.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9")
    scenes = [{"number": 1, "title": "One", "duration": 2.0,
               "chapter": "", "thumb_path": str(jpg)}]
    out = tmp_path / "board.pdf"
    ok, _msg = write_storyboard_pdf(str(out), "T", scenes)
    assert ok is True
    assert b"/DCTDecode" in out.read_bytes()


def test_storyboard_export_without_project() -> None:
    ok, message = _vm().export_storyboard_pdf("nope", "/tmp/x.pdf")
    assert ok is False and "No scenes" in message


def test_export_actions_in_action_defs_and_icons() -> None:
    ids = {a["id"] for a in ACTION_DEFS}
    for wanted in ("export_audio_only", "export_audio_mix",
                   "burn_subtitles", "export_thumbnails",
                   "export_storyboard_pdf"):
        assert wanted in ids
        assert ACTION_ICONS[wanted]


# ------------------------------------------------------------------
# Round 4: v3.0 master spec — control panels, workspaces, waveform
# ------------------------------------------------------------------
class _StubTTSModule:
    """TTS seam that actually 'speaks' the preview file."""

    def __init__(self) -> None:
        self.calls = []

    def synthesize_text(self, text, output_path, **params):
        self.calls.append({"text": text, "params": params})
        Path(str(output_path)).write_bytes(b"RIFFpreview")
        return {"message": f"spoken -> {output_path}"}


class _StubEngine:
    def __init__(self, module) -> None:
        self._module = module

    def module(self, name):
        assert name == "tts_engine_manager"
        return self._module


def test_v3_voice_controls_defaults() -> None:
    model = _vm().voice_controls_model()
    assert model["voice_speed"] == 1.0
    assert model["speed_percent"] == 100
    assert model["voice_emotion"] == "Neutral"
    assert model["voice_reverb"] == "Off"
    assert model["engines"][0] == "auto"
    assert model["presets"] == []
    assert model["voice_pause_comma_ms"] == 250


def test_v3_voice_controls_roundtrip() -> None:
    vm = _vm(container=_Container())
    ok, message = vm.save_voice_controls({
        "voice_engine": "auto", "voice_name": "Marcus",
        "voice_speed": 1.25, "voice_pitch_st": -2,
        "voice_volume": 80, "voice_emotion": "Happy",
        "voice_reverb": "Large Hall", "voice_reverb_amount": 60,
        "voice_breathing": True, "voice_breath_volume": 45,
        "voice_pause_comma_ms": 300, "voice_pause_sentence_ms": 600,
        "voice_pause_paragraph_ms": 1000,
        "voice_pause_chapter_ms": 1500,
        "voice_pronunciation": "D:/dict.txt", "voice_lock": True,
    })
    assert ok, message
    model = vm.voice_controls_model()
    assert model["voice_speed"] == 1.25
    assert model["speed_percent"] == 125
    assert model["voice_name"] == "Marcus"
    assert model["voice_pitch_st"] == -2
    assert model["voice_emotion"] == "Happy"
    assert model["voice_reverb"] == "Large Hall"
    assert model["voice_pause_chapter_ms"] == 1500
    assert model["voice_lock"] is True


def test_v3_voice_controls_validation_and_no_store() -> None:
    vm = _vm(container=_Container())
    ok, message = vm.save_voice_controls({"voice_speed": 2.5})
    assert ok is False and "Speed" in message
    ok, message = vm.save_voice_controls(
        {"voice_speed": 1.0, "voice_emotion": "Furious"})
    assert ok is False and "emotion" in message.lower()
    ok, message = _vm().save_voice_controls({"voice_speed": 1.0})
    assert ok is False and "unavailable" in message.lower()


def test_v3_voice_presets_full_cycle() -> None:
    vm = _vm(container=_Container())
    vm.save_voice_controls(
        {"voice_speed": 1.1, "voice_emotion": "Calm"})
    ok, message = vm.save_voice_preset("", {})
    assert ok is False and "Name" in message
    ok, message = vm.save_voice_preset("Warm Doc")
    assert ok, message
    assert vm.voice_controls_model()["presets"] == ["Warm Doc"]
    ok, _m, state = vm.apply_voice_preset("Warm Doc")
    assert ok and state["voice_speed"] == 1.1
    assert state["voice_emotion"] == "Calm"
    ok, message = vm.delete_voice_preset("Warm Doc")
    assert ok, message
    ok, message, _s = vm.apply_voice_preset("Warm Doc")
    assert ok is False and "No preset" in message


def test_v3_preview_voice_honest_without_seam() -> None:
    ok, message, payload = _vm(container=_Container()
                               ).preview_voice("Hello preview.")
    assert ok is False
    assert "preview" in message.lower()
    assert payload["available"] is False


def test_v3_preview_voice_with_stub_tts() -> None:
    module = _StubTTSModule()
    vm = _vm(container=_Container(), engine=_StubEngine(module))
    ok, message, payload = vm.preview_voice("This is a test line.")
    assert ok, message
    assert "spoken ->" in message
    assert Path(payload["path"]).read_bytes() == b"RIFFpreview"
    assert module.calls[0]["text"] == "This is a test line."
    assert module.calls[0]["params"]["speed"] == 1.0


def test_v3_transitions_model_and_fallbacks() -> None:
    model = _vm().transitions_model("p1")
    assert model["found"] is False
    assert model["scenes"] == []
    assert len(model["types"]) == len(FALLBACK_TRANSITIONS) >= 10
    assert model["types"][0]["id"] == "fade"
    assert model["empty_text"]


def test_v3_apply_transition_validation() -> None:
    vm = _vm()
    ok, message = vm.apply_transition("p1", [], "teleport")
    assert ok is False and "Unknown transition" in message
    ok, message = vm.apply_transition("p1", [], "fade")
    assert ok is False and "Select at least one" in message
    ok, message = vm.apply_transition("p1", [], "fade",
                                      apply_all=True)
    assert ok is False and "No scenes" in message
    ok, message = vm.apply_transition("p1", [1], "fade", 0.5)
    assert ok is False and "unavailable" in message.lower()


def test_v3_scene_controls_model_and_actions() -> None:
    vm = _vm()
    model = vm.scene_controls_model("p1")
    assert model["found"] is False
    assert model["scenes"] == []
    assert "ken_burns" in model["animations"]
    assert model["intensities"]
    ok, message = vm.apply_scene_animation_all(
        "p1", "ken_burns", "medium")
    assert ok is False and "No scenes" in message
    ok, message = vm.apply_scene_duration("p1", None, 4.0)
    assert ok is False and "per scene" in message
    ok, message = vm.apply_scene_duration("p1", 2, 4.0)
    assert ok is False and "unavailable" in message.lower()


def test_v3_export_settings_defaults_and_summary() -> None:
    vm = _vm()
    model = vm.export_settings_model()
    assert model["export_resolution"] == "1920x1080"
    assert model["export_fps"] == "30"
    assert model["export_codec"] == "libx264"
    assert model["summary_text"] == (
        "1080p · 30 fps · H.264 · CRF 20 · AAC 192k")
    assert len(model["codecs"]) == 4
    assert "libvpx-vp9" in [c["id"] for c in model["codecs"]]


def test_v3_export_settings_roundtrip_and_validation() -> None:
    vm = _vm(container=_Container())
    state = vm.export_settings_model()
    state.update({
        "export_resolution": "3840x2160",
        "export_fps": "60",
        "export_codec": "libx265",
        "export_crf": 24,
        "export_folder": "D:/exports",
    })
    ok, message = vm.save_export_settings(state)
    assert ok, message
    model = vm.export_settings_model()
    assert model["export_fps"] == "60"
    assert "4K" in model["summary_text"]
    assert "H.265" in model["summary_text"]
    bad = vm.export_settings_model()
    bad["export_fps"] = "29"
    ok, message = vm.save_export_settings(bad)
    assert ok is False and "FPS" in message
    bad2 = vm.export_settings_model()
    bad2["export_codec"] = "prores"
    ok, message = vm.save_export_settings(bad2)
    assert ok is False and "codec" in message.lower()
    ok, message = _vm().save_export_settings({})
    assert ok is False and "unavailable" in message.lower()


def test_v3_subtitle_style_defaults_and_force_style() -> None:
    vm = _vm()
    model = vm.subtitle_style_model()
    assert model["subtitle_font"] == "Montserrat"
    assert model["subtitle_size"] == 54
    assert model["subtitle_weight"] == "Bold"
    assert model["subtitle_position"] == "Bottom"
    style = vm.subtitle_force_style()
    assert "FontName=Montserrat" in style
    assert "FontSize=54" in style
    assert "PrimaryColour=&H00FFFFFF" in style
    assert "Bold=1" in style
    assert "Alignment=2" in style
    assert "MarginV=40" in style


def test_v3_subtitle_style_roundtrip_and_position() -> None:
    vm = _vm(container=_Container())
    state = vm.subtitle_style_model()
    state.update({
        "subtitle_font": "Arial",
        "subtitle_position": "Top",
        "subtitle_background": True,
        "subtitle_color": "#FF0000",
    })
    ok, message = vm.save_subtitle_style(state)
    assert ok, message
    model = vm.subtitle_style_model()
    assert model["subtitle_position"] == "Top"
    style = vm.subtitle_force_style()
    assert "FontName=Arial" in style
    assert "Alignment=8" in style
    assert "BorderStyle=3" in style
    bad = vm.subtitle_style_model()
    bad["subtitle_color"] = "red"
    ok, message = vm.save_subtitle_style(bad)
    assert ok is False and "#RRGGBB" in message
    ok, message = _vm().save_subtitle_style({})
    assert ok is False and "unavailable" in message.lower()


def test_v3_ass_colour_math() -> None:
    vm = _vm()
    assert vm._ass_colour("#FFFFFF") == "&H00FFFFFF"
    assert vm._ass_colour("#FF0000") == "&H000000FF"
    assert vm._ass_colour("#000000", 50) == "&H80000000"
    assert vm._ass_colour("junk") == "&H00FFFFFF"


def test_v3_burn_subtitles_uses_style_and_export_profile(
    tmp_path,
) -> None:
    video = tmp_path / "in.mp4"
    video.write_bytes(b"00")
    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8")
    vm = _vm(container=_Container())
    export = vm.export_settings_model()
    export["export_codec"] = "libx265"
    export["export_crf"] = 24
    export["export_preset"] = "slow"
    ok, _msg = vm.save_export_settings(export)
    assert ok
    ok, _msg = vm.save_subtitle_style(vm.subtitle_style_model())
    assert ok
    vm.ffmpeg_path = lambda: "C:/tools/ffmpeg.exe"
    ok, _msg, payload = vm.burn_subtitles(
        str(video), str(srt), str(tmp_path / "out.mp4"),
        runner=lambda _c: (0, ""))
    assert ok is True
    assert "force_style=" in payload["cmd"]
    assert "FontName=Montserrat" in payload["cmd"]
    assert "-c:v libx265" in payload["cmd"]
    assert "-crf 24" in payload["cmd"]
    assert "-preset slow" in payload["cmd"]
    assert "-c:a copy" in payload["cmd"]


def _write_sine_wav(path, freq: float = 440.0,
                    seconds: float = 0.4, rate: int = 8000) -> None:
    import math
    import struct
    import wave

    frames = bytearray()
    for index in range(int(rate * seconds)):
        sample = int(16000 * math.sin(
            2 * math.pi * freq * index / rate))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))


def test_v3_waveform_peaks_from_wav(tmp_path) -> None:
    target = tmp_path / "tone.wav"
    _write_sine_wav(target)
    ok, message, peaks = _vm().waveform_peaks(
        str(target), buckets=40)
    assert ok, message
    assert len(peaks) == 40
    assert all(0.0 <= p <= 1.0 for p in peaks)
    assert 0.40 <= max(peaks) <= 0.55  # 16000/32768 amplitude


def test_v3_waveform_peaks_missing_and_empty(tmp_path) -> None:
    import wave

    vm = _vm()
    ok, message, peaks = vm.waveform_peaks(str(tmp_path / "no.wav"))
    assert ok is False and "not found" in message and peaks == []
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"")
    ok, message, peaks = vm.waveform_peaks(str(empty))
    assert ok is False and "empty" in message.lower() and peaks == []


def test_v3_workspaces_model_and_switch() -> None:
    vm = _vm(container=_Container())
    model = vm.workspace_model()
    assert model["current"] == "Writing"
    assert len(model["names"]) >= 8
    ok, message = vm.set_workspace("Editing")
    assert ok, message
    assert vm.workspace_model()["current"] == "Editing"
    layout = vm.workspace_layout("Editing")
    assert layout["page"] == "studio"
    assert layout["inspector"] is True
    ok, message = vm.set_workspace("Hollywood")
    assert ok is False and "Unknown workspace" in message
    ok, message = _vm().set_workspace("Editing")
    assert ok is False and "unavailable" in message.lower()


class _DbStub:
    """Minimal projects-row stub so audio_settings() finds a row."""

    def fetch_one(self, *_args):
        return {"narration_volume": None, "music_volume": None,
                "sfx_volume": None, "music_file_path": ""}


class _DbHolder:
    db = _DbStub()


class _DbContainer(_Container):
    def get(self, name):
        if name == "database":
            return _DbHolder()
        return super().get(name)


def test_v3_audio_mutes_roundtrip_without_db() -> None:
    vm = _vm(container=_DbContainer())
    model = vm.audio_settings("p1")
    assert model["found"] is True
    assert model["mute_narration"] is False
    assert model["mute_music"] is False
    assert model["mute_sfx"] is False
    vm.container.get("config").set("mute_music", True)
    assert vm.audio_settings("p1")["mute_music"] is True


def test_v3_constants_shape() -> None:
    assert len(WORKSPACES) >= 8
    assert "Editing" in WORKSPACES
    assert "Whisper" in VOICE_EMOTIONS
    assert len(FALLBACK_TRANSITIONS) >= 10
    assert "Montserrat" in SUBTITLE_FONTS


# ------------------------------------------------------------------
# Round 5: review fixes (3.0.1)
# ------------------------------------------------------------------
def test_review_engine_indicators_not_raw_paths() -> None:
    lines = _vm().inspector_stats_model()
    ffmpeg = next(l for l in lines if l.startswith("FFmpeg:"))
    piper = next(l for l in lines if l.startswith("Piper TTS:"))
    # container-less vm -> honest "not found" indicators, no paths
    assert ffmpeg == "FFmpeg: ✗ not found"
    assert piper == "Piper TTS: ✗ not found"


def test_review_master_volume_model_and_overlay() -> None:
    vm = _vm(container=_DbContainer())
    assert vm.audio_settings("p1")["master_volume"] == 1.0
    vm.container.get("config").set("master_volume", 1.34)
    assert vm.audio_settings("p1")["master_volume"] == 1.34


def test_review_key_generator_text_is_customer_friendly() -> None:
    row = next(a for a in ACTION_DEFS if a["id"] == "key_generator")
    assert row["text"] == "My &License / Machine ID…"
    for action in ACTION_DEFS:
        assert "Admin Key Generator" not in action["text"]


class _AnyEngine:
    """Engine stub returning its module for any name."""

    def __init__(self, module) -> None:
        self._module = module

    def module(self, _name):
        return self._module


class _CatalogModule:
    def list_voices(self):
        return {"success": True, "data": {"voices": [
            {"voice_id": "v-it", "name": "Ava", "description": "d",
             "style": "calm", "gender": "F", "language": "it",
             "engine": "piper", "installed": True,
             "quality_rating": 5},
            {"voice_id": "v-ja", "name": "Ren", "description": "d",
             "style": "bright", "gender": "M", "language": "ja",
             "engine": "piper", "installed": True,
             "quality_rating": 4},
        ]}}


def test_review_voice_languages_are_catalog_driven() -> None:
    assert _vm().voice_languages() == []  # honest: no catalog, no list
    vm = _vm(engine=_AnyEngine(_CatalogModule()))
    assert vm.voice_languages() == ["it", "ja"]


# ---------------------------------------------------------------------------
# 3.0.4 expert-review round 4: pre-render match report, render warnings
# detail, and the waveform duration seam (revert buttons are Qt-side and
# pinned in test_ui_app_qt.py).
# ---------------------------------------------------------------------------
def test_review_pre_render_match_report_classifies(tmp_path) -> None:
    (tmp_path / "sunset_beach.jpg").write_bytes(b"x")
    (tmp_path / "old_factory.png").write_bytes(b"x")
    (tmp_path / "city_night.jpg").write_bytes(b"x")
    scenes = [
        {"scene_number": 1, "scene_title": "sunset beach"},
        {"scene_number": 2, "scene_title": "sunsets on the beach"},
        {"scene_number": 3, "scene_title": "distant nebula"},
    ]
    model = _vm().pre_render_match_report(
        images_folder=str(tmp_path), scenes=scenes)
    by_scene = {row["scene"]: row for row in model["rows"]}
    assert by_scene[1]["status"] == "exact"
    assert by_scene[1]["image"] == "sunset_beach.jpg"
    assert by_scene[2]["status"] == "fuzzy"
    assert by_scene[3]["status"] == "no_match"
    assert by_scene[3]["image"] == ""
    assert model["summary"] == {"exact": 1, "fuzzy": 1, "no_match": 1}
    assert "1 exact matches" in model["summary_text"]
    assert "1 fuzzy matches" in model["summary_text"]
    assert "1 unmatched" in model["summary_text"]
    assert model["images"] == 3
    assert model["matcher"] in {"engine", "local"}


def test_review_pre_render_match_report_honest_empty(tmp_path) -> None:
    model = _vm().pre_render_match_report(
        images_folder=str(tmp_path), scenes=[])
    assert model["available"] is False
    assert model["rows"] == []
    assert "No scenes" in model["note"]
    missing = _vm().pre_render_match_report(
        images_folder=str(tmp_path / "missing"),
        scenes=[{"scene_number": 1, "scene_title": "anything"}])
    assert missing["images"] == 0
    assert missing["rows"][0]["status"] == "no_match"
    assert missing["summary"]["no_match"] == 1


def test_review_render_warnings_list_roundtrip() -> None:
    vm = _vm()
    ok, _msg, warnings = vm.render_warnings_list()
    assert ok and warnings == []
    result = {"data": {"output_file_path": "", "warnings": [
        "Scene 12: image not found, using placeholder",
        "Scene 18: low-confidence image match"]}}
    vm.render_complete_model(result)
    ok, message, warnings = vm.render_warnings_list()
    assert ok and len(warnings) == 2
    assert "2 warning(s)" in message
    assert warnings[0] == "Scene 12: image not found, using placeholder"
    assert "placeholder" in warnings[0]


def test_review_audio_file_duration(tmp_path) -> None:
    import struct
    import wave

    target = tmp_path / "mix.wav"
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<4000h", *([0] * 4000)))
    ok, _msg, seconds = _vm().audio_file_duration(str(target))
    assert ok and abs(seconds - 0.5) < 0.01
    ok, message, seconds = _vm().audio_file_duration(
        str(tmp_path / "nope.wav"))
    assert not ok and seconds == 0.0 and "not found" in message
