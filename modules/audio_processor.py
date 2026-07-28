"""Audio processor: narration assembly, ducking, mix, LUFS, limiter.

Required BaseModule for final render audio. Uses NumPy for ducking envelopes,
PyDub for mixing, and FFmpeg loudnorm when available (graceful fallback).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.safe_io import (
    LazyModule,
    atomic_write,
    ensure_directory,
    purge_stale_temp_files,
    safe_unlink,
)
from core.service_container import BaseModule, ServiceContainer

np = LazyModule("numpy")

MODULE_NAME = "audio_processor"

DEFAULT_DUCKING: Dict[str, float] = {
    "ducking_threshold": 0.02,
    "ducking_depth": 0.15,
    "ducking_ceiling": 0.50,
    "attack_time": 0.30,
    "release_time": 0.80,
    "min_silence_duration": 0.50,
    "window_sec": 0.02,
    "hop_sec": 0.01,
}

SAMPLE_RATE = 48000

# PHASE 1 (audio stability): a buffer is rejected outright only when it is
# this degenerate — empty, or so overwhelmingly non-finite that repairing
# it in place would just be manufacturing fake audio. Buffers with a few
# stray NaN/Inf samples (the common real-world case: one bad frame from a
# flaky decoder) are sanitized instead of rejected, since a decorated
# error is worse than briefly muting a handful of samples.
_MAX_NONFINITE_RATIO = 0.5

# PHASE 6 (natural pauses & human pacing): absolute ceiling for any
# single inter-line gap accepted by build_narration_track's pause_plan.
# core.narration_pacing already clamps its own output well below this;
# this is the defensive backstop for a caller-supplied plan, so a bad
# value can never insert minutes of dead air into a narration track.
_MAX_LINE_PAUSE_SECONDS = 10.0

# PHASE 8 (rendering & export optimization): the mix pipeline reads the
# very same WAV several times in a row — every stage validates its input
# and its output with `_validate_audio_file` (a full decode), then the
# next stage decodes the identical bytes again, and measure_peak_db /
# measure_approx_lufs decode the final file twice more. Decoding is pure
# (same bytes in, same samples out), so results are memoized per file
# identity (path + size + mtime_ns) and reused instead of re-decoded.
# The cache is bounded: an entry larger than the whole budget is never
# stored, and least-recently-used entries are evicted once the budget is
# reached, so a 5-hour render can't grow memory without limit.
_DECODE_CACHE_BUDGET_BYTES = 256 * 1024 * 1024
_DECODE_CACHE_MAX_ENTRY_BYTES = 128 * 1024 * 1024


# PHASE 8: one memoized decode — (size, mtime_ns), samples, sample rate.
_DecodeEntry = Tuple[Tuple[int, int], np.ndarray, int]


class AudioProcessor(BaseModule):
    """Mix narration, music, SFX, and ambient into a final render track."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize without loading heavy audio into memory."""
        super().__init__(container, MODULE_NAME)
        self._project_root = Path.cwd()
        try:
            cfg = getattr(self.config, "config_folder", None)
            if cfg is not None:
                self._project_root = Path(cfg).resolve().parent
        except Exception:  # noqa: BLE001
            pass
        # PHASE 8: decoded-audio memo (see _DECODE_CACHE_BUDGET_BYTES).
        # OrderedDict == LRU order; the lock keeps it consistent when the
        # orchestrator mixes/validates from more than one thread.
        self._decode_cache: "OrderedDict[str, _DecodeEntry]" = OrderedDict()
        self._decode_cache_bytes = 0
        self._decode_lock = threading.Lock()
        self._ffmpeg_cache: Optional[Path] = None

    # ------------------------------------------------------------------
    # Decoded-audio cache (PHASE 8: rendering & export optimization)
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_cache_key(path: Path) -> Optional[Tuple[str, Tuple[int, int]]]:
        """Identity of a file's CONTENT: (resolved path, (size, mtime_ns))."""
        try:
            stat = path.stat()
        except OSError:
            return None
        return str(path.resolve()), (int(stat.st_size), int(stat.st_mtime_ns))

    def _decode_cache_get(self, path: Path) -> Optional[Tuple[np.ndarray, int]]:
        """Return cached samples for ``path`` when the file is unchanged."""
        key = self._decode_cache_key(path)
        if key is None:
            return None
        name, stamp = key
        with self._decode_lock:
            entry = self._decode_cache.get(name)
            if entry is None or entry[0] != stamp:
                return None
            self._decode_cache.move_to_end(name)
            return entry[1], entry[2]

    def _decode_cache_put(self, path: Path, data: np.ndarray, sr: int) -> None:
        """Memoize decoded samples, evicting LRU entries past the budget.

        PHASE 9 (memory allocation failures): the memo is an
        optimization. If the process is already under memory pressure,
        skip caching entirely rather than adding to it — the caller
        keeps the buffer it just decoded either way.
        """
        size = int(getattr(data, "nbytes", 0))
        if size <= 0 or size > _DECODE_CACHE_MAX_ENTRY_BYTES:
            return
        key = self._decode_cache_key(path)
        if key is None:
            return
        name, stamp = key
        with self._decode_lock:
            previous = self._decode_cache.pop(name, None)
            if previous is not None:
                self._decode_cache_bytes -= int(previous[1].nbytes)
            self._decode_cache[name] = (stamp, data, int(sr))
            self._decode_cache_bytes += size
            while (
                self._decode_cache_bytes > _DECODE_CACHE_BUDGET_BYTES
                and len(self._decode_cache) > 1
            ):
                _, evicted = self._decode_cache.popitem(last=False)
                self._decode_cache_bytes -= int(evicted[1].nbytes)

    def _decode_cache_clear(self) -> None:
        """Release every memoized buffer (end of a mix)."""
        with self._decode_lock:
            self._decode_cache.clear()
            self._decode_cache_bytes = 0

    def _decode_cache_drop(self, path: str | Path) -> None:
        """Forget any memoized samples for ``path``.

        Called whenever this module writes/replaces a file through a path
        that isn't ``_write_audio`` (pydub export, ffmpeg loudnorm, a
        copy): a rewrite can keep the same byte size, and some
        filesystems report a coarse mtime, so identity alone is not a
        sufficient guard for in-place overwrites.
        """
        try:
            name = str(Path(path).resolve())
        except OSError:
            return
        with self._decode_lock:
            entry = self._decode_cache.pop(name, None)
            if entry is not None:
                self._decode_cache_bytes -= int(entry[1].nbytes)

    # ------------------------------------------------------------------
    # Public orchestration
    # ------------------------------------------------------------------

    def _validate_audio_file(self, path: str | Path, min_duration: float = 0.0) -> bool:
        """PHASE 1/9 defensive gate: reject a corrupted stage output.

        Called after every pipeline stage (narration build, ducking, mix,
        limiter, LUFS normalize) so a broken intermediate file is caught
        immediately at its source instead of silently propagating into
        the next stage (or, worst case, into the final export). Checks:
        file exists and is non-empty, is readable as audio, has a valid
        sample rate, is not empty/too-short, and contains no NaN/Inf
        (via the same sanitizing `_read_audio` every stage already uses).
        """
        p = Path(path)
        # PHASE 9: `stat()` on a path removed between the two calls (or
        # on a disconnected network share) raised OSError out of what is
        # supposed to be a boolean gate — an unreadable file is simply
        # invalid, which is exactly what every caller already handles.
        try:
            if not p.is_file() or p.stat().st_size == 0:
                return False
        except OSError:
            return False
        try:
            data, sr = self._read_audio(p)
        except Exception:  # noqa: BLE001 - unreadable is invalid
            return False
        if sr <= 0 or data.size == 0:
            return False
        if min_duration > 0.0 and (len(data) / float(sr)) < min_duration:
            return False
        return True

    def generate_final_mix(
        self,
        project_id: str,
        output_path: str | Path,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run full mix pipeline for a project.

        PHASE 9 (resource cleanup): the decoded-audio memo (PHASE 8) was
        released on the two SUCCESS paths only, so an early return — a
        failed narration build, a validation rejection — left up to
        256MB of decoded PCM held for the rest of the render. The memo
        is now released in a ``finally``, which also covers an
        unexpected exception propagating out of a mix stage.

        Args:
            project_id: Project UUID (used for temp naming / future DB).
            output_path: Final WAV path.
            settings: Optional ducking/volume overrides and track paths.

        Returns:
            Standard response with final path and metadata.
        """
        try:
            return self._generate_final_mix(project_id, output_path, settings)
        finally:
            self._decode_cache_clear()

    def _generate_final_mix(
        self,
        project_id: str,
        output_path: str | Path,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Mix pipeline body (see generate_final_mix for the contract)."""
        started = time.perf_counter()
        cfg = self._merge_settings(settings)
        work = self._work_dir(project_id)
        # PHASE 9: an unusable work folder is a clean, explained failure
        # instead of an OSError escaping the stage handler.
        if ensure_directory(work) is None:
            return self._err(f"Cannot create mix working folder: {work}", started)
        # PHASE 9: clear temp files a previously killed mix left behind
        # so they can't accumulate across retries of the same project.
        purge_stale_temp_files(work)

        narration = cfg.get("narration_path")
        if not narration:
            # BUGFIX (v3.1.8): this call relied on build_narration_track's
            # own default pause (0.4s) between lines, while word-timestamp
            # offsets (core_engine._PAUSE_BETWEEN_LINES) and scene duration
            # calculation (timeline_engine's matching constant) both assume
            # 0.25s — a THIRD instance of the same duplicated-constant
            # pattern already found in this codebase (outro duration,
            # subtitle pause). The real audio's actual gaps (confirmed via
            # direct measurement) were ~0.4s, not the 0.25s everything else
            # assumed — causing image/caption positions to drift from the
            # real voice more with every additional line. Pass the shared
            # value explicitly so the actual audio matches what every other
            # timing calculation in the app assumes.
            #
            # PHASE 6 (natural pauses & human pacing): that shared value is
            # now a FLOOR/fallback rather than the whole story — when the
            # caller supplies a per-join pause plan (core_engine does: the
            # very same one its word-timestamp offsets used), the narration
            # is assembled with those varying, human gaps. The invariant
            # the 3.1.8 fix established is unchanged — audio, captions and
            # scenes still all derive from one agreed set of gaps. Without
            # a plan this stays byte-for-byte the previous behavior.
            built = self.build_narration_track(
                project_id,
                cfg.get("line_paths") or [],
                work / "narration.wav",
                pause_seconds=float(cfg.get("pause_seconds") or 0.25),
                pause_plan=cfg.get("pause_plan"),
            )
            if not built["success"]:
                return built
            narration = built["data"]["audio_path"]
        if not self._validate_audio_file(narration):
            return self._err(
                f"Narration audio failed validation: {narration}", started
            )

        music = cfg.get("music_path")
        ducked = None
        if music and Path(music).exists():
            duck_out = work / "music_ducked.wav"
            ducked_resp = self.apply_music_ducking(narration, music, cfg, duck_out)
            if not ducked_resp["success"]:
                return ducked_resp
            ducked = ducked_resp["data"]["audio_path"]
            if not self._validate_audio_file(ducked):
                return self._err(
                    f"Ducked music audio failed validation: {ducked}", started
                )

        mixed_path = work / "mixed.wav"
        mix_resp = self.mix_tracks(
            narration,
            ducked,
            cfg.get("sfx_list") or [],
            cfg.get("ambient_path"),
            mixed_path,
            cfg,
        )
        if not mix_resp["success"]:
            return mix_resp
        if not self._validate_audio_file(mix_resp["data"]["audio_path"]):
            return self._err(
                f"Mixed audio failed validation: {mixed_path}", started
            )

        limited = work / "limited.wav"
        lim = self.apply_limiter(
            mix_resp["data"]["audio_path"], limited, ceiling_db=-1.0
        )
        if not lim["success"]:
            return lim
        if not self._validate_audio_file(lim["data"]["audio_path"]):
            return self._err(
                f"Limited audio failed validation: {limited}", started
            )

        final = Path(output_path)
        final.parent.mkdir(parents=True, exist_ok=True)
        norm = self.normalize_to_lufs(
            lim["data"]["audio_path"], final, target_lufs=-14.0
        )
        if not norm["success"] or not self._validate_audio_file(final):
            # Fall back to limited file if loudnorm unavailable or produced
            # an invalid result — the limited mix is already clean/safe.
            shutil.copy2(lim["data"]["audio_path"], final)
            self._decode_cache_drop(final)
            warnings = list(norm.get("warnings") or []) + [
                "LUFS normalization unavailable; used limited mix"
            ]
            # PHASE 3 (click removal): final whole-program safety net —
            # catches any residual click introduced by ducking envelope
            # transitions, resampling, or the limiter/normalize stages
            # themselves, on the exact bytes about to be exported.
            self.detect_and_repair_clicks(final)
            response = self.make_response(
                True,
                {
                    "audio_path": str(final),
                    "duration": self._wav_duration(final),
                    "lufs_normalized": False,
                    "peak_db": self.measure_peak_db(str(final)),
                },
                warnings=warnings,
                duration_ms=_ms(started),
            )
            # PHASE 8: the mix is finished — release the decode memo so
            # none of it is held for the rest of the render.
            self._decode_cache_clear()
            return response

        self.detect_and_repair_clicks(final)
        response = self.make_response(
            True,
            {
                "audio_path": str(final),
                "duration": self._wav_duration(final),
                "lufs_normalized": True,
                "peak_db": self.measure_peak_db(str(final)),
                "target_lufs": -14.0,
            },
            duration_ms=_ms(started),
        )
        # PHASE 8: the mix is finished — release the decode memo so none
        # of it is held for the rest of the render.
        self._decode_cache_clear()
        return response


    def build_narration_track(
        self,
        project_id: str,
        line_paths: Sequence[str | Path],
        output_path: str | Path,
        pause_seconds: float = 0.25,
        crossfade_ms: int = 20,
        trim_leading_silence: bool = True,
        pause_plan: Optional[Sequence[float]] = None,
    ) -> Dict[str, Any]:
        """Concatenate TTS line WAVs with pauses and equal-power crossfades.

        PHASE 3 (click removal / click & transition refinement): every
        line gets a short (5ms) edge fade before joining — cheap
        insurance against a join/start/end click regardless of whether
        the line's own generation already faded its edges (every TTS-
        generated line does — see TTSEngineManager._finalize_line_audio —
        but build_narration_track is also called directly, e.g. from
        tests or future non-TTS sources, so it must not assume that).
        Each join point is additionally snapped to the nearest true zero
        crossing (not just "close to zero") before crossfading, and uses
        an equal-power (not linear) crossfade curve so segment-to-segment
        transitions don't produce the audible loudness dip ("zipper")
        a linear crossfade causes. Every transition is validated
        (finite samples, no NaN/Inf, correct duration progression)
        immediately after it's made — a bad join is caught and the
        previous, still-good state kept rather than propagating silently
        into the rest of the track.

        PHASE 4 (remove hard intro): defense-in-depth — the very FIRST
        line's leading silence is trimmed here too (breath-safe, same
        algorithm as trim_leading_silence()), so an assembled narration
        track never opens with a hard/abrupt start even when the source
        line WAVs weren't produced via TTSEngineManager.generate_audio
        (which already trims at synthesis time). Pass
        ``trim_leading_silence=False`` to opt out entirely.

        PHASE 6 (natural pauses & human pacing): ``pause_plan`` supplies
        a PER-JOIN gap (seconds) instead of one flat gap everywhere — a
        comma-ended line runs on quickly, a full stop settles, a
        question hangs, a speaker/scene change gets a real beat, and the
        narrator gets room to breathe periodically. The plan is computed
        once by ``core.narration_pacing`` and shared with the
        orchestrator's word-timestamp offsets and timeline_engine's
        scene durations, so varying the gaps never desynchronises
        subtitles or images. ``pause_plan[i]`` is the silence BETWEEN
        ``line_paths[i]`` and ``line_paths[i + 1]``; a short plan falls
        back to ``pause_seconds`` for the remaining joins, and omitting
        it entirely keeps the exact previous flat-gap behavior.

        Args:
            project_id: Project UUID (echoed back in the response).
            line_paths: Per-line WAVs, in playback order.
            output_path: Destination narration WAV.
            pause_seconds: Flat gap between lines; also the fallback for
                any join ``pause_plan`` doesn't cover.
            crossfade_ms: Equal-power crossfade length at each join.
            trim_leading_silence: Trim the first line's leading silence.
            pause_plan: Optional per-join gaps (PHASE 6), seconds.

        Returns:
            Standard response with ``audio_path``, ``duration``,
            ``line_count``, ``segment_timestamps`` and (PHASE 6)
            ``pause_plan_used`` — the gaps actually inserted, so a
            caller can verify the audio matches the timing it planned.
        """
        started = time.perf_counter()
        paths = [Path(p) for p in line_paths if p and Path(p).exists()]
        if not paths:
            return self._err("No narration line files provided", started)
        try:
            from pydub import AudioSegment
        except ImportError:
            return self._err("pydub not installed", started)

        # PHASE 8 (rendering & export optimization): the track is grown
        # as a list of finalized PCM chunks plus one small mutable tail
        # segment instead of one ever-growing AudioSegment. Every join
        # only ever touches the last (crossfade + zero-crossing search)
        # milliseconds, so re-copying the entire assembled track at each
        # of the N joins — the O(N^2) byte copying that dominated a long
        # narration build — is unnecessary. The samples produced are
        # bit-identical: at 48kHz a millisecond is exactly 48 frames, so
        # every chunk boundary is cut on an exact frame boundary and all
        # pydub position arithmetic resolves to the same frame indices as
        # the single-segment form. See _flush_narration_prefix.
        prefix_chunks: List[Any] = []
        prefix_ms = 0
        tail = AudioSegment.silent(duration=0, frame_rate=SAMPLE_RATE).set_channels(2)
        timestamps: List[Dict[str, Any]] = []
        default_pause = max(0.0, float(pause_seconds))
        gaps = self._resolve_pause_plan(pause_plan, len(paths), default_pause)
        # Reuse one silence segment per distinct gap length — a long
        # narration has hundreds of joins but only a handful of distinct
        # rounded gap values, so this keeps allocation flat instead of
        # building a fresh buffer for every single join.
        silence_cache: Dict[int, Any] = {}

        def _silence_for(seconds: float) -> Any:
            ms = int(round(max(0.0, seconds) * 1000))
            segment = silence_cache.get(ms)
            if segment is None:
                # Built exactly as before (default 11025Hz mono) and then
                # converted through the same steps pydub's own `+` would
                # apply, so the inserted silence has the identical frame
                # count it always had — the conversion is just hoisted
                # out of the per-join path and memoized.
                segment = (
                    AudioSegment.silent(duration=ms)
                    .set_channels(2)
                    .set_frame_rate(SAMPLE_RATE)
                    .set_sample_width(2)
                )
                silence_cache[ms] = segment
            return segment

        fade = max(0, int(crossfade_ms))
        edge_fade_ms = 5
        # Everything older than this many milliseconds can never be read
        # again (a join only touches the crossfade window and the ±5ms
        # zero-crossing search), so it is flushed to a finalized chunk.
        keep_ms = fade + 64
        for index, path in enumerate(paths):
            seg = AudioSegment.from_file(str(path))
            seg = self._ensure_stereo_48k(seg)
            if index == 0 and trim_leading_silence:
                seg = self._trim_leading_silence_pydub(seg)
            if len(seg) > edge_fade_ms * 2:
                seg = seg.fade_in(edge_fade_ms).fade_out(edge_fade_ms)
            before_ms = prefix_ms + len(tail)
            # PHASE 6: the gap that precedes THIS line (i.e. the one
            # planned after the previous line). Index 0 has no incoming
            # join, so it never gets one.
            gap_seconds = gaps[index - 1] if index > 0 else 0.0
            silence = _silence_for(gap_seconds)
            if index > 0:
                # Pause between lines, then an equal-power micro-fade
                # into the next clip — snapping the join to a true zero
                # crossing first removes any residual discontinuity a
                # fade curve alone wouldn't fully hide.
                tail = tail + silence
                if fade > 0 and len(seg) > fade and prefix_ms + len(tail) > fade:
                    snap_ms = self._nearest_zero_crossing_ms(tail, len(tail))
                    if snap_ms < len(tail):
                        tail = tail[:snap_ms]
                    joined = self._equal_power_crossfade(tail, seg, fade)
                    if self._pydub_segment_is_valid(joined):
                        tail = joined
                    else:
                        # PHASE 3: validate after every transition — a
                        # broken crossfade (NaN/garbage samples) must
                        # never be accepted; fall back to a plain,
                        # already-fade-safe concatenation instead.
                        self.log.warning(
                            "Crossfade join produced invalid audio for "
                            "line %d — falling back to a plain join",
                            index,
                        )
                        tail = tail + seg
                else:
                    tail = tail + seg
            else:
                tail = tail + seg
            prefix_ms, tail = self._flush_narration_prefix(
                prefix_chunks, prefix_ms, tail, keep_ms
            )
            end_ms = prefix_ms + len(tail)
            start_ms = end_ms - len(seg)
            if index > 0 and fade > 0:
                start_ms = max(0, end_ms - len(seg))
            timestamps.append(
                {
                    "index": index,
                    "path": str(path),
                    "start": start_ms / 1000.0,
                    "end": end_ms / 1000.0,
                }
            )
            # PHASE 3: per-transition sanity check — duration must have
            # progressed by a plausible amount (never negative, never
            # wildly more than the pause + segment just added), catching
            # a corrupt join immediately rather than after the whole
            # track is assembled.
            added_ms = end_ms - before_ms
            if added_ms < 0 or added_ms > len(seg) + len(silence) + fade + 1:
                self.log.warning(
                    "Narration join %d produced an implausible duration "
                    "delta (%dms) — track may contain a corrupted join",
                    index,
                    added_ms,
                )

        total_ms = prefix_ms + len(tail)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # PHASE 8: stream the finalized chunks straight into the WAV
        # instead of first materializing one giant in-memory segment —
        # the bytes written are exactly the concatenation pydub's own
        # export would have produced, at a fraction of the peak memory.
        self._write_pcm_chunks_wav(out, prefix_chunks + [tail])
        self._decode_cache_drop(out)
        if not self._validate_audio_file(out):
            return self._err(
                f"Narration track failed validation after assembly: {out}", started
            )
        # PHASE 3 (click removal): a whole-track pass in addition to each
        # line's own edge fade — catches any residual click at a join or
        # pause splice that survived per-line processing once everything
        # is concatenated together.
        self.detect_and_repair_clicks(out)
        return self.make_response(
            True,
            {
                "audio_path": str(out),
                "duration": total_ms / 1000.0,
                "line_count": len(paths),
                "segment_timestamps": timestamps,
                "project_id": project_id,
                # PHASE 6: the gaps actually inserted (one per join), so
                # a caller can confirm the rendered audio matches the
                # timing it planned for subtitles/scenes.
                "pause_plan_used": [round(g, 3) for g in gaps],
            },
            duration_ms=_ms(started),
        )

    @staticmethod
    def _flush_narration_prefix(
        prefix_chunks: List[Any],
        prefix_ms: int,
        tail: Any,
        keep_ms: int,
    ) -> Tuple[int, Any]:
        """Move the settled head of ``tail`` into the finalized prefix.

        PHASE 8 (rendering & export optimization): only the last
        ``keep_ms`` of the assembled track can still be read by a later
        join (the crossfade window plus the zero-crossing search
        window), so everything before that is final and is moved out of
        the working segment. Cutting on a whole millisecond at 48kHz is
        an exact frame boundary (48 frames/ms), so the resulting samples
        are identical to keeping one growing segment — this only stops
        pydub from re-copying the entire track on every append.

        Args:
            prefix_chunks: Finalized segments, appended to in place.
            prefix_ms: Milliseconds already moved into ``prefix_chunks``.
            tail: The current working segment.
            keep_ms: Milliseconds of ``tail`` that must stay reachable.

        Returns:
            ``(new_prefix_ms, new_tail)``.
        """
        cut_ms = len(tail) - max(0, int(keep_ms))
        if cut_ms <= 0:
            return prefix_ms, tail
        prefix_chunks.append(tail[:cut_ms])
        return prefix_ms + cut_ms, tail[cut_ms:]

    @staticmethod
    def _write_pcm_chunks_wav(path: Path, chunks: Sequence[Any]) -> None:
        """Write pydub segments to one WAV without concatenating them first.

        PHASE 8 (rendering & export optimization): equivalent to
        ``sum(chunks).export(path, format="wav")`` for same-format PCM
        segments — which is exactly what the narration assembler
        produces — but streams each chunk's raw frames straight to disk
        instead of building a full-length copy in memory first.

        PHASE 9: the WAV is built at a temp path and atomically renamed.
        A narration track can take minutes to assemble; an interruption
        during the write used to leave a truncated (but existing) file
        that the validation gate then had to catch after the fact.
        Identical bytes, identical frame layout — only the moment the
        destination path starts existing changed.
        """
        reference = next((c for c in chunks if len(c) > 0), None)
        if reference is None:
            reference = chunks[-1]

        def _write(temp: Path) -> None:
            with wave.open(str(temp), "wb") as handle:
                handle.setnchannels(reference.channels)
                handle.setsampwidth(reference.sample_width)
                handle.setframerate(reference.frame_rate)
                for chunk in chunks:
                    if len(chunk):
                        handle.writeframes(chunk.raw_data)

        if not atomic_write(path, _write):
            raise OSError(f"could not write narration WAV: {path}")

    def _resolve_pause_plan(
        self,
        pause_plan: Optional[Sequence[float]],
        line_count: int,
        default_pause: float,
    ) -> List[float]:
        """Normalize a PHASE 6 per-join pause plan for ``line_count`` lines.

        PHASE 6 (natural pauses & human pacing): a narration of N lines
        has exactly N-1 joins. This returns that many gaps, in order,
        taking them from ``pause_plan`` where available and falling back
        to the flat ``default_pause`` for anything missing — so a plan
        that is short, over-long, partly unparseable, or absent entirely
        can never produce a mis-assembled track. Every value is clamped
        to a sane, non-negative range (the planner already clamps, but
        this method is public API surface: a caller may pass its own).

        Args:
            pause_plan: Optional per-join gaps in seconds.
            line_count: Number of narration line files.
            default_pause: Flat fallback gap in seconds.

        Returns:
            Exactly ``max(0, line_count - 1)`` non-negative gaps.
        """
        joins = max(0, int(line_count) - 1)
        if joins == 0:
            return []
        fallback = max(0.0, float(default_pause))
        if not pause_plan:
            return [fallback] * joins
        gaps: List[float] = []
        for index in range(joins):
            value = fallback
            if index < len(pause_plan):
                try:
                    value = float(pause_plan[index])
                except (TypeError, ValueError):
                    value = fallback
            if value != value or value in (float("inf"), float("-inf")):
                # NaN/Inf from a hand-built plan — use the flat default
                # rather than an unbounded or zero-length silence.
                value = fallback
            gaps.append(max(0.0, min(_MAX_LINE_PAUSE_SECONDS, value)))
        return gaps

    def apply_music_ducking(
        self,
        narration_path: str | Path,
        music_path: str | Path,
        settings: Optional[Dict[str, Any]],
        output_path: str | Path,
    ) -> Dict[str, Any]:
        """Duck music under narration using RMS envelope (File 08 algorithm)."""
        started = time.perf_counter()
        cfg = self._merge_settings(settings)
        if not Path(narration_path).exists():
            return self._err(f"Narration audio not found: {narration_path}", started)
        if not Path(music_path).exists():
            return self._err(f"Music audio not found: {music_path}", started)
        try:
            narr, sr = self._read_audio(narration_path)
            music, sr2 = self._read_audio(music_path)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Failed to load audio: {exc}", started)
        if sr <= 0 or sr2 <= 0:
            return self._err("Invalid sample rate (<=0) in ducking input", started)
        if narr.size == 0:
            return self._err("Narration audio is empty", started)

        if sr != sr2:
            music = self._resample_np(music, sr2, sr)
        # BUGFIX (v3.2.4): _match_length pads a too-short music track with
        # SILENCE, not a loop — for a long project (e.g. 5-hour narration
        # with a normal-length music track), the music would play once and
        # then go silent for the rest of the video. Loop it with a short
        # crossfade at each seam instead, so long content stays musically
        # covered throughout. Tracks already >= narration length are
        # completely unaffected (just trimmed as before).
        if len(music) < len(narr):
            music = self._loop_audio_to_length(music, len(narr), sr)
        music = self._match_length(music, len(narr))
        envelope = self.calculate_ducking_envelope(narration_path, cfg)
        if not envelope["success"]:
            return envelope
        env = np.asarray(envelope["data"]["envelope"], dtype=np.float64)
        env = self._match_length_1d(env, len(narr))
        ducked = self._apply_envelope(music, env)
        self._write_audio(output_path, ducked, sr)
        return self.make_response(
            True,
            {
                "audio_path": str(output_path),
                "sample_rate": sr,
                "duration": len(ducked) / float(sr),
            },
            duration_ms=_ms(started),
        )

    def calculate_ducking_envelope(
        self,
        narration_path: str | Path,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build smoothed music volume multipliers from narration RMS."""
        started = time.perf_counter()
        cfg = self._merge_settings(settings)
        if not Path(narration_path).exists():
            return self._err(f"Narration audio not found: {narration_path}", started)
        narr, sr = self._read_audio(narration_path)
        if sr <= 0:
            return self._err("Invalid sample rate (<=0) in narration audio", started)
        mono = narr.mean(axis=1) if narr.ndim == 2 else narr
        speech_mask = self._speech_mask(mono, sr, cfg)
        speech_mask = self._fill_short_silences(speech_mask, sr, cfg)
        depth = float(cfg["ducking_depth"])
        ceiling = float(cfg["ducking_ceiling"])
        envelope = np.where(speech_mask > 0, depth, ceiling).astype(np.float64)
        smoothed = self._smooth_envelope(
            envelope,
            int(cfg["attack_time"] * sr),
            int(cfg["release_time"] * sr),
        )
        return self.make_response(
            True,
            {
                "envelope": smoothed,
                "sample_rate": sr,
                "speech_ratio": float(np.mean(speech_mask)),
            },
            duration_ms=_ms(started),
        )

    def mix_tracks(
        self,
        narration_path: str | Path,
        music_path: Optional[str | Path],
        sfx_list: Sequence[Dict[str, Any]],
        ambient_path: Optional[str | Path],
        output_path: str | Path,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Layer narration, ducked music, timed SFX, and ambient."""
        started = time.perf_counter()
        cfg = self._merge_settings(settings)
        if not Path(narration_path).exists():
            return self._err(f"Narration audio not found: {narration_path}", started)
        try:
            narr, sr = self._read_audio(narration_path)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Failed to load narration: {exc}", started)
        if sr <= 0:
            return self._err("Invalid sample rate (<=0) in narration audio", started)
        mix = narr.astype(np.float64)
        if music_path and Path(music_path).exists():
            music, msr = self._read_audio(music_path)
            if msr != sr:
                music = self._resample_np(music, msr, sr)
            music = self._match_length(music, len(mix))
            music_vol = float(cfg.get("music_volume", 1.0))
            mix = mix + music * music_vol
        if ambient_path and Path(ambient_path).exists():
            amb, asr = self._read_audio(ambient_path)
            if asr != sr:
                amb = self._resample_np(amb, asr, sr)
            amb = self._loop_to_length(amb, len(mix))
            amb_vol = float(cfg.get("ambient_volume", 0.2))
            mix = mix + amb * amb_vol
        mix = self._overlay_sfx(mix, sr, sfx_list, float(cfg.get("sfx_volume", 0.7)))
        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        if peak > 0.95:
            mix = mix * (0.95 / peak)
        self._write_audio(output_path, mix, sr)
        return self.make_response(
            True,
            {
                "audio_path": str(output_path),
                "duration": len(mix) / float(sr),
                "peak": peak,
                "sample_rate": sr,
            },
            duration_ms=_ms(started),
        )

    def normalize_to_lufs(
        self,
        audio_path: str | Path,
        output_path: str | Path,
        target_lufs: float = -14.0,
    ) -> Dict[str, Any]:
        """Two-pass FFmpeg loudnorm when available; RMS fallback otherwise."""
        started = time.perf_counter()
        src = Path(audio_path)
        if not src.exists():
            return self._err(f"Audio not found: {src}", started)
        ffmpeg = self._find_ffmpeg()
        if ffmpeg is None:
            return self._normalize_rms_fallback(src, output_path, target_lufs, started)
        measured = self._loudnorm_measure(ffmpeg, src, target_lufs)
        if measured is None:
            return self._normalize_rms_fallback(src, output_path, target_lufs, started)
        ok = self._loudnorm_apply(ffmpeg, src, Path(output_path), target_lufs, measured)
        if not ok:
            return self._normalize_rms_fallback(src, output_path, target_lufs, started)
        return self.make_response(
            True,
            {
                "audio_path": str(output_path),
                "target_lufs": target_lufs,
                "measured": measured,
                "two_pass": True,
                "ffmpeg": True,
            },
            duration_ms=_ms(started),
        )

    def apply_limiter(
        self,
        audio_path: str | Path,
        output_path: str | Path,
        ceiling_db: float = -1.0,
    ) -> Dict[str, Any]:
        """Single-stage peak limiter with ceiling (default -1 dBTP).

        PHASE 7 (loudness): this is the ONE limiter in the render chain
        (generate_final_mix calls it exactly once, after mixing and
        before LUFS normalize) — never chain multiple limiters, since
        each additional stage only adds pumping/distortion risk for no
        audible benefit once the first stage has already guaranteed the
        ceiling.
        """
        started = time.perf_counter()
        if not Path(audio_path).exists():
            return self._err(f"Audio not found: {audio_path}", started)
        data, sr = self._read_audio(audio_path)
        if sr <= 0:
            return self._err("Invalid sample rate (<=0) for limiter input", started)
        ceiling = 10 ** (ceiling_db / 20.0)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak > ceiling and peak > 0:
            data = data * (ceiling / peak)
        # Safety net: uniform gain reduction above always removes clipping
        # for the common case, but a final hard clip guarantees the
        # HARD RULE (never introduce clipping) holds even for the
        # pathological inputs (e.g. a stray sample right at the float
        # boundary after resampling/rounding) the gain-only path can't
        # fully rule out.
        data = np.clip(data, -1.0, 1.0)
        self._write_audio(output_path, data, sr)
        new_peak = float(np.max(np.abs(data))) if data.size else 0.0
        return self.make_response(
            True,
            {
                "audio_path": str(output_path),
                "peak_before": peak,
                "peak_after": new_peak,
                "ceiling": ceiling,
                "clipping": bool(new_peak > 1.0 + 1e-6),
            },
            duration_ms=_ms(started),
        )

    def detect_and_repair_clicks(
        self,
        audio_path: str | Path,
        output_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        """Detect abrupt sample-to-sample discontinuities and repair them.

        PHASE 3 (click removal): a whole-track safety net that runs on
        any assembled audio file (narration track, final mix) — not just
        the individual TTS lines TTSEngineManager already fades/repairs
        before assembly. Multiple faded/repaired lines can still leave a
        residual click at a join, pause splice, or effects-chain seam
        once concatenated, so this re-checks the FULL track using the
        same physically-grounded detector: a sample-to-sample jump that
        is both large in absolute terms and a large multiple of the
        local (windowed) typical jump is the click fingerprint — real
        speech/music transients scale with the signal and never produce
        a true instantaneous jump of that scale. Detected spikes are
        repaired with a short linear interpolation across the gap.

        Args:
            audio_path: Source WAV to scan.
            output_path: Where to write the repaired audio. Defaults to
                overwriting ``audio_path`` in place.

        Returns:
            Standard response with ``clicks_detected`` and
            ``clicks_repaired`` counts. Never modifies the file when no
            clicks are found (still writes to ``output_path`` if that
            differs from the source, for a consistent calling contract).
        """
        started = time.perf_counter()
        if not Path(audio_path).exists():
            return self._err(f"Audio not found: {audio_path}", started)
        try:
            data, sr = self._read_audio(audio_path)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Failed to load audio: {exc}", started)
        if sr <= 0:
            return self._err("Invalid sample rate (<=0) in audio", started)

        repaired, click_count = self._repair_click_spikes(data)
        dest = Path(output_path) if output_path is not None else Path(audio_path)
        if click_count > 0 or dest != Path(audio_path):
            self._write_audio(dest, repaired, sr)
        return self.make_response(
            True,
            {
                "audio_path": str(dest),
                "clicks_detected": click_count,
                "clicks_repaired": click_count,
            },
            duration_ms=_ms(started),
        )

    def _repair_click_spikes(self, data: np.ndarray) -> Tuple[np.ndarray, int]:
        """Interpolate over abrupt per-channel discontinuities.

        Shared detector logic with
        TTSEngineManager._repair_clicks (modules don't cross-import in
        this codebase — Rule A — so this is kept in sync deliberately).
        Returns ``(repaired_array, spike_count)``.
        """
        if data.size == 0:
            return data, 0
        # PHASE 8 (rendering & export optimization): this pass runs on
        # every assembled track and on the final mix, where it almost
        # always finds nothing. Detect first on a read-only view and only
        # pay for the full-buffer copy when there is actually something
        # to repair — the detection maths and the repair itself are
        # unchanged.
        source = data if data.ndim == 2 else data.reshape(-1, 1)
        channels = source.shape[1]
        detected: List[Tuple[int, np.ndarray]] = []
        total_spikes = 0
        for ch in range(channels):
            probe = source[:, ch]
            if probe.size < 8:
                continue
            diffs = np.abs(np.diff(probe))
            if diffs.size == 0:
                continue
            med = float(np.median(diffs)) + 1e-9
            spike = (diffs > 0.25) & (diffs > med * 12.0)
            found = np.flatnonzero(spike)
            if found.size:
                detected.append((ch, found))
                total_spikes += int(found.size)
        if not detected:
            return data, 0
        work = np.array(data, dtype=np.float64, copy=True)
        cols = work.reshape(-1, 1) if work.ndim == 1 else work
        for ch, idx in detected:
            col = cols[:, ch]
            for i in idx:
                lo = max(0, i - 1)
                hi = min(col.size - 1, i + 2)
                if hi > lo:
                    col[lo : hi + 1] = np.linspace(col[lo], col[hi], hi - lo + 1)
        return (cols.reshape(-1) if data.ndim == 1 else cols), total_spikes

    def detect_silence_regions(
        self,
        audio_path: str | Path,
        threshold_db: float = -40.0,
        min_duration: float = 0.1,
    ) -> Dict[str, Any]:
        """Return silence regions as (start, end) second tuples."""
        started = time.perf_counter()
        if not Path(audio_path).exists():
            return self._err(f"Audio not found: {audio_path}", started)
        data, sr = self._read_audio(audio_path)
        if sr <= 0:
            return self._err("Invalid sample rate (<=0) in audio", started)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        # Convert threshold_db to linear RMS-ish amplitude
        thresh = 10 ** (threshold_db / 20.0)
        # PHASE 8 (rendering & export optimization): same window grid and
        # same threshold comparison as before, evaluated in bulk instead
        # of one Python iteration per 10ms frame.
        window = max(1, int(0.02 * sr))
        hop = max(1, int(0.01 * sr))
        total = len(mono)
        silent = np.zeros(total, dtype=bool)
        starts = np.arange(0, max(1, total - window), hop)
        if starts.size:
            complete = int(np.count_nonzero(starts + window <= total))
            rms = np.empty(starts.size, dtype=np.float64)
            if complete:
                rms[:complete] = self._windowed_rms(mono, window, hop, complete)
            for k in range(complete, starts.size):
                frame = mono[starts[k] : starts[k] + window]
                rms[k] = float(np.sqrt(np.mean(frame**2))) if frame.size else 0.0
            quiet = np.flatnonzero(rms < thresh)
            if quiet.size:
                begins = starts[quiet]
                ends = np.minimum(begins + window, total)
                deltas = np.zeros(total + 1, dtype=np.int32)
                np.add.at(deltas, begins, 1)
                np.add.at(deltas, ends, -1)
                silent = np.cumsum(deltas[:-1]) > 0
        regions = self._bool_runs_to_regions(silent, sr, min_duration)
        return self.make_response(
            True,
            {"regions": regions, "count": len(regions), "threshold_db": threshold_db},
            duration_ms=_ms(started),
        )

    def trim_leading_silence(
        self,
        audio_path: str | Path,
        output_path: Optional[str | Path] = None,
        threshold_db: float = -40.0,
        margin_ms: float = 60.0,
        min_excess_ms: float = 150.0,
    ) -> Dict[str, Any]:
        """Trim excessive leading silence without cutting the first phoneme.

        PHASE 4 (remove hard intro): a hard/abrupt narration start is
        almost always excess dead air left over from TTS synthesis, not
        an intentional pause — this removes it while guaranteeing the
        first phoneme is never clipped:

          * ``margin_ms`` (default 60ms) is always kept immediately
            before the first sample louder than ``threshold_db`` — a
            breath-safe cushion so a soft consonant onset or intake of
            breath right before speech starts is never cut into.
          * Only trims when the detected silence is "excessive" (more
            than ``min_excess_ms`` beyond that margin) — normal,
            already-tight TTS output (or audio that opens with a
            deliberate short pause) is left completely untouched.

        Args:
            audio_path: Source WAV to scan.
            output_path: Where to write the trimmed audio. Defaults to
                overwriting ``audio_path`` in place.
            threshold_db: Level below which audio counts as silence
                (same convention as detect_silence_regions).
            margin_ms: Breath-safe cushion kept before the first
                detected sound.
            min_excess_ms: Minimum silence-beyond-the-margin required
                before any trim happens at all.

        Returns:
            Standard response with ``trimmed_ms`` (0.0 when nothing was
            trimmed) and the resulting ``duration``. Never raises; on
            any internal failure the source is left completely
            untouched and ``trimmed_ms`` is 0.0.
        """
        started = time.perf_counter()
        if not Path(audio_path).exists():
            return self._err(f"Audio not found: {audio_path}", started)
        try:
            data, sr = self._read_audio(audio_path)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Failed to load audio: {exc}", started)
        if sr <= 0:
            return self._err("Invalid sample rate (<=0) in audio", started)
        dest = Path(output_path) if output_path is not None else Path(audio_path)
        if data.size == 0:
            if dest != Path(audio_path):
                self._write_audio(dest, data, sr)
            return self.make_response(
                True,
                {"audio_path": str(dest), "trimmed_ms": 0.0, "duration": 0.0},
                duration_ms=_ms(started),
            )

        mono = data.mean(axis=1) if data.ndim == 2 else data
        threshold = 10 ** (threshold_db / 20.0)
        above = np.flatnonzero(np.abs(mono) > threshold)
        if above.size == 0:
            # Entirely silent clip — nothing safe to trim toward; leave
            # it alone rather than guessing.
            if dest != Path(audio_path):
                self._write_audio(dest, data, sr)
            return self.make_response(
                True,
                {
                    "audio_path": str(dest),
                    "trimmed_ms": 0.0,
                    "duration": len(data) / float(sr),
                },
                duration_ms=_ms(started),
            )

        first_sound = int(above[0])
        margin_samples = int((margin_ms / 1000.0) * sr)
        cut_at = max(0, first_sound - margin_samples)
        min_excess_samples = int((min_excess_ms / 1000.0) * sr)
        if cut_at < min_excess_samples:
            # Not excessive — leave the file completely untouched.
            if dest != Path(audio_path):
                self._write_audio(dest, data, sr)
            return self.make_response(
                True,
                {
                    "audio_path": str(dest),
                    "trimmed_ms": 0.0,
                    "duration": len(data) / float(sr),
                },
                duration_ms=_ms(started),
            )

        trimmed = data[cut_at:]
        self._write_audio(dest, trimmed, sr)
        return self.make_response(
            True,
            {
                "audio_path": str(dest),
                "trimmed_ms": (cut_at / float(sr)) * 1000.0,
                "duration": len(trimmed) / float(sr),
            },
            duration_ms=_ms(started),
        )

    def measure_peak_db(self, audio_path: str | Path) -> float:
        """Return peak level in dBFS. Never raises — returns -120.0 on error."""
        try:
            data, _ = self._read_audio(audio_path)
        except Exception:  # noqa: BLE001 - defensive: bad file, never crash caller
            return -120.0
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak <= 0:
            return -120.0
        return 20.0 * math.log10(peak)

    def measure_approx_lufs(self, audio_path: str | Path) -> float:
        """Rough integrated loudness estimate from RMS (not true ITU BS.1770).

        Never raises — returns -70.0 on error (silent/unreadable input).
        """
        try:
            data, _ = self._read_audio(audio_path)
        except Exception:  # noqa: BLE001 - defensive: bad file, never crash caller
            return -70.0
        mono = data.mean(axis=1) if data.ndim == 2 else data
        rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
        if rms <= 0:
            return -70.0
        # Empirical offset so sine ~ -3 dBFS peak tracks near -14 after normalize
        return 20.0 * math.log10(rms) + 0.0


    def crossfade_join(
        self,
        path_a: str | Path,
        path_b: str | Path,
        output_path: str | Path,
        crossfade_ms: int = 50,
    ) -> Dict[str, Any]:
        """Join two audio files with an equal-power crossfade (no click).

        PHASE 3 (click & transition refinement): the join point at the
        end of ``a`` is snapped to the nearest true zero crossing before
        crossfading (removing any residual discontinuity right where the
        two clips actually meet), and the crossfade itself uses an
        equal-power curve instead of pydub's default linear one, so the
        transition doesn't dip in perceived loudness partway through.
        Falls back to a plain (non-equal-power) pydub crossfade if the
        equal-power path can't run for any reason — never raises, never
        produces a worse result than the previous implementation.
        """
        started = time.perf_counter()
        if not Path(path_a).exists():
            return self._err(f"Audio not found: {path_a}", started)
        if not Path(path_b).exists():
            return self._err(f"Audio not found: {path_b}", started)
        try:
            from pydub import AudioSegment
        except ImportError:
            return self._err("pydub not installed", started)
        a = self._ensure_stereo_48k(AudioSegment.from_file(str(path_a)))
        b = self._ensure_stereo_48k(AudioSegment.from_file(str(path_b)))
        fade_ms = max(1, min(int(crossfade_ms), len(a), len(b)))
        snap_ms = self._nearest_zero_crossing_ms(a, len(a))
        if 0 < snap_ms < len(a):
            a = a[:snap_ms]
            fade_ms = max(1, min(fade_ms, len(a), len(b)))
        joined = self._equal_power_crossfade(a, b, fade_ms)
        if not self._pydub_segment_is_valid(joined):
            self.log.warning(
                "Equal-power crossfade produced invalid audio for %s + %s "
                "— falling back to plain crossfade join",
                path_a, path_b,
            )
            joined = a.append(b, crossfade=fade_ms)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        joined.export(str(out), format="wav")
        self._decode_cache_drop(out)
        if not self._validate_audio_file(out):
            return self._err(
                f"Crossfade join failed validation after export: {out}", started
            )
        return self.make_response(
            True,
            {"audio_path": str(out), "duration": len(joined) / 1000.0},
            duration_ms=_ms(started),
        )

    def _pydub_segment_is_valid(self, seg: Any) -> bool:
        """PHASE 3: validate a pydub segment after a transition/join.

        Checks the segment is non-empty and — for the common 16-bit PCM
        case — contains only finite values (defends against a NaN/Inf
        leaking in through a numpy round-trip in a crossfade helper).
        Best-effort: an unrecognized sample format is assumed valid
        rather than guessed at.
        """
        try:
            if seg is None or len(seg) <= 0:
                return False
            if seg.sample_width != 2:
                return True
            import numpy as np

            samples = np.frombuffer(seg.raw_data, dtype="<i2")
            if samples.size == 0:
                return False
            return bool(np.isfinite(samples.astype(np.float64)).all())
        except Exception:  # noqa: BLE001 - treat unreadable as invalid
            return False

    def split_paragraph_audio(
        self,
        paragraph_audio_path: str | Path,
        line_breakdown: Sequence[Dict[str, Any]],
        output_dir: str | Path,
        base_name: str = "line",
        output_paths: Optional[Sequence[str | Path]] = None,
    ) -> Dict[str, Any]:
        """Split one paragraph WAV back into per-line WAV files.

        PHASE 5 (natural narration / paragraph-based TTS / backward
        compatibility): paragraph mode synthesizes several dialogue
        lines as ONE continuous clip for more natural flow, but every
        downstream consumer (the UI's dialogue grid, SFX auto-placement,
        the quality checker, batch re-render) still expects one WAV
        file per ``dialogue_lines`` row via ``audio_file_path`` — this
        recovers that exact per-line file layout from a paragraph clip
        and TTSEngineManager.generate_paragraph_audio's ``line_breakdown``
        (each entry's ``start``/``end`` are already the exact,
        non-approximate boundaries — see that method's docstring).

        Each split is taken with a small symmetric margin so the cut
        doesn't land exactly on a word boundary (which risks clipping a
        trailing consonant) and is re-faded at both edges — the same
        click-safety guarantee every other audio boundary in this
        pipeline already gets (Phases 1 & 3).

        Args:
            paragraph_audio_path: The full paragraph WAV.
            line_breakdown: ``generate_paragraph_audio``'s per-line
                breakdown list (``{"start": ..., "end": ..., ...}``,
                seconds, paragraph-relative).
            output_dir: Directory to write the per-line WAV files into
                (used only when ``output_paths`` is not given).
            base_name: Filename prefix; files are named
                ``{base_name}_{index:03d}.wav`` (used only when
                ``output_paths`` is not given).
            output_paths: Optional explicit destination path per line
                (same length/order as ``line_breakdown``) — lets a
                caller reuse its own existing naming convention (e.g.
                core_engine's ``line_{index:03d}.wav``) instead of this
                method's default.

        Returns:
            Standard response with ``line_paths`` (list of str, same
            order/length as ``line_breakdown``) and ``durations`` (per
            file, seconds). Never raises: any internal failure returns
            a recoverable error response instead of a corrupted split.
        """
        started = time.perf_counter()
        src = Path(paragraph_audio_path)
        if not src.exists():
            return self._err(f"Paragraph audio not found: {src}", started)
        if not line_breakdown:
            return self._err("No line breakdown provided to split", started)
        if output_paths is not None and len(output_paths) != len(line_breakdown):
            return self._err(
                "output_paths length must match line_breakdown length", started
            )
        try:
            data, sr = self._read_audio(src)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"Failed to load paragraph audio: {exc}", started)
        if sr <= 0 or data.size == 0:
            return self._err("Invalid paragraph audio (empty or bad sample rate)", started)

        total_samples = len(data)
        total_duration = total_samples / float(sr)
        out_dir = Path(output_dir)
        if output_paths is None:
            out_dir.mkdir(parents=True, exist_ok=True)

        margin_s = 0.03  # 30ms symmetric margin around each word boundary
        line_paths: List[str] = []
        durations: List[float] = []
        clip_start_offsets: List[float] = []
        for index, entry in enumerate(line_breakdown):
            raw_start = float(entry.get("start", 0.0))
            raw_end = float(entry.get("end", raw_start))
            # First/last line get the true clip start/end (no margin
            # trimmed off the very beginning/end of the paragraph —
            # that boundary already went through the normal per-line
            # fade/trim safety passes at synthesis time).
            start_s = raw_start if index == 0 else max(0.0, raw_start - margin_s)
            end_s = (
                raw_end
                if index == len(line_breakdown) - 1
                else min(total_duration, raw_end + margin_s)
            )
            start_idx = max(0, min(total_samples, int(start_s * sr)))
            end_idx = max(start_idx, min(total_samples, int(end_s * sr)))
            piece = data[start_idx:end_idx]
            if piece.size == 0:
                # Degenerate slice (e.g. a zero-length line) — write a
                # short silence rather than an empty/invalid file so
                # every expected output path always exists.
                piece = np.zeros(max(1, int(0.05 * sr)), dtype=np.float64)
                if data.ndim == 2:
                    piece = np.stack([piece] * data.shape[1], axis=1)
            piece = self._apply_edge_fades_np(piece, sr)
            if output_paths is not None:
                out_path = Path(output_paths[index])
                out_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                out_path = out_dir / f"{base_name}_{index:03d}.wav"
            self._write_audio(out_path, piece, sr)
            line_paths.append(str(out_path))
            durations.append(len(piece) / float(sr))
            # PHASE 5 (timestamp preservation): the actual paragraph-
            # relative time that sample 0 of THIS split file corresponds
            # to — start_idx/sr, not the caller's raw "start", since the
            # margin above can shift the real clip boundary earlier.
            # Callers rebase this line's word timestamps (which are
            # paragraph-relative) to line-relative by subtracting this
            # exact offset, guaranteeing no drift vs. sentence mode.
            clip_start_offsets.append(start_idx / float(sr))

        return self.make_response(
            True,
            {
                "line_paths": line_paths,
                "durations": durations,
                "sample_rate": sr,
                "clip_start_offsets": clip_start_offsets,
            },
            duration_ms=_ms(started),
        )

    def _apply_edge_fades_np(
        self, data: np.ndarray, sr: int, fade_ms: float = 8.0
    ) -> np.ndarray:
        """Short fade-in/out on a raw numpy buffer (click-safety, Phase 3).

        Mirrors TTSEngineManager._apply_edge_fades's guarantee for the
        numpy domain used here (modules don't cross-import — Rule A).
        """
        n = len(data)
        fade_len = min(int(sr * fade_ms / 1000.0), n // 2)
        if fade_len <= 1:
            return data
        result = data.copy()
        fade_in = np.linspace(0.0, 1.0, fade_len)
        fade_out = np.linspace(1.0, 0.0, fade_len)
        if result.ndim == 2:
            fade_in = fade_in[:, None]
            fade_out = fade_out[:, None]
        result[:fade_len] = result[:fade_len] * fade_in
        result[-fade_len:] = result[-fade_len:] * fade_out
        return result

    # ------------------------------------------------------------------
    # Ducking internals
    # ------------------------------------------------------------------

    def _speech_mask(
        self, mono: np.ndarray, sr: int, cfg: Dict[str, Any]
    ) -> np.ndarray:
        """Binary speech mask from RMS windows.

        PHASE 8 (rendering & export optimization): the per-frame RMS and
        the mask painting are both vectorized. A 60-minute narration is
        ~360,000 frames — as a Python loop that alone took minutes of
        the mix stage. The frame grid, the comparison and the resulting
        mask are unchanged (verified sample-exact against the previous
        loop), so ducking behaves identically.
        """
        window = max(1, int(float(cfg["window_sec"]) * sr))
        hop = max(1, int(float(cfg["hop_sec"]) * sr))
        thr = float(cfg["ducking_threshold"])
        mask = np.zeros(len(mono), dtype=np.float64)
        if len(mono) < window:
            rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
            return np.ones(len(mono)) if rms > thr else mask
        num_frames = max(1, (len(mono) - window) // hop)
        rms = self._windowed_rms(mono, window, hop, num_frames)
        loud = np.flatnonzero(rms > thr)
        if loud.size == 0:
            return mask
        # Paint the union of the loud windows with one prefix sum instead
        # of num_frames overlapping slice assignments.
        starts = loud * hop
        ends = np.minimum(starts + window, len(mono))
        deltas = np.zeros(len(mono) + 1, dtype=np.int32)
        np.add.at(deltas, starts, 1)
        np.add.at(deltas, ends, -1)
        return (np.cumsum(deltas[:-1]) > 0).astype(np.float64)

    @staticmethod
    def _windowed_rms(
        mono: np.ndarray, window: int, hop: int, num_frames: int
    ) -> np.ndarray:
        """RMS of ``num_frames`` overlapping windows, without a Python loop.

        Uses a strided view when NumPy provides one (no copy of the
        signal at all) and falls back to the equivalent explicit loop on
        any NumPy build that doesn't — same values either way.
        """
        try:
            from numpy.lib.stride_tricks import sliding_window_view

            frames = sliding_window_view(mono, window)[: num_frames * hop : hop]
            return np.sqrt(np.mean(frames**2, axis=1))
        except Exception:  # noqa: BLE001 - graceful fallback, never fail a mix
            values = np.empty(num_frames, dtype=np.float64)
            for i in range(num_frames):
                start = i * hop
                values[i] = np.sqrt(np.mean(mono[start : start + window] ** 2))
            return values

    def _fill_short_silences(
        self, mask: np.ndarray, sr: int, cfg: Dict[str, Any]
    ) -> np.ndarray:
        """Ignore silence gaps shorter than min_silence_duration.

        PHASE 8 (rendering & export optimization): silent runs are found
        with one diff instead of a per-sample Python loop over the whole
        narration (millions of iterations on a long project). Identical
        output — only runs strictly shorter than ``min_samples`` are
        filled, including a run that reaches the end of the mask.
        """
        min_samples = int(float(cfg["min_silence_duration"]) * sr)
        out = np.array(mask, dtype=np.float64, copy=True)
        if out.size == 0:
            return out
        silent = out == 0
        edges = np.diff(silent.astype(np.int8))
        starts = np.flatnonzero(edges == 1) + 1
        ends = np.flatnonzero(edges == -1) + 1
        if silent[0]:
            starts = np.concatenate(([0], starts))
        for start, end in zip(starts, ends):
            if end - start < min_samples:
                out[start:end] = 1.0
        if silent[-1] and starts.size and len(out) - starts[-1] < min_samples:
            out[starts[-1]:] = 1.0
        return out

    def _smooth_envelope(
        self, envelope: np.ndarray, attack_samples: int, release_samples: int
    ) -> np.ndarray:
        """One-pole attack/release smoother.

        PHASE 8 (rendering & export optimization): a ducking envelope is
        piecewise constant (it only ever holds the duck depth or the
        ceiling), and over a constant input the recursion has the exact
        closed form ``y[k] = x + (y0 - x) * (1 - alpha)^k`` — so each
        constant run is evaluated in one vectorized step instead of one
        Python iteration per sample (millions per hour of narration).
        The per-run alpha choice is the same comparison as before, and
        the result matches the loop to ~1e-13 (far below the 16-bit
        quantization step, i.e. bit-identical rendered audio). An input
        that is NOT mostly piecewise constant would gain nothing from
        this form, so it keeps using the direct recursion.
        """
        attack_samples = max(1, attack_samples)
        release_samples = max(1, release_samples)
        smoothed = np.array(envelope, dtype=np.float64, copy=True)
        total = len(smoothed)
        if total < 2:
            return smoothed
        bounds = np.concatenate(
            ([0], np.flatnonzero(np.diff(smoothed) != 0) + 1, [total])
        )
        if len(bounds) - 1 > total // 8:
            for i in range(1, total):
                if smoothed[i] < smoothed[i - 1]:
                    alpha = 1.0 / attack_samples
                else:
                    alpha = 1.0 / release_samples
                smoothed[i] = alpha * smoothed[i] + (1.0 - alpha) * smoothed[i - 1]
            return smoothed
        attack_alpha = 1.0 / attack_samples
        release_alpha = 1.0 / release_samples
        previous = float(smoothed[0])
        for run in range(len(bounds) - 1):
            start, end = int(bounds[run]), int(bounds[run + 1])
            target = float(envelope[start])
            first = start if start > 0 else 1
            if first >= end:
                continue
            alpha = attack_alpha if target < previous else release_alpha
            decay = np.cumprod(np.full(end - first, 1.0 - alpha))
            smoothed[first:end] = target + (previous - target) * decay
            previous = float(smoothed[end - 1])
        return smoothed

    def _apply_envelope(self, music: np.ndarray, env: np.ndarray) -> np.ndarray:
        """Multiply music by envelope (mono or stereo)."""
        if music.ndim == 2:
            return music * env[:, np.newaxis]
        return music * env

    # ------------------------------------------------------------------
    # Mix helpers
    # ------------------------------------------------------------------

    def _overlay_sfx(
        self,
        mix: np.ndarray,
        sr: int,
        sfx_list: Sequence[Dict[str, Any]],
        default_vol: float,
    ) -> np.ndarray:
        """Add SFX clips at timestamps (seconds).

        PHASE 3 (click removal): an SFX clip is intentionally percussive
        at its own natural start (a boom/whoosh attack), so no fade is
        applied there. But if a clip runs past the end of the mix buffer
        and gets hard-truncated (``piece = clip[: end - start]``), that
        truncation is NOT natural — it chops the clip off mid-waveform
        and is a real, avoidable click source. A short fade-out is
        applied only in that truncated case.
        """
        out = mix.copy()
        for item in sfx_list:
            path = item.get("path")
            if not path or not Path(path).exists():
                continue
            clip, csr = self._read_audio(path)
            if csr != sr:
                clip = self._resample_np(clip, csr, sr)
            start = int(float(item.get("timestamp", 0.0)) * sr)
            vol = float(item.get("volume", default_vol))
            clip = clip * vol
            end = min(len(out), start + len(clip))
            if start >= len(out) or end <= start:
                continue
            truncated = (end - start) < len(clip)
            piece = clip[: end - start]
            if truncated:
                fade_len = min(int(0.005 * sr), len(piece) // 2)
                if fade_len > 1:
                    ramp = np.linspace(1.0, 0.0, fade_len)
                    if piece.ndim == 2:
                        ramp = ramp[:, None]
                    piece = piece.copy()
                    piece[-fade_len:] = piece[-fade_len:] * ramp
            if out.ndim == 2 and piece.ndim == 1:
                piece = np.stack([piece, piece], axis=1)
            if out.ndim == 1 and piece.ndim == 2:
                piece = piece.mean(axis=1)
            out[start:end] = out[start:end] + piece
        return out

    def _loop_to_length(self, audio: np.ndarray, length: int) -> np.ndarray:
        """Repeat audio to target sample length.

        PHASE 3 (click removal): the final tile/trim to an exact sample
        length almost never lands exactly on the loop's own natural loop
        point, so the last sample is very likely away from zero — same
        hard-cut risk as _match_length, fixed the same way.
        """
        if len(audio) == 0:
            shape = (length, 2) if audio.ndim == 2 else (length,)
            return np.zeros(shape, dtype=np.float64)
        if len(audio) >= length:
            return self._fade_out_tail(audio[:length])
        reps = int(math.ceil(length / len(audio)))
        tiled = np.tile(audio, (reps, 1) if audio.ndim == 2 else reps)
        return self._fade_out_tail(tiled[:length])

    def _loop_audio_to_length(
        self, audio: np.ndarray, length: int, sample_rate: int,
        crossfade_ms: int = 500,
    ) -> np.ndarray:
        """Loop a short track to at least `length` samples, crossfaded.

        Equal-power crossfade at each loop seam avoids an audible click/
        jump at the repeat point — much less noticeable than a hard cut
        back to the start, especially over many repeats in a long video.
        """
        if len(audio) == 0 or len(audio) >= length:
            return audio
        fade_len = min(int(sample_rate * crossfade_ms / 1000), len(audio) // 2)
        if fade_len <= 0:
            # Too short to crossfade meaningfully — plain tile fallback.
            reps = int(np.ceil(length / len(audio)))
            tiled = np.tile(audio, (reps,) + (1,) * (audio.ndim - 1))
            return tiled[:length]

        fade_out = np.cos(np.linspace(0, np.pi / 2, fade_len)) ** 2
        fade_in = np.sin(np.linspace(0, np.pi / 2, fade_len)) ** 2
        if audio.ndim == 2:
            fade_out = fade_out[:, None]
            fade_in = fade_in[:, None]

        result = audio.copy()
        while len(result) < length:
            tail = result[-fade_len:] * fade_out
            head = audio[:fade_len] * fade_in
            crossfaded = tail + head
            result = np.concatenate([result[:-fade_len], crossfaded, audio[fade_len:]])
        return result[:length]

    def _match_length(self, audio: np.ndarray, length: int) -> np.ndarray:
        """Trim or pad audio to length.

        PHASE 3 (click removal): trimming a continuous music/ambient bed
        to fit the narration length is a hard cut — if the sample at the
        cut point isn't near zero, that's an audible click at the very
        end of the program. A short fade-out on the trimmed tail removes
        it; padding with silence is already click-free (zeros).
        """
        if len(audio) == length:
            return audio
        if len(audio) > length:
            trimmed = audio[:length]
            return self._fade_out_tail(trimmed)
        pad = length - len(audio)
        # The boundary between the real audio's last sample and the
        # zero-padding can click too if that last sample isn't already
        # near zero — fade the real audio's own tail before appending
        # silence so the join lands at zero on both sides.
        faded = self._fade_out_tail(audio)
        if audio.ndim == 2:
            return np.vstack([faded, np.zeros((pad, audio.shape[1]))])
        return np.concatenate([faded, np.zeros(pad)])

    def _fade_out_tail(self, audio: np.ndarray, fade_ms: float = 15.0) -> np.ndarray:
        """Apply a short fade-out to the last ``fade_ms`` of ``audio``.

        Best-effort, always-safe helper: no-op on empty/too-short input.
        Assumes SAMPLE_RATE (48kHz) since callers work in that domain
        throughout this module.
        """
        fade_len = min(int(SAMPLE_RATE * fade_ms / 1000.0), len(audio) // 2)
        if fade_len <= 1:
            return audio
        result = audio.copy()
        ramp = np.linspace(1.0, 0.0, fade_len)
        if result.ndim == 2:
            ramp = ramp[:, None]
        result[-fade_len:] = result[-fade_len:] * ramp
        return result

    def _match_length_1d(self, arr: np.ndarray, length: int) -> np.ndarray:
        """Trim/pad 1D array."""
        if len(arr) == length:
            return arr
        if len(arr) > length:
            return arr[:length]
        return np.concatenate(
            [arr, np.full(length - len(arr), arr[-1] if len(arr) else 0.0)]
        )

    # ------------------------------------------------------------------
    # FFmpeg / I/O
    # ------------------------------------------------------------------

    def _loudnorm_measure(
        self, ffmpeg: Path, src: Path, target_lufs: float
    ) -> Optional[Dict[str, str]]:
        """First-pass loudnorm measurement."""
        cmd = [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            f"loudnorm=I={target_lufs}:LRA=11:TP=-1:print_format=json",
            "-f",
            "null",
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
            text = (proc.stderr or "") + (proc.stdout or "")
            return self._parse_loudnorm_json(text)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.warning("loudnorm measure failed: %s", exc)
            return None

    def _loudnorm_apply(
        self,
        ffmpeg: Path,
        src: Path,
        dest: Path,
        target_lufs: float,
        measured: Dict[str, str],
    ) -> bool:
        """Second-pass loudnorm apply."""
        filt = (
            f"loudnorm=I={target_lufs}:LRA=11:TP=-1:"
            f"measured_I={measured.get('input_i', target_lufs)}:"
            f"measured_LRA={measured.get('input_lra', 11)}:"
            f"measured_TP={measured.get('input_tp', -1)}:"
            f"measured_thresh={measured.get('input_thresh', -24)}:"
            f"offset={measured.get('target_offset', 0)}:linear=true"
        )
        cmd = [
            str(ffmpeg),
            "-y",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            filt,
            "-ar",
            str(SAMPLE_RATE),
            str(dest),
        ]
        # PHASE 8: ffmpeg writes this file behind the decode memo's back.
        self._decode_cache_drop(dest)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180, check=False
            )
            return proc.returncode == 0 and dest.exists()
        except (OSError, subprocess.SubprocessError):
            return False

    def _parse_loudnorm_json(self, text: str) -> Optional[Dict[str, str]]:
        """Extract loudnorm JSON block from FFmpeg stderr."""
        start = text.rfind("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return {str(k): str(v) for k, v in data.items()}
        except json.JSONDecodeError:
            return None

    def _normalize_rms_fallback(
        self,
        src: Path,
        output_path: str | Path,
        target_lufs: float,
        started: float,
    ) -> Dict[str, Any]:
        """Approximate loudness normalize without FFmpeg loudnorm."""
        data, sr = self._read_audio(src)
        mono = data.mean(axis=1) if data.ndim == 2 else data
        rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
        # Map target_lufs roughly to RMS target
        target_rms = 10 ** ((target_lufs + 3.0) / 20.0)
        if rms > 1e-9:
            data = data * (target_rms / rms)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        ceiling = 10 ** (-1.0 / 20.0)
        if peak > ceiling:
            data = data * (ceiling / peak)
        self._write_audio(output_path, data, sr)
        return self.make_response(
            True,
            {
                "audio_path": str(output_path),
                "target_lufs": target_lufs,
                "two_pass": False,
                "ffmpeg": False,
                "approx_lufs": self.measure_approx_lufs(output_path),
            },
            warnings=[
                "FFmpeg loudnorm unavailable — used RMS approximation (NOT VERIFIED true LUFS)"
            ],
            duration_ms=_ms(started),
        )

    def _find_ffmpeg(self) -> Optional[Path]:
        """Locate ffmpeg binary.

        PHASE 1 (audio stability / effects chain instability fix):
        ``shutil.which("ffmpeg")`` returns ``None`` when ffmpeg isn't on
        PATH — wrapping that in ``Path(None or "")`` produced ``Path("")``,
        which resolves to (and ``.exists()`` reports True for) the current
        working directory. That falsely-valid "ffmpeg" path was then
        handed to ``subprocess.run`` as the executable, which fails with
        a permission/exec error at call time instead of cleanly falling
        back to the no-ffmpeg code path — the actual root cause of the
        "effects chain instability" this phase targets. Every candidate
        is now filtered to a real, named file, not just something that
        happens to satisfy ``.exists()``.
        """
        # PHASE 8 (rendering & export optimization): the resolved binary
        # is remembered while it stays valid, so the loudnorm passes stop
        # re-scanning PATH on every call. Revalidated each time (is_file),
        # so a removed/moved ffmpeg still degrades exactly as before.
        cached = getattr(self, "_ffmpeg_cache", None)
        if cached is not None and cached.is_file():
            return cached
        candidates: List[Path] = []
        try:
            hint = self.config.get("ffmpeg_path")
            if hint:
                candidates.append(Path(str(hint)))
        except Exception:  # noqa: BLE001
            pass
        candidates.append(self._project_root / "engines" / "ffmpeg" / "ffmpeg")
        candidates.append(self._project_root / "engines" / "ffmpeg" / "ffmpeg.exe")
        which = shutil.which("ffmpeg")
        if which:
            candidates.append(Path(which))
        for path in candidates:
            if path and str(path) and path.is_file():
                self._ffmpeg_cache = path
                return path
        return None

    def _read_audio(self, path: str | Path) -> Tuple[np.ndarray, int]:
        """Read WAV/audio as float64 numpy array shape (n,) or (n, ch).

        PHASE 1 (audio stability): every buffer that leaves this function
        is guaranteed finite (no NaN/Inf) and within [-1, 1] — callers
        downstream (ducking, mixing, limiter, LUFS) can assume clean
        input instead of each re-implementing their own defensive check.

        PHASE 8 (rendering & export optimization): decoding is pure, so
        the result is memoized per file identity and reused when the same
        bytes are asked for again (validation gates re-read every stage's
        input and output; the final mix is read four more times for the
        click pass and the peak/LUFS measurements). Buffers handed out
        are read-only — exactly like the ``np.frombuffer`` path below has
        always returned — so a shared buffer can never be mutated by one
        caller behind another's back.
        """
        path = Path(path)
        cached = self._decode_cache_get(path)
        if cached is not None:
            return cached
        try:
            import soundfile as sf

            data, sr = sf.read(str(path), always_2d=False)
            arr = self._sanitize_audio(np.asarray(data, dtype=np.float64))
            arr.setflags(write=False)
            self._decode_cache_put(path, arr, int(sr))
            return arr, int(sr)
        except Exception:  # noqa: BLE001
            pass
        # wave fallback for PCM wav
        with wave.open(str(path), "r") as handle:
            sr = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frames = handle.readframes(handle.getnframes())
        if width == 2:
            arr = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
        elif width == 4:
            arr = np.frombuffer(frames, dtype="<i4").astype(np.float64) / 2147483648.0
        else:
            arr = np.frombuffer(frames, dtype=np.uint8).astype(np.float64)
            arr = (arr - 128.0) / 128.0
        if channels > 1:
            arr = arr.reshape(-1, channels)
        arr = self._sanitize_audio(arr)
        arr.setflags(write=False)
        self._decode_cache_put(path, arr, sr)
        return arr, sr

    def _sanitize_audio(self, data: np.ndarray) -> np.ndarray:
        """Repair or reject a buffer with NaN/Inf/out-of-range samples.

        PHASE 1 (audio stability): a decoder hiccup, a corrupt upstream
        WAV, or an unstable effects chain can all produce NaN/Inf
        samples — left unhandled these propagate through every later
        mix/limiter/LUFS stage as silence, clicks, or a hard crash deep
        in a NumPy reduction. A handful of bad samples (common: one glitch
        frame) is zeroed in place, which is inaudible; a buffer that is
        mostly non-finite is far more likely evidence of a corrupted file
        than "safely repairable audio", so the whole buffer is zeroed to a
        matching-length silent clip instead of shipping garbage.
        """
        if data.size == 0:
            return data
        finite = np.isfinite(data)
        if finite.all():
            return np.clip(data, -1.0, 1.0)
        bad_ratio = 1.0 - (float(np.count_nonzero(finite)) / float(data.size))
        if bad_ratio > _MAX_NONFINITE_RATIO:
            self.log.warning(
                "Audio buffer %.1f%% non-finite (NaN/Inf) — replacing with "
                "silence instead of shipping corrupted samples",
                bad_ratio * 100.0,
            )
            return np.zeros_like(data)
        repaired = np.where(finite, data, 0.0)
        return np.clip(repaired, -1.0, 1.0)

    def _write_audio(self, path: str | Path, data: np.ndarray, sr: int) -> None:
        """Write float audio to WAV.

        PHASE 1 (audio stability): sanitized (NaN/Inf-safe) before the
        final hard clip to [-1, 1] — this is the last line of defense
        before bytes hit disk, so no stage downstream of here can ever
        write a clipped, NaN, or Inf sample into a WAV file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        clipped = np.clip(self._sanitize_audio(data), -1.0, 1.0)
        # PHASE 8: whatever was memoized for this path is now stale.
        self._decode_cache_drop(path)

        def _write(temp: Path) -> None:
            try:
                import soundfile as sf

                sf.write(str(temp), clipped, sr)
                return
            except Exception:  # noqa: BLE001
                safe_unlink(temp)
            pcm = (clipped * 32767.0).astype("<i2")
            channels = 1 if clipped.ndim == 1 else clipped.shape[1]
            with wave.open(str(temp), "w") as handle:
                handle.setnchannels(channels)
                handle.setsampwidth(2)
                handle.setframerate(sr)
                handle.writeframes(pcm.tobytes())

        # PHASE 9: written to a temp file and atomically renamed. The
        # previous in-place write meant a soundfile failure PART WAY
        # through (disk full mid-write) truncated the destination and
        # the `wave` fallback then reopened that same half-written path;
        # both writers now start from a clean temp file and the caller's
        # destination is only touched once a complete file exists.
        if not atomic_write(path, _write):
            raise OSError(f"could not write audio file: {path}")

    def _resample_np(self, data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Simple linear resample."""
        if src_sr == dst_sr or len(data) == 0:
            return data
        duration = len(data) / float(src_sr)
        new_len = max(1, int(duration * dst_sr))
        x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
        if data.ndim == 1:
            return np.interp(x_new, x_old, data)
        channels = [np.interp(x_new, x_old, data[:, c]) for c in range(data.shape[1])]
        return np.stack(channels, axis=1)

    def _ensure_stereo_48k(self, seg: Any) -> Any:
        """Normalize pydub segment to stereo 48k."""
        if seg.channels == 1:
            seg = seg.set_channels(2)
        if seg.frame_rate != SAMPLE_RATE:
            seg = seg.set_frame_rate(SAMPLE_RATE)
        return seg

    def _nearest_zero_crossing_ms(
        self, seg: Any, pos_ms: int, search_ms: int = 5
    ) -> int:
        """Snap a millisecond position to the nearest true zero crossing.

        PHASE 3 (click & transition refinement): "near a zero crossing"
        isn't good enough on its own — a join at a sample that's merely
        SMALL but not zero, with a big neighbour on the other side of the
        splice, still produces an audible tick. This scans a small window
        (default +/-5ms, tiny compared to any pause/segment duration) of
        the underlying PCM samples around ``pos_ms`` for the sample
        closest to true zero (a genuine sign-change crossing when one
        exists in the window, otherwise the smallest-magnitude sample),
        and returns the millisecond position to splice at instead.

        Falls back to the original ``pos_ms`` unchanged for anything that
        can't be read as PCM samples (best-effort, never raises).
        """
        try:
            import numpy as np

            width = seg.sample_width
            if width != 2:
                return pos_ms  # only the common 16-bit PCM case is handled
            channels = max(1, seg.channels)
            sr = seg.frame_rate
            total_ms = len(seg)
            pos_ms = max(0, min(total_ms, pos_ms))
            window_ms = max(1, search_ms)
            start_ms = max(0, pos_ms - window_ms)
            end_ms = min(total_ms, pos_ms + window_ms)
            if end_ms <= start_ms:
                return pos_ms
            chunk = seg[start_ms:end_ms]
            samples = np.frombuffer(chunk.raw_data, dtype="<i2")
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            if samples.size < 2:
                return pos_ms

            # Prefer a genuine sign change (true zero crossing); fall back
            # to the smallest-magnitude sample in the window otherwise.
            signs = np.sign(samples)
            crossing_idx = np.flatnonzero(np.diff(signs) != 0)
            if crossing_idx.size > 0:
                # Pick the crossing closest to the original target position.
                target_sample = int((pos_ms - start_ms) / 1000.0 * sr)
                best = crossing_idx[np.argmin(np.abs(crossing_idx - target_sample))]
                chosen = int(best)
            else:
                chosen = int(np.argmin(np.abs(samples)))
            offset_ms = int(round(chosen / float(sr) * 1000.0))
            return max(0, min(total_ms, start_ms + offset_ms))
        except Exception:  # noqa: BLE001 - best-effort, never break a join
            return pos_ms

    def _equal_power_crossfade(self, a: Any, b: Any, crossfade_ms: int) -> Any:
        """Join two pydub segments with an equal-power crossfade.

        PHASE 3 (click & transition refinement): pydub's built-in
        ``AudioSegment.append(..., crossfade=...)`` uses a LINEAR gain
        ramp (see pydub.AudioSegment.fade) — mixing two linear ramps at
        the crossfade midpoint produces a dip in perceived loudness
        (fade to -120dB / from -120dB, summed, is quieter than either
        signal alone at the middle of the fade), which reads as an
        audible "gap" or "zipper" artifact between segments, especially
        on continuous narration. An equal-power (sin/cos, sums to unity
        power throughout) crossfade keeps perceived loudness constant
        across the whole transition — the standard fix for this class of
        artifact. Falls back to pydub's linear crossfade if numpy/pydub
        raw-sample access fails for any reason.
        """
        crossfade_ms = max(1, int(crossfade_ms))
        try:
            import numpy as np

            a2, b2 = self._match_pydub_pair(a, b)
            if crossfade_ms > len(a2) or crossfade_ms > len(b2):
                crossfade_ms = max(1, min(len(a2), len(b2)))
            width = a2.sample_width
            channels = max(1, a2.channels)
            sr = a2.frame_rate
            if width != 2:
                return a2.append(b2, crossfade=crossfade_ms)

            a_head = a2[:-crossfade_ms] if len(a2) > crossfade_ms else a2[:0]
            a_tail = a2[-crossfade_ms:]
            b_tail_seg = b2[crossfade_ms:] if len(b2) > crossfade_ms else b2[:0]
            b_head = b2[:crossfade_ms]

            a_samples = np.frombuffer(a_tail.raw_data, dtype="<i2").astype(np.float64)
            b_samples = np.frombuffer(b_head.raw_data, dtype="<i2").astype(np.float64)
            if channels > 1:
                a_samples = a_samples.reshape(-1, channels)
                b_samples = b_samples.reshape(-1, channels)
            n = min(len(a_samples), len(b_samples))
            if n <= 1:
                return a2.append(b2, crossfade=crossfade_ms)
            a_samples = a_samples[:n]
            b_samples = b_samples[:n]

            t = np.linspace(0.0, np.pi / 2.0, n)
            fade_out = np.cos(t)
            fade_in = np.sin(t)
            if channels > 1:
                fade_out = fade_out[:, None]
                fade_in = fade_in[:, None]

            mixed = a_samples * fade_out + b_samples * fade_in
            mixed = np.clip(mixed, -32768.0, 32767.0).astype("<i2")

            from pydub import AudioSegment

            xf_seg = AudioSegment(
                mixed.tobytes(),
                sample_width=width,
                frame_rate=sr,
                channels=channels,
            )
            return a_head + xf_seg + b_tail_seg
        except Exception:  # noqa: BLE001 - fall back to pydub's own crossfade
            try:
                a2, b2 = self._match_pydub_pair(a, b)
                if crossfade_ms > len(a2) or crossfade_ms > len(b2):
                    crossfade_ms = max(1, min(len(a2), len(b2)))
                return a2.append(b2, crossfade=crossfade_ms)
            except Exception:  # noqa: BLE001 - absolute last resort: hard join
                return a + b

    def _match_pydub_pair(self, a: Any, b: Any) -> Tuple[Any, Any]:
        """Ensure two pydub segments share format before mixing/joining."""
        from pydub import AudioSegment

        try:
            return AudioSegment._sync(a, b)
        except Exception:  # noqa: BLE001
            return self._ensure_stereo_48k(a), self._ensure_stereo_48k(b)

    def _trim_leading_silence_pydub(
        self, seg: Any, silence_thresh_db: float = -40.0, margin_ms: int = 60
    ) -> Any:
        """Trim excessive leading silence from a pydub segment.

        PHASE 4 (remove hard intro): mirrors trim_leading_silence()'s
        numpy implementation (used from build_narration_track, which
        already works with pydub AudioSegment objects at that point —
        round-tripping through disk just to reuse the numpy version
        would be wasteful). Same breath-safe margin before the first
        detected sound, and only trims when the excess silence is more
        than 150ms so normal, already-tight audio is left alone.
        """
        try:
            from pydub.silence import detect_leading_silence
        except ImportError:
            return seg
        if len(seg) == 0:
            return seg
        leading_ms = detect_leading_silence(seg, silence_threshold=silence_thresh_db)
        cut_at = max(0, leading_ms - margin_ms)
        if cut_at < 150:
            return seg
        return seg[cut_at:]

    def _wav_duration(self, path: Path) -> float:
        """WAV duration seconds.

        PHASE 9: the fallback decode can itself fail (the file is not
        audio at all, or is unreadable). Duration is reported in
        responses and used for QA warnings — 0.0 is the honest,
        already-handled answer, whereas raising aborted the mix.
        """
        try:
            with wave.open(str(path), "r") as handle:
                rate = float(handle.getframerate())
                return handle.getnframes() / rate if rate else 0.0
        except Exception:  # noqa: BLE001
            pass
        try:
            data, sr = self._read_audio(path)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Could not measure duration of %s: %s", path, exc)
            return 0.0
        return len(data) / float(sr) if sr else 0.0

    def _bool_runs_to_regions(
        self, silent: np.ndarray, sr: int, min_duration: float
    ) -> List[Tuple[float, float]]:
        """Convert boolean silence mask to time regions.

        PHASE 8 (rendering & export optimization): run boundaries come
        from one diff rather than a per-sample Python loop; the regions
        produced (and their ordering) are unchanged.
        """
        regions: List[Tuple[float, float]] = []
        total = len(silent)
        if total == 0:
            return regions
        min_samples = int(min_duration * sr)
        flags = np.asarray(silent, dtype=bool)
        edges = np.diff(flags.astype(np.int8))
        starts = np.flatnonzero(edges == 1) + 1
        ends = np.flatnonzero(edges == -1) + 1
        if flags[0]:
            starts = np.concatenate(([0], starts))
        if flags[-1]:
            ends = np.concatenate((ends, [total]))
        for start, end in zip(starts, ends):
            if end - start >= min_samples:
                regions.append((start / sr, end / sr))
        return regions

    def _work_dir(self, project_id: str) -> Path:
        """Temp working directory for a project mix."""
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_id)[:32]
        return self._project_root / "temp" / f"mix_{safe}"

    def _merge_settings(self, settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge user settings over ducking defaults."""
        cfg = dict(DEFAULT_DUCKING)
        if settings:
            cfg.update(settings)
        return cfg

    def _err(self, message: str, started: float) -> Dict[str, Any]:
        """Error response helper."""
        return self.make_response(
            False,
            data={
                "error_code": "AUDIO_PROCESSING_ERROR",
                "user_message": message,
                "is_recoverable": True,
            },
            error=message,
            duration_ms=_ms(started),
        )


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)
