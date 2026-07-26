"""Media nodes: TTS, STT, FFmpeg and the media processing pipeline.

Before M4 workflows could not touch the media subsystem at all (gap I4). These
nodes bridge the engine to the M2 media services, reusing ``resolve_media_path``
so every path stays confined to ``MEDIA_ROOT``.

Honesty note: Creator OS ships **no** speech model. The TTS and STT nodes call
whichever provider has been registered with the AI orchestrator; with none
registered they raise a clear ``PROVIDER`` error instead of returning a
fabricated transcript.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any, Dict, List

from app.core.errors import SecurityError, ValidationError
from app.infrastructure.config.settings import settings
from app.services.workflow.executors import coerce_number, render_template, render_value
from app.services.workflow.runtime import (
    FieldSpec,
    NodeErrorCode,
    NodeExecutionError,
    NodeSchema,
    RuntimeNodeExecutor,
)


class TTSNode(RuntimeNodeExecutor):
    """Synthesises speech from text using a registered TTS provider."""

    label = "Text to Speech"
    category = "media"
    description = "Synthesises speech audio from text via a registered TTS provider."
    aliases = ("tts_node", "text_to_speech", "speech")
    schema = NodeSchema(
        inputs=[
            FieldSpec("text", "string", required=True, description="Text to speak"),
            FieldSpec("voice", "string", description="Provider voice identifier"),
            FieldSpec("model", "string"),
            FieldSpec("provider", "string"),
            FieldSpec("language", "string", default="en"),
            FieldSpec(
                "output_path",
                "string",
                description="Destination under MEDIA_ROOT (auto-named when blank)",
            ),
            FieldSpec("speed", "number", minimum=0.25, maximum=4.0, default=1.0),
        ],
        outputs=[
            FieldSpec("audio_path", "string", description="Path relative to MEDIA_ROOT"),
            FieldSpec("duration_seconds", "number"),
            FieldSpec("provider", "string"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        from app.services.ai.orchestrator import ai_orchestrator

        text = render_template(str(config.get("text") or ""), context).strip()
        if not text:
            raise ValidationError("TTS node requires non-empty 'text'.")

        provider_name = str(config.get("provider") or "").strip() or None
        provider = ai_orchestrator.get_speech_provider("tts", provider_name)
        if provider is None:
            raise NodeExecutionError(
                "No text-to-speech provider is configured. Register one via "
                "ai_orchestrator.register_speech_provider('tts', ...) to enable "
                "this node.",
                code=NodeErrorCode.PROVIDER,
                details={"requested_provider": provider_name},
            )

        output_path = render_template(
            str(config.get("output_path") or ""), context
        ).strip() or None

        result = await provider.synthesize(
            text=text,
            voice=config.get("voice"),
            model=config.get("model"),
            language=config.get("language") or "en",
            speed=float(config.get("speed") or 1.0),
            output_path=output_path,
        )
        if not isinstance(result, dict):
            result = {"audio_path": result}
        result.setdefault("provider", provider_name or getattr(provider, "name", "custom"))
        result.setdefault("text_length", len(text))
        return result


class STTNode(RuntimeNodeExecutor):
    """Transcribes an audio file using a registered STT provider."""

    label = "Speech to Text"
    category = "media"
    description = "Transcribes audio to text via a registered STT provider."
    aliases = ("stt_node", "speech_to_text", "transcribe")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "audio_path",
                "string",
                required=True,
                description="Audio file relative to MEDIA_ROOT",
            ),
            FieldSpec("model", "string"),
            FieldSpec("provider", "string"),
            FieldSpec("language", "string", description="Blank = auto-detect"),
        ],
        outputs=[
            FieldSpec("text", "string", description="Transcript"),
            FieldSpec("language", "string"),
            FieldSpec("segments", "array"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        from app.services.ai.orchestrator import ai_orchestrator
        from app.services.media.storage import resolve_media_path

        relative = render_template(str(config.get("audio_path") or ""), context).strip()
        if not relative:
            raise ValidationError("STT node requires an 'audio_path'.")

        # Validates containment and existence before involving the provider.
        path = await asyncio.to_thread(resolve_media_path, relative, must_exist=True)

        provider_name = str(config.get("provider") or "").strip() or None
        provider = ai_orchestrator.get_speech_provider("stt", provider_name)
        if provider is None:
            raise NodeExecutionError(
                "No speech-to-text provider is configured. Register one via "
                "ai_orchestrator.register_speech_provider('stt', ...) to enable "
                "this node.",
                code=NodeErrorCode.PROVIDER,
                details={"requested_provider": provider_name},
            )

        result = await provider.transcribe(
            audio_path=str(path),
            model=config.get("model"),
            language=config.get("language") or None,
        )
        if not isinstance(result, dict):
            result = {"text": str(result)}
        result.setdefault("provider", provider_name or getattr(provider, "name", "custom"))
        result.setdefault("source_path", relative)
        return result


class FFmpegNode(RuntimeNodeExecutor):
    """Runs a constrained FFmpeg transformation.

    The node never accepts a raw command line. Input and output paths are
    resolved inside ``MEDIA_ROOT`` and extra arguments are parsed with
    ``shlex`` and screened, so a workflow cannot use ``-i /etc/passwd`` or
    redirect output outside the sandbox.
    """

    label = "FFmpeg"
    category = "media"
    description = "Transcodes or transforms media with FFmpeg (sandboxed paths)."
    aliases = ("ffmpeg_node", "transcode")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "input_path", "string", required=True,
                description="Source file relative to MEDIA_ROOT",
            ),
            FieldSpec(
                "output_path", "string", required=True,
                description="Destination relative to MEDIA_ROOT",
            ),
            FieldSpec(
                "operation",
                "string",
                default="transcode",
                enum=["transcode", "extract_audio", "thumbnail", "trim", "custom"],
            ),
            FieldSpec("start", "string", description="Start timestamp for 'trim'"),
            FieldSpec("duration", "string", description="Duration for 'trim'"),
            FieldSpec("video_codec", "string"),
            FieldSpec("audio_codec", "string"),
            FieldSpec("scale", "string", description="e.g. '1280:-2'"),
            FieldSpec(
                "extra_args",
                "string",
                description="Additional FFmpeg flags (path arguments are rejected)",
            ),
            FieldSpec("timeout", "number", minimum=1.0, maximum=3600.0),
        ],
        outputs=[
            FieldSpec("output_path", "string"),
            FieldSpec("size_bytes", "integer"),
            FieldSpec("command", "array"),
        ],
    )

    #: Flags that take a filesystem path and would let a workflow escape the root.
    BLOCKED_ARGS = {"-i", "-f", "-passlogfile", "-attach", "-y", "-n"}

    def _screen_extra_args(self, raw: str) -> List[str]:
        if not raw or not raw.strip():
            return []
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            raise ValidationError(
                f"Could not parse 'extra_args': {exc}"
            ) from exc
        for part in parts:
            lowered = part.lower()
            if lowered in self.BLOCKED_ARGS:
                raise SecurityError(
                    f"FFmpeg argument {part!r} is not permitted here.",
                    details={"argument": part},
                )
            if "/" in part or "\\" in part:
                raise SecurityError(
                    "Path-like values are not permitted in 'extra_args'; use "
                    "'input_path' and 'output_path'.",
                    details={"argument": part[:100]},
                )
        return parts

    def _build_command(
        self, config: Dict[str, Any], source: str, destination: str
    ) -> List[str]:
        operation = str(config.get("operation") or "transcode").lower()
        cmd: List[str] = [settings.FFMPEG_BINARY, "-y", "-hide_banner", "-loglevel", "error"]

        start = str(config.get("start") or "").strip()
        if operation == "trim" and start:
            cmd += ["-ss", start]

        cmd += ["-i", source]

        duration = str(config.get("duration") or "").strip()
        if operation == "trim" and duration:
            cmd += ["-t", duration]

        if operation == "extract_audio":
            cmd += ["-vn"]
            cmd += ["-acodec", str(config.get("audio_codec") or "libmp3lame")]
        elif operation == "thumbnail":
            cmd += ["-frames:v", "1"]
            scale = str(config.get("scale") or "320:-2")
            cmd += ["-vf", f"scale={scale}"]
        else:
            if config.get("video_codec"):
                cmd += ["-c:v", str(config["video_codec"])]
            if config.get("audio_codec"):
                cmd += ["-c:a", str(config["audio_codec"])]
            if config.get("scale"):
                cmd += ["-vf", f"scale={config['scale']}"]

        cmd += self._screen_extra_args(str(config.get("extra_args") or ""))
        cmd.append(destination)
        return cmd

    def _run_sync(self, cmd: List[str], destination_rel: str, timeout: float) -> Dict[str, Any]:
        import subprocess

        from app.services.media.storage import resolve_media_path

        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            raise NodeExecutionError(
                f"FFmpeg binary {settings.FFMPEG_BINARY!r} was not found.",
                code=NodeErrorCode.DISABLED,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise NodeExecutionError(
                f"FFmpeg timed out after {timeout}s.", code=NodeErrorCode.TIMEOUT
            ) from exc

        if completed.returncode != 0:
            raise NodeExecutionError(
                f"FFmpeg exited with code {completed.returncode}: "
                f"{completed.stderr[-800:]}",
                code=NodeErrorCode.RUNTIME,
                details={"exit_code": completed.returncode},
            )

        output = resolve_media_path(destination_rel, must_exist=False)
        return {
            "output_path": destination_rel,
            "size_bytes": output.stat().st_size if output.exists() else 0,
            "command": cmd,
            "stderr": completed.stderr[-2000:],
        }

    async def run(self, node, context, config) -> Any:
        import shutil as _shutil

        from app.services.media.storage import resolve_media_path

        input_rel = render_template(str(config.get("input_path") or ""), context).strip()
        output_rel = render_template(str(config.get("output_path") or ""), context).strip()
        if not input_rel or not output_rel:
            raise ValidationError(
                "FFmpeg node requires both 'input_path' and 'output_path'."
            )

        if not _shutil.which(settings.FFMPEG_BINARY):
            raise NodeExecutionError(
                f"FFmpeg binary {settings.FFMPEG_BINARY!r} is not available on PATH.",
                code=NodeErrorCode.DISABLED,
                details={"binary": settings.FFMPEG_BINARY},
            )

        source = await asyncio.to_thread(resolve_media_path, input_rel, must_exist=True)
        destination = await asyncio.to_thread(
            resolve_media_path, output_rel, must_exist=False
        )
        await asyncio.to_thread(
            lambda: destination.parent.mkdir(parents=True, exist_ok=True)
        )

        cmd = self._build_command(config, str(source), str(destination))
        timeout = coerce_number(config.get("timeout"), settings.FFMPEG_TIMEOUT_SECONDS)
        timeout = max(1.0, min(timeout, 3600.0))

        return await asyncio.to_thread(self._run_sync, cmd, output_rel, timeout)


class MediaProcessingNode(RuntimeNodeExecutor):
    """Runs the M2 media pipeline against an asset (probe, metadata, poster)."""

    label = "Media Processing"
    category = "media"
    description = "Processes a media asset through the pipeline (probe + poster)."
    aliases = ("media_processing_node", "media_process", "process_media")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "asset_id",
                "integer",
                description="Existing media asset id (or supply 'path' to register)",
            ),
            FieldSpec(
                "path",
                "string",
                description="File under MEDIA_ROOT to register then process",
            ),
            FieldSpec(
                "operation", "string", default="process",
                enum=["process", "probe", "metadata"],
            ),
            FieldSpec(
                "wait", "boolean", default=True,
                description="Await pipeline completion before continuing",
            ),
            FieldSpec("timeout", "number", minimum=1.0, maximum=3600.0, default=300.0),
        ],
        outputs=[
            FieldSpec("asset_id", "integer"),
            FieldSpec("job_id", "integer"),
            FieldSpec("status", "string"),
            FieldSpec("result", "object"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        from app.services.media.ffmpeg import probe_media
        from app.services.media.pipeline import media_pipeline

        operation = str(config.get("operation") or "process").lower()
        relative = render_template(str(config.get("path") or ""), context).strip()

        if operation == "probe":
            if not relative:
                raise ValidationError("Media probe requires a 'path'.")
            probe = await asyncio.to_thread(probe_media, relative)
            return {"operation": "probe", "path": relative, "result": probe}

        asset_id = config.get("asset_id")
        if not asset_id:
            raise ValidationError(
                "Media processing requires an 'asset_id' (register the file "
                "via POST /api/media/assets/register first)."
            )
        asset_id = int(asset_id)

        try:
            job = await asyncio.to_thread(media_pipeline.enqueue_asset, asset_id)
        except Exception as exc:
            raise NodeExecutionError(
                f"Failed to enqueue media asset {asset_id}: {exc}",
                code=NodeErrorCode.RUNTIME,
                details={"asset_id": asset_id},
            ) from exc

        job_id = job.id
        if not config.get("wait", True):
            return {
                "asset_id": asset_id,
                "job_id": job_id,
                "status": "PENDING",
                "waited": False,
            }

        timeout = coerce_number(config.get("timeout"), 300.0)
        try:
            await media_pipeline.wait_for_job(job_id, timeout=max(1.0, timeout))
        except asyncio.TimeoutError as exc:
            raise NodeExecutionError(
                f"Media job {job_id} did not finish within {timeout}s.",
                code=NodeErrorCode.TIMEOUT,
                details={"job_id": job_id},
            ) from exc

        final = await asyncio.to_thread(media_pipeline.get_job, job_id)
        status = getattr(final, "status", "UNKNOWN")
        result = getattr(final, "result", None)
        if status == "FAILED":
            raise NodeExecutionError(
                f"Media processing failed: {getattr(final, 'error', 'unknown error')}",
                code=NodeErrorCode.RUNTIME,
                details={"job_id": job_id},
            )
        return {
            "asset_id": asset_id,
            "job_id": job_id,
            "status": status,
            "result": result,
            "waited": True,
        }
