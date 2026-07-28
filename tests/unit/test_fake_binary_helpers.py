"""Self-tests for the cross-platform fake ffmpeg/ffprobe test doubles.

Guards the WinError 193 hotfix: the Python (Windows-mode) fakes must
execute identically to the bash (POSIX) fakes, and the subprocess-shim
rewrite must only touch sentinel-marked fakes. These tests force both
variants explicitly, so they run the same on Windows and POSIX hosts.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

import conftest

IS_WINDOWS = platform.system() == "Windows"


def _run_fake(argv: List[str]) -> subprocess.CompletedProcess:
    """Execute a fake binary via the shim rewrite (any platform)."""
    rewritten = [str(arg) for arg in conftest._rewrite_fake_argv(argv)]
    return subprocess.run(rewritten, capture_output=True, text=True, timeout=30)


class TestBuilders:
    def test_bash_variant(self, tmp_path: Path) -> None:
        ffmpeg = conftest.build_fake_ffmpeg(
            tmp_path, tmp_path / "log.txt", variant="bash"
        )
        body = ffmpeg.read_text(encoding="utf-8")
        assert ffmpeg.name == "ffmpeg"
        assert body.startswith("#!/usr/bin/env bash")
        assert "autopilot-test-fake-binary" not in body
        probe = conftest.build_fake_ffprobe(tmp_path, variant="bash")
        assert probe.name == "ffprobe"
        assert "FAKE_PROBE_DURATION" in probe.read_text(encoding="utf-8")

    def test_python_variant_layout(self, tmp_path: Path) -> None:
        ffmpeg = conftest.build_fake_ffmpeg(tmp_path, variant="python")
        assert ffmpeg.name == "ffmpeg"  # name identical across platforms
        lines = ffmpeg.read_text(encoding="utf-8").splitlines()
        assert lines[0] == conftest._FAKE_SENTINEL
        probe = conftest.build_fake_ffprobe(tmp_path, variant="python")
        assert probe.name == "ffprobe"
        assert (
            probe.read_text(encoding="utf-8").splitlines()[0] == conftest._FAKE_SENTINEL
        )

    def test_python_sources_compile(self, tmp_path: Path) -> None:
        ffmpeg = conftest.build_fake_ffmpeg(tmp_path, variant="python")
        probe = conftest.build_fake_ffprobe(tmp_path, variant="python")
        for fake in (ffmpeg, probe):
            source = fake.read_text(encoding="utf-8")
            compile(source, str(fake), "exec")  # raises on syntax error


class TestPythonFakeBehaviour:
    def test_argv_log_and_output_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "fake.log"
        monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
        ffmpeg = conftest.build_fake_ffmpeg(tmp_path, log, variant="python")
        out = tmp_path / "nested" / "o.mp4"
        proc = _run_fake([str(ffmpeg), "-i", "a.png", "-t", "8.000", str(out)])
        assert proc.returncode == 0
        assert out.read_bytes() == b"FAKEMP4DATA\n"
        argv_log = log.read_text(encoding="utf-8")
        assert "CMD -i a.png" in argv_log and "-t 8.000" in argv_log

    def test_env_switches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_FFMPEG_LOG", str(tmp_path / "fake.log"))
        ffmpeg = conftest.build_fake_ffmpeg(tmp_path, variant="python")

        monkeypatch.setenv("FAKE_NVENC_LISTED", "1")
        listed = _run_fake([str(ffmpeg), "-encoders"])
        assert listed.returncode == 0
        assert "libx264" in listed.stdout and "h264_nvenc" in listed.stdout

        monkeypatch.setenv("FAKE_NVENC_BROKEN", "1")
        broken = _run_fake([str(ffmpeg), "-c:v", "h264_nvenc", str(tmp_path / "t.mp4")])
        assert broken.returncode == 1 and "nvenc broken" in broken.stderr
        monkeypatch.delenv("FAKE_NVENC_BROKEN")

        monkeypatch.setenv("FAKE_FRAMES", "1")
        framed = _run_fake([str(ffmpeg), "-i", "a.png", str(tmp_path / "f.mp4")])
        assert framed.returncode == 0
        assert framed.stderr.count("frame=") == 5
        assert "frame=  240 fps= 60.0" in framed.stderr
        monkeypatch.delenv("FAKE_FRAMES")

        monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
        failed = _run_fake([str(ffmpeg), "-i", "a.png", str(tmp_path / "x.mp4")])
        assert failed.returncode == 1 and "Conversion failed!" in failed.stderr
        assert not (tmp_path / "x.mp4").exists()  # failure happens before write

    def test_ffprobe_duration_and_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = conftest.build_fake_ffprobe(tmp_path, variant="python")
        monkeypatch.setenv("FAKE_PROBE_DURATION", "12.5")
        ok = _run_fake([str(probe), str(tmp_path / "v.mp4")])
        assert ok.returncode == 0
        assert '"duration":"12.5"' in ok.stdout
        assert '"codec_type":"video"' in ok.stdout
        assert '"codec_type":"audio"' in ok.stdout
        monkeypatch.setenv("FAKE_PROBE_FAIL", "1")
        bad = _run_fake([str(probe), str(tmp_path / "v.mp4")])
        assert bad.returncode == 1

    def test_special_characters_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Args with & (ASS colours) and () must reach the fake intact.

        This is the failure a .bat/cmd-based fake would have suffered.
        """
        log = tmp_path / "fake.log"
        monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
        ffmpeg = conftest.build_fake_ffmpeg(tmp_path, log, variant="python")
        style = "FontName=Arial,PrimaryColour=&H00BBGGRR,BorderStyle=1"
        graph = "zoompan=z='min(max(zoom+0.0008,1.0),1.4)',subtitles='s.srt'"
        out = tmp_path / "o.mp4"
        proc = _run_fake([str(ffmpeg), "-vf", graph, "-force_style", style, str(out)])
        assert proc.returncode == 0
        argv_log = log.read_text(encoding="utf-8")
        assert "&H00BBGGRR" in argv_log
        assert "min(max(zoom+0.0008,1.0),1.4)" in argv_log


class TestShimRewrite:
    def test_rewrite_targets_only_python_fakes(self, tmp_path: Path) -> None:
        fake = conftest.build_fake_ffmpeg(tmp_path, variant="python")
        rewritten = conftest._rewrite_fake_argv([str(fake), "-version"])
        assert rewritten[0] == sys.executable
        assert rewritten[1] == str(fake)
        assert rewritten[2:] == ["-version"]
        # Idempotent: rewriting the rewritten list changes nothing.
        again = conftest._rewrite_fake_argv(rewritten)
        assert again == rewritten

    def test_rewrite_ignores_real_and_missing(self, tmp_path: Path) -> None:
        assert conftest._rewrite_fake_argv(["git", "status"]) == ["git", "status"]
        binary = tmp_path / "ffmpeg.exe"
        binary.write_bytes(b"MZ not a python fake")
        argv = [str(binary), "-version"]
        assert conftest._rewrite_fake_argv(argv) == argv
        bash = conftest.build_fake_ffmpeg(tmp_path / "posix", variant="bash")
        bash_argv = [str(bash), "-version"]
        assert conftest._rewrite_fake_argv(bash_argv) == bash_argv
        assert conftest._rewrite_fake_argv([str(tmp_path / "missing"), "x"]) == [
            str(tmp_path / "missing"),
            "x",
        ]
        assert conftest._rewrite_fake_argv([]) == []

    @pytest.mark.skipif(IS_WINDOWS, reason="bash fakes only execute on POSIX")
    def test_bash_and_python_variants_behave_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same scenario, both variants: identical observable behaviour."""
        results = {}
        for variant in ("bash", "python"):
            folder = tmp_path / variant
            log = folder / "fake.log"
            monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
            ffmpeg = conftest.build_fake_ffmpeg(folder, log, variant=variant)
            out = folder / "o.mp4"
            monkeypatch.setenv("FAKE_FRAMES", "1")
            proc = _run_fake([str(ffmpeg), "-i", "in.png", "-t", "4.000", str(out)])
            monkeypatch.delenv("FAKE_FRAMES")
            results[variant] = (
                proc.returncode,
                proc.stderr.count("frame="),
                out.read_bytes() if out.exists() else b"",
                "-t 4.000" in log.read_text(encoding="utf-8"),
            )
        assert results["bash"] == results["python"] == (0, 5, b"FAKEMP4DATA\n", True)


class TestWavPayloadValidity:
    """D.2 recurrence guard: .wav outputs must be VALID, non-empty WAVs.

    The first D.2 implementation wrote the payload as
    b'\\x00\\x00' * 48000 * 2 // 4, which raises TypeError
    (bytes // int evaluates left-to-right). The fake still exited 0,
    silently leaving a 44-byte header-only WAV behind; pydub only
    blew up far downstream in the CLI smoke (CouldntDecodeError with
    rc 0). Pin both variants to a real, readable 0.25 s stereo WAV.
    """

    def test_wav_output_readable_both_variants(self, tmp_path: Path) -> None:
        import wave

        # Real Windows cannot execute the bash variant (WinError 193);
        # the Python variant is the canonical fake there. POSIX runs
        # both so drift between the two is caught on developer boxes.
        variants = ["python"] if IS_WINDOWS else ["bash", "python"]
        for variant in variants:
            folder = tmp_path / variant
            ffmpeg = conftest.build_fake_ffmpeg(folder, variant=variant)
            out = folder / "line.wav"
            proc = _run_fake([str(ffmpeg), "-i", "in", "-y", str(out)])
            assert proc.returncode == 0, proc.stderr
            with wave.open(str(out), "rb") as handle:
                assert handle.getnchannels() == 2
                assert handle.getsampwidth() == 2
                assert handle.getframerate() == 48000
                assert handle.getnframes() == 12000  # 0.25 s @ 48 kHz stereo
