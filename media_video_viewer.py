"""Save VIDEO/IMAGE frames and serve browser-compatible, seekable previews."""
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import wave
import zipfile
from collections import OrderedDict
from fractions import Fraction

import numpy as np
import torch
from PIL import Image

import folder_paths
from comfy_api.latest import io, VideoComponents, VideoFromComponents
from aiohttp import web


FORMATS = {
    "mp4 H264": ("mp4", "libx264", None),
    "webm VP9": ("webm", "libvpx-vp9", None),
    "MOV ProRes Proxy": ("mov", "prores_ks", "0"),
    "MOV ProRes LT": ("mov", "prores_ks", "1"),
    "MOV ProRes 422HQ": ("mov", "prores_ks", "3"),
    "MOV ProRes 4444": ("mov", "prores_ks", "4"),
    "animated webp": ("webp", None, None),
    "animated gif": ("gif", None, None),
    "PNG sequence": ("png", None, None),
    "EXR sequence": ("exr", None, None),
}
_PREVIEWS = OrderedDict()


def _roots():
    paths = [folder_paths.get_output_directory(), *filter(None, os.environ.get("MEDIA_VIDEO_OUTPUT_ROOTS", "").split(os.pathsep))]
    return {"output" if i == 0 else f"extra-{i - 1}": os.path.realpath(path) for i, path in enumerate(paths)}


def _folder(root_id, relative):
    roots = _roots()
    if root_id not in roots:
        raise ValueError("Unknown video output root")
    if not isinstance(relative, str) or os.path.isabs(relative) or "\\" in relative or "\x00" in relative:
        raise ValueError("Invalid output subfolder")
    root = roots[root_id]
    target = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath((root, target)) != root:
        raise ValueError("Output subfolder escapes its allowed root")
    return target


def _token(path):
    token = secrets.token_urlsafe(24)
    _PREVIEWS[token] = os.path.realpath(path)
    while len(_PREVIEWS) > 128:
        _PREVIEWS.popitem(last=False)
    return f"/media-video/preview/{token}"


async def media_video_dirs(request):
    try:
        roots = _roots()
        root_id = request.query.get("root", "output")
        relative = request.query.get("path", "")
        directory = _folder(root_id, relative)
        if not os.path.isdir(directory):
            raise ValueError("Output directory does not exist")
        dirs = sorted(name for name in os.listdir(directory)
                      if os.path.isdir(os.path.join(directory, name))
                      and os.path.commonpath((roots[root_id], os.path.realpath(os.path.join(directory, name)))) == roots[root_id])
        return web.json_response({"roots": [{"id": key, "label": path} for key, path in roots.items()],
                                  "path": relative, "dirs": dirs})
    except (ValueError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def media_video_preview(request):
    path = _PREVIEWS.get(request.match_info["token"])
    if not path or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


try:
    from server import PromptServer

    PromptServer.instance.routes.get("/media-video/dirs")(media_video_dirs)
    PromptServer.instance.routes.get("/media-video/preview/{token}")(media_video_preview)
except (ImportError, AttributeError):
    # Tests import the node without ComfyUI's running PromptServer.
    pass


def _frames(media, fps):
    if isinstance(media, torch.Tensor):
        images, audio = media, None
        rate = fps or 25.0
    else:
        components = media.get_components()
        images, audio = components.images, components.audio
        rate = fps or float(components.frame_rate)
    if images.ndim != 4 or images.shape[-1] not in (3, 4) or not len(images):
        raise ValueError("Video Viewer needs a nonempty RGB/RGBA VIDEO or IMAGE batch")
    rate = float(rate)
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Video Viewer fps must be finite and positive")
    return images, audio, rate


def _rgb(frame, alpha=False):
    channels = 4 if alpha and frame.shape[-1] == 4 else 3
    return (frame[..., :channels].detach().clamp(0, 1) * 255).round().byte().cpu().numpy()


def _run_ffmpeg_pipe(command, feed):
    """Pipe frames from feed() into ffmpeg stdin; raise with stderr on failure."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors)
        try:
            feed(proc.stdin)
            proc.stdin.close()
            code = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if code:
            errors.seek(0)
            raise RuntimeError(f"ffmpeg encoding failed: {errors.read()[-1500:].decode(errors='replace')}")


def _encode_exr(images, folder, name, fps):
    # ponytail: embedded cv2 5.0 has no EXR writer even with OPENCV_IO_ENABLE_OPENEXR;
    # ffmpeg exr encoder (gbrpf32le planar float G,B,R) preserves HDR float instead.
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for EXR sequences")
    height, width = images.shape[1:3]
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "rawvideo", "-pixel_format", "gbrpf32le",
               "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", "pipe:0",
               "-c:v", "exr", "-start_number", "1", os.path.join(folder, f"{name}.%04d.exr")]

    def feed(pipe):
        for frame in images:
            rgb = np.nan_to_num(frame[..., :3].detach().float().cpu().numpy(),
                                nan=0.0, posinf=65504.0, neginf=-65504.0)
            pipe.write(rgb[..., [1, 0, 2]].transpose(2, 0, 1).astype("<f4").tobytes())

    _run_ffmpeg_pipe(command, feed)


def _encode(images, path, fmt, fps, audio=None):
    ext, codec, profile = FORMATS[fmt]
    if ext in ("gif", "webp"):
        frames = [Image.fromarray(_rgb(frame)) for frame in images]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=max(1, round(1000 / fps)), loop=0)
        return
    if ext in ("png", "exr"):
        os.makedirs(path, exist_ok=True)
        name = os.path.basename(path)
        if ext == "png":
            for index, frame in enumerate(images, 1):
                Image.fromarray(_rgb(frame, alpha=True)).save(os.path.join(path, f"{name}.{index:04d}.png"))
        else:
            _encode_exr(images, path, name, fps)
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for video encoding")
    alpha = profile == "4" and images.shape[-1] == 4
    height, width = images.shape[1:3]
    command = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pixel_format", "rgba" if alpha else "rgb24",
               "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", "pipe:0"]
    wav_path = None
    try:
        if audio and ext in ("mp4", "webm", "mov"):
            waveform = audio["waveform"][0].detach().float().clamp(-1, 1).cpu().numpy()
            rate = int(audio["sample_rate"])
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as output:
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(waveform.shape[0])
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes((waveform.T * 32767).astype("<i2").tobytes())
            command += ["-i", wav_path]
        command += ["-c:v", codec]
        if profile is not None:
            # ProRes 4444 keeps alpha when present; RGB-only stays true 4:4:4.
            command += ["-profile:v", profile,
                        "-pix_fmt", ("yuva444p10le" if alpha else "yuv444p10le") if profile == "4" else "yuv422p10le"]
        elif ext == "mp4":
            command += ["-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        else:
            command += ["-crf", "32", "-b:v", "0", "-pix_fmt", "yuv420p"]
        if ext != "mov":
            command += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
        if wav_path:
            command += ["-c:a", {"mp4": "aac", "webm": "libopus", "mov": "pcm_s16le"}[ext],
                        "-t", str(len(images) / fps)]
        command.append(path)
        _run_ffmpeg_pipe(command, lambda pipe: [pipe.write(_rgb(frame, alpha).tobytes()) for frame in images])
    finally:
        if wav_path:
            os.unlink(wav_path)


def _available(folder, stem, extension, sequence=False, versioning=False):
    for number in range(1, 1000000):
        name = f"{stem}_v{number:03d}" if versioning else f"{stem}_{number:05d}"
        path = os.path.join(folder, name if sequence else f"{name}.{extension}")
        if not os.path.lexists(path):
            return path
    raise RuntimeError("No free video output name")


def _write(path, sequence, encode):
    """Reserve path exclusively, encode via callable(target), publish atomically.

    The reservation happens before encoding so concurrent runs can never pick
    the same output name; on failure only our own reservation/part is removed.
    """
    if sequence:
        os.mkdir(path)  # atomic exclusive reservation for the whole sequence dir
        try:
            encode(path)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return path
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    base, ext = os.path.splitext(path)
    part = f"{base}.part{ext}"  # keep real extension so encoders infer the muxer
    try:
        encode(part)
        os.replace(part, path)
    except BaseException:
        for victim in (part, path):
            if os.path.lexists(victim):
                os.unlink(victim)
        raise
    return path


class MediaVideoViewer(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MediaVideoViewer", display_name="Video Viewer/Saver", category="media",
            inputs=[
                io.MultiType.Input("video", [io.Video, io.Image]),
                io.MultiType.Input("reference", [io.Video, io.Image], optional=True),
                io.Audio.Input("audio", optional=True),
                io.Float.Input("fps", default=0.0, min=0.0, max=1000.0, step=0.01),
                io.Boolean.Input("pingpong", default=False),
                io.Combo.Input("format", options=list(FORMATS)),
                io.String.Input("filename_prefix", default="video"),
                io.String.Input("output_root", default="output"),
                io.String.Input("subfolder", default=""),
                io.Boolean.Input("versioning", default=False),
                io.Boolean.Input("save_output", default=True),
            ],
            outputs=[io.Video.Output("video"), io.String.Output("path")],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, video, fps=0.0, pingpong=False, format="mp4 H264", filename_prefix="video",
                output_root="output", subfolder="", versioning=False, save_output=True,
                reference=None, audio=None):
        if format not in FORMATS:
            raise ValueError("Unsupported Video Viewer format")
        images, native_audio, rate = _frames(video, fps)
        if pingpong and len(images) > 2:
            images = torch.cat((images, images[1:-1].flip(0)), dim=0)
        stem = filename_prefix.strip()
        if not stem or stem in (".", "..") or not re.fullmatch(r"[^/\\\x00-\x1f]+", stem):
            raise ValueError("filename_prefix must be a filename, not a path")
        folder = _folder(output_root, subfolder) if save_output else folder_paths.get_temp_directory()
        os.makedirs(folder, exist_ok=True)
        ext = FORMATS[format][0]
        sequence = ext in ("png", "exr")
        resolved_audio = audio if audio is not None else native_audio
        path = _available(folder, stem, ext, sequence, versioning)
        _write(path, sequence, lambda target: _encode(images, target, format, rate, resolved_audio))
        preview_path = path
        if format != "mp4 H264":
            # Browsers need H264 MP4 even when the saved file is MOV/EXR/etc.
            preview_path = _available(folder_paths.get_temp_directory(), "media_video_preview", "mp4")
            _write(preview_path, False, lambda target: _encode(images, target, "mp4 H264", rate, resolved_audio))
        reference_url = None
        if reference is not None:
            ref_images, _, ref_rate = _frames(reference, fps)
            ref_path = _available(folder_paths.get_temp_directory(), "media_video_reference", "mp4")
            _write(ref_path, False, lambda target: _encode(ref_images, target, "mp4 H264", ref_rate))
            reference_url = _token(ref_path)
        # "save copy" works on every run: single files token the render itself
        # (temp when preview-only), sequences always zip into temp regardless.
        download_path = path
        if sequence:
            download_path = _available(folder_paths.get_temp_directory(), "media_video_sequence", "zip")

            def _zip(target):
                with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                    for name in sorted(os.listdir(path)):
                        archive.write(os.path.join(path, name), name)

            _write(download_path, False, _zip)
        download_url = _token(download_path)
        item = {"preview_url": _token(preview_path), "reference_url": reference_url,
                "fps": rate, "frames": len(images), "saved_path": path if save_output else None,
                "download_url": download_url, "download_name": os.path.basename(download_path)}
        out_video = VideoFromComponents(VideoComponents(
            images=images, frame_rate=Fraction(rate).limit_denominator(1000000), audio=resolved_audio))
        return io.NodeOutput(out_video, path, ui={"media_video": (item,)})
