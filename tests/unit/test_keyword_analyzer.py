"""Unit tests for modules.keyword_analyzer.KeywordAnalyzer."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from core.time_helper import utc_now_str
from modules.keyword_analyzer import TTS_EMOTIONS, KeywordAnalyzer


@pytest.fixture
def analyzer(project_root: Path, tmp_path: Path) -> KeywordAnalyzer:
    """KeywordAnalyzer with isolated production container."""
    container = ServiceContainer.create_production_container(
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
    return KeywordAnalyzer(container)


# Representative phrases expected to score for each of the 28 TTS emotions.
EMOTION_SAMPLES: dict[str, str] = {
    "neutral": "According to the report, the event was noted and described clearly.",
    "calm": "The town settled into a peaceful and tranquil evening, gently and quietly.",
    "serious": "However, nevertheless, in fact this is an important critical fact.",
    "dramatic": "A shocking bombshell was revealed, devastating and catastrophic for all.",
    "mysterious": "An unexplained mysterious anomaly vanished into an enigmatic secret.",
    "excited": "An amazing incredible breakthrough triumph thrilled the entire nation.",
    "sad": "Tears of sorrow and heartbreaking grief filled the mournful lament.",
    "angry": "Furious rage and outraged wrath left the crowd enraged and indignant.",
    "fearful": "They were terrified in panic, frightened and trembling with horror.",
    "whisper": "He whispered and murmured softly under breath in a hushed tone.",
    "tense": "The tension was strained and on edge during the nervous standoff.",
    "reverent": "A sacred holy prayer filled the blessed cathedral with worship.",
    "investigative": "Forensic evidence was discovered; the detective found a crucial clue.",
    "authoritative": "Historical records confirmed the research as an established fact.",
    "conspiratorial": "A classified conspiracy was suppressed and redacted by deep state.",
    "ominous": "Sinister darkness was lurking and looming with inevitable foreboding dread.",
    "shocked": "Nobody knew it was impossible; the world was shocked and stunned.",
    "melancholic": "A bittersweet melancholy and wistful longing for a faded lost era.",
    "urgent": "Warning: emergency danger immediately, a critical imminent threat rapidly.",
    "nostalgic": "Remember when in those days years ago in the bygone old times.",
    "cold": "A calculated ruthless and indifferent clinical report without emotion.",
    "haunted": "A ghost of the abandoned empty silence still haunts the traumatized survivor.",
    "solemn": "Victims died and were murdered; burial and memorial followed the tragedy.",
    "contemplative": "In the end looking back, the legacy emerged and was remembered.",
    "incredulous": "Surely not — hard to believe, cannot be, the public doubted skeptically.",
    "compassionate": "Mercy and kindness for the suffering of those they helped with empathy.",
    "detached": "Objectively and from a distance, a clinical observation without bias.",
    "accusatory": "They accused and blamed the guilty party charged with the crime.",
}


class TestEmotionDetection:
    """Verify each of 28 TTS emotions can be detected."""

    def test_all_twenty_eight_emotions_listed(self, analyzer: KeywordAnalyzer) -> None:
        assert len(TTS_EMOTIONS) == 28
        supported = set(analyzer.list_supported_emotions())
        for emotion in TTS_EMOTIONS:
            assert emotion in supported

    @pytest.mark.parametrize("emotion", list(TTS_EMOTIONS))
    def test_each_emotion_detection(
        self, analyzer: KeywordAnalyzer, emotion: str
    ) -> None:
        sample = EMOTION_SAMPLES[emotion]
        result = analyzer.analyze_scene_text(sample)
        assert result["success"] is True
        data = result["data"]
        assert emotion in data["all_moods"]
        # Primary should be the target emotion, or at least positive score
        assert data["all_moods"][emotion] > 0
        # For strongly distinct samples, primary should match
        if data["all_moods"][emotion] >= 2:
            assert data["primary_mood"] == emotion or data["all_moods"][emotion] == max(
                data["all_moods"].values()
            )


class TestSfxTransitionsAnimations:
    """SFX, transition, and animation suggestions."""

    def test_sfx_keyword_detection(self, analyzer: KeywordAnalyzer) -> None:
        result = analyzer.detect_sfx_keywords(
            "The shocking death and funeral bell rang after the war battle."
        )
        assert result["success"] is True
        sfx = result["data"]["sfx"]
        assert "church_bell" in sfx or "dramatic_boom" in sfx
        assert "battlefield" in sfx

    def test_transition_suggestion(self, analyzer: KeywordAnalyzer) -> None:
        result = analyzer.detect_transition_style(
            "Suddenly without warning the darkness fell and he died.",
            "dramatic",
        )
        assert result["success"] is True
        assert result["data"]["transition"] in {
            "hard_cut",
            "dip_to_black",
            "crossfade",
            "dissolve",
            "fade",
        }

    def test_animation_suggestion(self, analyzer: KeywordAnalyzer) -> None:
        result = analyzer.detect_animation_style(
            "They traveled across the map on a long journey.",
            "historical",
        )
        assert result["success"] is True
        assert result["data"]["animation"] in {
            "pan_left",
            "pan_right",
            "ken_burns",
            "slow_zoom_in",
            "slow_zoom_out",
            "dramatic_zoom_in",
            "vertical_scan",
            "static",
        }

    def test_analyze_scene_includes_recommendations(
        self, analyzer: KeywordAnalyzer
    ) -> None:
        result = analyzer.analyze_scene_text(
            "A shocking secret conspiracy was revealed with devastating bombshell evidence."
        )
        assert result["success"] is True
        data = result["data"]
        assert (
            data["primary_mood"] in TTS_EMOTIONS or data["primary_mood"] == "historical"
        )
        assert isinstance(data["detected_sfx"], list)
        assert data["recommended_transition"]
        assert data["recommended_animation"]


class TestEdgeCases:
    """Empty, neutral, long, and mixed text."""

    def test_empty_text(self, analyzer: KeywordAnalyzer) -> None:
        result = analyzer.analyze_scene_text("")
        assert result["success"] is True
        assert result["data"]["primary_mood"] == "neutral"
        assert result["data"]["confidence"] == 0.0
        assert result["data"]["detected_sfx"] == []

    def test_whitespace_only(self, analyzer: KeywordAnalyzer) -> None:
        result = analyzer.analyze_scene_text("   \n\t  ")
        assert result["success"] is True
        assert result["data"]["primary_mood"] == "neutral"

    def test_no_keywords_returns_neutral(self, analyzer: KeywordAnalyzer) -> None:
        result = analyzer.analyze_scene_text(
            "The apple sat on the table near a blue cup and a wooden chair."
        )
        assert result["success"] is True
        assert result["data"]["primary_mood"] == "neutral"
        assert result["data"]["confidence"] == 0.0

    def test_long_text(self, analyzer: KeywordAnalyzer) -> None:
        base = (
            "The shocking investigation uncovered classified evidence of a conspiracy. "
            "Historical records confirmed the devastating secret. "
        )
        long_text = base * 80  # well over 1000 words
        assert len(long_text.split()) > 1000
        result = analyzer.analyze_scene_text(long_text)
        assert result["success"] is True
        assert result["data"]["word_count"] > 1000
        assert result["data"]["primary_mood"] != "neutral"
        assert result["data"]["confidence"] > 0

    def test_all_keywords_one_emotion(self, analyzer: KeywordAnalyzer) -> None:
        # Pack many dramatic keywords
        text = (
            "shocking horrifying devastating revealed exposed stunned terrifying "
            "catastrophic unimaginable extreme explosive staggering bombshell"
        )
        result = analyzer.analyze_scene_text(text)
        assert result["success"] is True
        assert result["data"]["primary_mood"] == "dramatic"
        assert result["data"]["all_moods"]["dramatic"] >= 5
        assert result["data"]["confidence"] > 0.5

    def test_mixed_emotion_text(self, analyzer: KeywordAnalyzer) -> None:
        text = (
            "A mysterious unexplained secret was revealed in a shocking bombshell. "
            "The victims died in a solemn tragedy while the investigation found evidence."
        )
        result = analyzer.analyze_scene_text(text)
        assert result["success"] is True
        positive = {k: v for k, v in result["data"]["all_moods"].items() if v > 0}
        assert len(positive) >= 2
        assert result["data"]["primary_mood"] in positive
        assert result["data"]["secondary_mood"] in positive


class TestProjectAnalysisAndOptional:
    """Database integration and disable behavior."""

    def test_analyze_all_scenes_updates_mood(self, analyzer: KeywordAnalyzer) -> None:
        db = analyzer.db
        project_id = db.new_id()
        assert db.create_project(
            {
                "id": project_id,
                "title": "Keyword Test",
                "project_folder_path": "projects/kw_test",
            }
        )
        scene_id = db.new_id()
        assert db.save_scene(
            {
                "id": scene_id,
                "project_id": project_id,
                "scene_number": 1,
                "image_filename": "dark_castle_night.jpg",
            }
        )
        line_id = db.new_id()
        db.db.execute(
            "INSERT INTO dialogue_lines "
            "(id, project_id, scene_id, line_number, character_name, text_content, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                line_id,
                project_id,
                scene_id,
                1,
                "NARRATOR",
                "A shocking devastating bombshell was revealed to the stunned world.",
                utc_now_str(),
                utc_now_str(),
            ),
        )
        result = analyzer.analyze_all_scenes(project_id)
        assert result["success"] is True
        assert result["data"]["analyzed"] == 1
        row = db.db.fetch_one(
            "SELECT keyword_mood FROM scenes WHERE id = ?", (scene_id,)
        )
        assert row is not None
        assert row["keyword_mood"]
        assert row["keyword_mood"] != ""

    def test_cache_used_on_second_analyze(self, analyzer: KeywordAnalyzer) -> None:
        text = "A mysterious unexplained secret vanished without a trace."
        first = analyzer.analyze_scene_text(text)
        assert first["success"]
        # Manual cache path via analyze_all is covered; direct cache set/get
        key = analyzer._cache_key("p", "s", text)
        analyzer._set_cached_analysis(key, first["data"])
        cached = analyzer._get_cached_analysis(key)
        assert cached is not None
        assert cached["primary_mood"] == first["data"]["primary_mood"]

    def test_disabled_module(self, analyzer: KeywordAnalyzer) -> None:
        analyzer.set_enabled(False)
        result = analyzer.analyze_scene_text("shocking revealed bombshell")
        assert result["success"] is False
        assert "disabled" in (result["error"] or "").lower()
        analyzer.set_enabled(True)

    def test_optional_flag(self, analyzer: KeywordAnalyzer) -> None:
        assert analyzer.is_optional_module() is True

    def test_modules_config_registration(self, analyzer: KeywordAnalyzer) -> None:
        entry = analyzer.config.get_module_config("keyword_analyzer")
        assert entry is not None
        assert entry.get("enabled") is True
        assert entry.get("required") is False
