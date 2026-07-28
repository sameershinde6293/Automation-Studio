"""Generate synthetic sample project fixtures for Autopilot tests.

Creates labeled JPEG images, sine-wave WAV audio, and sample scripts
in TXT, JSON, and CSV formats under tests/fixtures/sample_project/.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).resolve().parent / "sample_project"


def generate_test_images() -> None:
    """Generate solid color test images for all sample scenes."""
    colors = {
        "dark_castle_night.jpg": (15, 20, 40),
        "ancient_map_europe.jpg": (101, 67, 33),
        "plague_victims_medieval.jpg": (40, 40, 50),
        "black_death_ships.jpg": (20, 30, 60),
        "medieval_city_street.jpg": (70, 70, 80),
        "church_interior_dark.jpg": (50, 35, 25),
        "mass_grave_field.jpg": (30, 50, 30),
        "rat_infestation.jpg": (60, 25, 25),
        "doctor_plague_mask.jpg": (10, 10, 10),
        "europe_aftermath.jpg": (45, 30, 55),
    }
    output_dir = SAMPLE / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, color in colors.items():
        img = Image.new("RGB", (1920, 1080), color=color)
        draw = ImageDraw.Draw(img)
        label = filename.replace(".jpg", "").replace("_", " ").title()
        draw.text((960, 540), label, fill=(200, 200, 200), anchor="mm")
        img.save(str(output_dir / filename), "JPEG", quality=85)
    print(f"Generated {len(colors)} test images")


def generate_test_wav(
    output_path: str | Path,
    duration_seconds: int,
    frequency: int = 440,
    sample_rate: int = 48000,
) -> None:
    """Generate stereo sine wave WAV file for testing."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = duration_seconds * sample_rate
    with wave.open(str(path), "w") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = []
        for i in range(int(num_samples)):
            value = int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
            frames.append(struct.pack("<hh", value, value))
        wav_file.writeframes(b"".join(frames))
    print(f"Generated WAV: {path}")


def write_sample_script_txt() -> None:
    """Write the Black Death sample script in TXT format."""
    script_dir = SAMPLE / "script"
    script_dir.mkdir(parents=True, exist_ok=True)
    content = """//AUTOPILOT SCRIPT v1.0
//TITLE: The Dark History of the Black Death
//CHANNEL: Dark History Channel
//GENRE: dark_history
//COLOR_GRADE: dark_moody
//TRANSITION: crossfade
//ANIMATION: ken_burns
//MUSIC: sample_music.wav
//EXPORT: youtube_1080p

//VOICE_SETUP_START
NARRATOR: voice=deep_male_us, engine=piper, emotion=dramatic, speed=0.90, pitch=-2, reverb=subtle_room, echo=none, breathing=on, pause_sentence=0.6, pause_paragraph=1.8
HISTORIAN: voice=british_male_01, engine=piper, emotion=authoritative, speed=0.95, pitch=0, reverb=none, echo=none, breathing=off, pause_sentence=0.5, pause_paragraph=1.5
//VOICE_SETUP_END

//SCENE_START: scene_01
//IMAGE: dark_castle_night.jpg
//DURATION: auto
//TRANSITION_IN: fade
//TRANSITION_OUT: crossfade
//ANIMATION: slow_zoom_in
//COLOR_GRADE: dark_moody
//SFX: ominous_drone
//CHAPTER: Introduction

[NARRATOR|dramatic]
In the year 1347, [PAUSE:SHORT] twelve Genoese trading ships docked at the port of Messina in Sicily.

[PAUSE:MEDIUM]

[NARRATOR|ominous]
What the port authorities found when they approached the ships was terrifying.

[PAUSE:LONG]

[NARRATOR|dramatic]
Most of the sailors were dead. [PAUSE:SHORT] And those still alive were gravely ill.

//SCENE_END

//SCENE_START: scene_02
//IMAGE: ancient_map_europe.jpg
//DURATION: auto
//TRANSITION_IN: crossfade
//TRANSITION_OUT: dissolve
//ANIMATION: pan_left
//COLOR_GRADE: dark_moody
//SFX: none
//CHAPTER: The Spread

[HISTORIAN|authoritative]
Historical records confirm that the disease spread with terrifying speed across the continent.

[PAUSE:SHORT]

[HISTORIAN|serious]
Within months, it had reached the major cities of southern Europe.

[PAUSE:MEDIUM]

[HISTORIAN|cold]
The death toll was unlike anything in recorded human history.

//SCENE_END

//SCENE_START: scene_03
//IMAGE: plague_victims_medieval.jpg
//DURATION: auto
//TRANSITION_IN: dip_to_black
//TRANSITION_OUT: crossfade
//ANIMATION: ken_burns
//COLOR_GRADE: dark_moody
//SFX: church_bell

[NARRATOR|solemn]
The victims suffered terribly. [PAUSE:MEDIUM] Dark swellings appeared on the lymph nodes.

[PAUSE:SHORT]

[NARRATOR|haunted]
The skin turned black. [PAUSE:DRAMATIC] That is how it earned its name.

//SCENE_END

//SCENE_START: scene_04
//IMAGE: black_death_ships.jpg
//DURATION: auto
//TRANSITION_IN: crossfade
//TRANSITION_OUT: crossfade
//ANIMATION: slow_zoom_out
//COLOR_GRADE: dark_moody

[NARRATOR|conspiratorial]
Many believed it was divine punishment. [PAUSE:MEDIUM] Others blamed outsiders.

[PAUSE:SHORT]

[HISTORIAN|serious]
We now know it was caused by the bacterium Yersinia pestis.

//SCENE_END

//SCENE_START: scene_05
//IMAGE: medieval_city_street.jpg
//DURATION: auto
//TRANSITION_IN: crossfade
//TRANSITION_OUT: crossfade
//ANIMATION: pan_right
//COLOR_GRADE: dark_moody
//SFX: crowd_murmur

[NARRATOR|urgent]
Cities became ghost towns overnight. [PAUSE:SHORT] The living could not bury the dead fast enough.

//SCENE_END

//SCENE_START: scene_06
//IMAGE: church_interior_dark.jpg
//DURATION: auto
//TRANSITION_IN: dissolve
//TRANSITION_OUT: crossfade
//ANIMATION: slow_zoom_in
//COLOR_GRADE: dark_moody
//CHAPTER: The Response

[HISTORIAN|authoritative]
The Church was powerless against this invisible enemy.

//SCENE_END

//SCENE_START: scene_07
//IMAGE: mass_grave_field.jpg
//DURATION: auto
//TRANSITION_IN: dip_to_black
//TRANSITION_OUT: crossfade
//ANIMATION: slow_zoom_out
//COLOR_GRADE: dark_moody
//SFX: church_bell

[NARRATOR|solemn]
Mass graves were dug outside city walls. [PAUSE:MEDIUM] Thousands buried with no ceremony.

//SCENE_END

//SCENE_START: scene_08
//IMAGE: rat_infestation.jpg
//DURATION: auto
//TRANSITION_IN: crossfade
//TRANSITION_OUT: crossfade
//ANIMATION: ken_burns
//COLOR_GRADE: dark_moody
//CHAPTER: The Cause

[HISTORIAN|investigative]
It was carried by fleas living on black rats.

//SCENE_END

//SCENE_START: scene_09
//IMAGE: doctor_plague_mask.jpg
//DURATION: auto
//TRANSITION_IN: crossfade
//TRANSITION_OUT: crossfade
//ANIMATION: slow_zoom_in
//COLOR_GRADE: dark_moody

[NARRATOR|dramatic]
Plague doctors wore beaked masks filled with herbs and flowers.

//SCENE_END

//SCENE_START: scene_10
//IMAGE: europe_aftermath.jpg
//DURATION: auto
//TRANSITION_IN: crossfade
//TRANSITION_OUT: fade
//ANIMATION: slow_zoom_out
//COLOR_GRADE: dark_moody
//CHAPTER: The Aftermath

[NARRATOR|solemn]
By the time the plague had run its course, between thirty and sixty percent of Europe's population was dead.

[PAUSE:LONG]

[NARRATOR|dramatic]
Fifty million people. [PAUSE:DRAMATIC] Gone.

//SCENE_END
"""
    (script_dir / "sample_script.txt").write_text(content, encoding="utf-8")
    print("Wrote sample_script.txt")


def write_sample_script_json() -> None:
    """Write a compact JSON sample script (first scenes + summary)."""
    data = {
        "autopilot_version": "1.0",
        "project": {
            "title": "The Dark History of the Black Death",
            "channel": "Dark History Channel",
            "genre": "dark_history",
            "color_grade": "dark_moody",
            "default_transition": "crossfade",
            "default_animation": "ken_burns",
            "music": "sample_music.wav",
            "export_preset": "youtube_1080p",
        },
        "voice_profiles": [
            {
                "character": "NARRATOR",
                "voice": "deep_male_us",
                "engine": "piper",
                "default_emotion": "dramatic",
                "speed": 0.9,
                "pitch": -2,
            },
            {
                "character": "HISTORIAN",
                "voice": "british_male_01",
                "engine": "piper",
                "default_emotion": "authoritative",
                "speed": 0.95,
                "pitch": 0,
            },
        ],
        "scenes": [
            {
                "id": "scene_01",
                "image": "dark_castle_night.jpg",
                "dialogue": [
                    {
                        "character": "NARRATOR",
                        "emotion": "dramatic",
                        "text": "In the year 1347, twelve Genoese trading ships docked at the port of Messina in Sicily.",
                    }
                ],
            },
            {
                "id": "scene_02",
                "image": "ancient_map_europe.jpg",
                "dialogue": [
                    {
                        "character": "HISTORIAN",
                        "emotion": "authoritative",
                        "text": "Historical records confirm that the disease spread with terrifying speed across the continent.",
                    }
                ],
            },
        ],
    }
    path = SAMPLE / "script" / "sample_script.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("Wrote sample_script.json")


def write_sample_script_csv() -> None:
    """Write CSV sample script rows."""
    rows = [
        "character,emotion,pause,image,transition_in,transition_out,animation,sfx,chapter,text",
        'NARRATOR,dramatic,medium,dark_castle_night.jpg,fade,crossfade,slow_zoom_in,ominous_drone,Introduction,"In the year 1347, twelve Genoese trading ships docked at the port of Messina in Sicily."',
        'HISTORIAN,authoritative,short,ancient_map_europe.jpg,crossfade,dissolve,pan_left,,The Spread,"Historical records confirm that the disease spread with terrifying speed across the continent."',
        'NARRATOR,haunted,dramatic,plague_victims_medieval.jpg,dip_to_black,crossfade,ken_burns,church_bell,,"The skin turned black. That is how it earned its name."',
    ]
    path = SAMPLE / "script" / "sample_script.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("Wrote sample_script.csv")


def generate_all_fixtures() -> None:
    """Generate images, audio, and scripts for the sample project."""
    generate_test_images()
    audio_dir = SAMPLE / "audio"
    generate_test_wav(
        audio_dir / "sample_narration.wav", duration_seconds=8, frequency=440
    )
    generate_test_wav(
        audio_dir / "sample_music.wav", duration_seconds=12, frequency=220
    )
    write_sample_script_txt()
    write_sample_script_json()
    write_sample_script_csv()
    print("All fixtures generated successfully")


if __name__ == "__main__":
    generate_all_fixtures()
