"""Media Load Video/Sequence — ComfyUI V3 custom node.

Loads MOV/MP4 video (upload or local path) or EXR/PNG/TIF/TIFF image-sequence
folders (local path) into an IMAGE tensor plus fps / frame_count / width / height.

Two-pass PyAV decode with a single final allocation: count in pass 1, fill one
preallocated canvas in pass 2 (no per-frame list accumulation).

output_fps is a free positive float (default 25.0), independent of the temporal
preset. Presets NEVER override fps and NEVER resample frames; they only pad the
frame count via last-frame freeze:
  None      -> no temporal padding
  WAN       -> next 4n+1
  LTX 2.3   -> next 8n+1
"""
import os
import re
import math
import asyncio
import secrets
import shutil
import hashlib
import tempfile
import threading
import subprocess
from collections import OrderedDict

import numpy as np
import torch

import folder_paths
from comfy_api.latest import ComfyExtension, io

# ponytail: OpenCV EXR reads require this flag before the first cv2.imread('.exr').
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"

WEB_DIRECTORY = "./web"

_VIDEO_EXTS = (".mov", ".mp4")
_SEQUENCE_EXTS = (".exr", ".png", ".tif", ".tiff")
_8BIT_FORMATS = ("yuvj420p", "yuvj422p", "yuvj444p", "rgb24", "rgba", "pal8")
_HIGHBIT_FORMATS = ("gbrpf32le", "gbrapf32le")
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_PREVIEW_FILES = OrderedDict()
_LIVE_PROBE_CACHE = OrderedDict()
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_MAX_DIM = max(160, int(os.environ.get("MEDIA_LOADER_PREVIEW_MAX_DIM", "640")))
_BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}


def _register_video_source(path, codec):
    entry = (os.path.realpath(path), codec)
    for existing_token, existing_entry in _PREVIEW_FILES.items():
        if existing_entry == entry:
            _PREVIEW_FILES.move_to_end(existing_token)
            return "/media-loader/preview/{}".format(existing_token)
    token = secrets.token_urlsafe(24)
    _PREVIEW_FILES[token] = entry
    while len(_PREVIEW_FILES) > 16:
        _PREVIEW_FILES.popitem(last=False)
    return "/media-loader/preview/{}".format(token)


def _browser_preview_path(path, codec):
    if codec in _BROWSER_CODECS:
        return path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required to preview {} video in browser".format(codec))
    stat = os.stat(path)
    key = hashlib.sha256("v2:{}:{}:{}".format(os.path.realpath(path), stat.st_mtime_ns, stat.st_size).encode()).hexdigest()[:20]
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    output = os.path.join(temp_dir, "media_loader_live_{}.mp4".format(key))
    with _PREVIEW_LOCK:
        if os.path.isfile(output) and os.path.getsize(output) > 0:
            return output
        partial = output + ".part.mp4"
        command = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", path,
                   "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1", "-vf",
                   "scale={0}:{0}:force_original_aspect_ratio=decrease:force_divisible_by=2".format(_PREVIEW_MAX_DIM),
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", partial]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError("preview proxy failed: {}".format(result.stderr.strip()))
            os.replace(partial, output)
        except Exception:
            if os.path.exists(partial):
                os.unlink(partial)
            raise
    return output


try:
    from aiohttp import web as _web
    from server import PromptServer as _PromptServer

    @_PromptServer.instance.routes.get("/media-loader/preview/{token}")
    async def _serve_video_preview(request):
        entry = _PREVIEW_FILES.get(request.match_info["token"])
        if not entry:
            raise _web.HTTPNotFound()
        path, codec = entry
        if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in _VIDEO_EXTS:
            raise _web.HTTPNotFound()
        preview_path = await asyncio.to_thread(_browser_preview_path, path, codec)
        return _web.FileResponse(preview_path, headers={"Cache-Control": "no-store", "Content-Disposition": "inline"})

    @_PromptServer.instance.routes.post("/media-loader/probe")
    async def _probe_media_live(request):
        try:
            return _web.json_response(_live_metadata(await request.json()))
        except (KeyError, TypeError, ValueError, OSError) as e:
            return _web.json_response({"error": str(e)}, status=400)

    @_PromptServer.instance.routes.post("/media-loader/upload")
    async def _upload_media_uncapped(request):
        filename = request.query.get("filename", "")
        if filename != os.path.basename(filename) or os.path.splitext(filename)[1].lower() not in _VIDEO_EXTS:
            return _web.json_response({"error": "filename must be a MOV or MP4 basename"}, status=400)
        input_dir = folder_paths.get_input_directory()
        stem, extension = os.path.splitext(filename)
        target = os.path.join(input_dir, filename)
        suffix = 1
        while os.path.exists(target):
            target = os.path.join(input_dir, "{}_{}{}".format(stem, suffix, extension))
            suffix += 1
        partial = target + ".upload-" + secrets.token_urlsafe(8)
        output = open(partial, "xb")
        written = 0
        try:
            async for chunk in request.content.iter_chunked(8 * 1024 * 1024):
                await asyncio.to_thread(output.write, chunk)
                written += len(chunk)
            await asyncio.to_thread(output.flush)
            await asyncio.to_thread(os.fsync, output.fileno())
            output.close()
            os.replace(partial, target)
        except BaseException:
            output.close()
            if os.path.exists(partial):
                os.unlink(partial)
            raise
        return _web.json_response({"name": os.path.basename(target), "size": written})
except (ImportError, AttributeError):
    # Tests import the node without ComfyUI's running PromptServer.
    pass


# ---------------------------------------------------------------------------
# pure helpers (unit-testable, no media deps)
# ---------------------------------------------------------------------------

def _natural_key(name):
    # re.split(r'(\d+)', s) alternates non-digit/digit by parity, so element
    # types are consistent across names at every index -> safe to compare.
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _normalize_pad_color(value):
    return "#000000" if value == "image" else value


def _hex_to_rgb01(hexstr):
    h = (hexstr or "").lstrip("#")
    return np.array([int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)], dtype=np.float32) / 255.0


def _align_count(count, preset):
    if preset == "WAN":
        return ((count - 2) // 4 + 1) * 4 + 1
    if preset == "LTX 2.3":
        return ((count - 2) // 8 + 1) * 8 + 1
    return count


def _snap_last_frame(first_frame, last_frame, preset):
    if last_frame == -1 or preset == "None":
        return last_frame
    duration = last_frame - first_frame + 1
    return first_frame + _align_count(duration, preset) - 1


def _resolve_frame_range(total_frames, first_frame, last_frame, preset):
    requested_last = total_frames - 1 if last_frame == -1 else last_frame
    if first_frame >= total_frames:
        raise ValueError("first_frame {} is outside source duration {}".format(first_frame, total_frames))
    if preset == "None" and requested_last >= total_frames:
        raise ValueError("last_frame {} is outside source duration {}".format(requested_last, total_frames))
    source_last = min(requested_last, total_frames - 1)
    if source_last < first_frame:
        raise ValueError("last_frame must be -1 or greater than or equal to first_frame")
    selected_count = source_last - first_frame + 1
    requested_count = requested_last - first_frame + 1
    return first_frame, source_last, selected_count, requested_last, requested_count


def _padded_dim(dim, m):
    padded = math.ceil(dim / m) * m
    before = (padded - dim) // 2
    after = padded - dim - before
    return padded, before, after


def _align32(value):
    return ((value + 31) // 32) * 32


def _apply_rotation(img, rotation):
    # mirrors comfy_api video_types rotation: k = round(rotation // 90)
    if rotation:
        k = int(round(rotation // 90))
        return np.rot90(img, k=k, axes=(0, 1)).copy()
    return img


def _normalize_pixels(img, highbit):
    # high-bit (gbr*pf32le) is identity: NO /255, NO clamp (only EXR is clamped).
    if highbit:
        return img.astype(np.float32)
    return img.astype(np.float32) / 255.0


def _composite_alpha(rgb, alpha, pad_rgb):
    return rgb * alpha + pad_rgb * (1.0 - alpha)


# ---------------------------------------------------------------------------
# sequence loading
# ---------------------------------------------------------------------------

def _scan_sequence_dir(path):
    return [f for f in os.listdir(path) if os.path.splitext(f)[1].lower() in _SEQUENCE_EXTS]


def _probe_sequence_uniform(names, base):
    ref = None
    for name in names:
        fp = os.path.join(base, name)
        ext = os.path.splitext(name)[1].lower()
        if ext == ".exr":
            import cv2
            raw = cv2.imread(fp, cv2.IMREAD_UNCHANGED)
            if raw is None:
                raise ValueError("EXR read failed: {}".format(name))
            h, w = raw.shape[0], raw.shape[1]
            c = raw.shape[2] if raw.ndim == 3 else 1
        else:
            from PIL import Image, ImageOps
            with Image.open(fp) as im:
                im = ImageOps.exif_transpose(im)
                w, h = im.size
                c = len(im.getbands())
        cur = (h, w, c)
        if ref is None:
            ref = cur
        elif cur != ref:
            raise ValueError("sequence frame dimension mismatch: {} has {}, expected {}".format(name, cur, ref))
    if ref is None:
        raise ValueError("empty sequence")
    return ref


def _load_sequence_image(fp, pad_rgb):
    ext = os.path.splitext(fp)[1].lower()
    if ext == ".exr":
        import cv2
        raw = cv2.imread(fp, cv2.IMREAD_UNCHANGED)
        if raw is None:
            raise RuntimeError("EXR read failed: {}".format(fp))
        if raw.ndim == 2:
            raw = np.stack([raw, raw, raw], axis=-1)
        raw = raw.astype(np.float32)
        if raw.shape[2] == 4:
            clamped = np.clip(raw, 0.0, 1.0)
            rgb = clamped[..., :3][:, :, ::-1].copy()
            a = clamped[..., 3:4]
            return _composite_alpha(rgb, a, pad_rgb).astype(np.float32)
        rgb = np.clip(raw[..., :3], 0.0, 1.0)[:, :, ::-1].copy()
        return rgb.astype(np.float32)
    from PIL import Image, ImageOps
    with Image.open(fp) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGBA")
    arr = np.asarray(im, dtype=np.float32) / 255.0
    rgb = arr[..., :3]
    a = arr[..., 3:4]
    return _composite_alpha(rgb, a, pad_rgb).astype(np.float32)


# ---------------------------------------------------------------------------
# video decode (PyAV, two-pass, single allocation)
# ---------------------------------------------------------------------------

def _decide_video_format(frame):
    alpha_channel = False
    for comp in frame.format.components:
        if comp.is_alpha or frame.format.name == "pal8":
            alpha_channel = True
            break
    if frame.format.name in _8BIT_FORMATS:
        highbit = False
        image_format = "rgba" if alpha_channel else "rgb24"
    else:
        highbit = True
        image_format = "gbrapf32le" if alpha_channel else "gbrpf32le"
    return image_format, highbit, alpha_channel


def _process_video_frame(frame, state, stream):
    import av
    image_format = state["image_format"]
    if image_format in _HIGHBIT_FORMATS and frame.width % 32 != 0:
        g_info = state["align_graph"]
        if g_info is None:
            pad_w = _align32(frame.width)
            pad_h = _align32(frame.height)
            g = av.filter.Graph()
            g_src = g.add_buffer(width=frame.width, height=frame.height,
                                 format=frame.format.name, time_base=stream.time_base)
            g_pad = g.add("pad", "{}:{}:0:0".format(pad_w, pad_h))
            g_fill = g.add("fillborders",
                           "left=0:right={}:top=0:bottom={}:mode=smear".format(pad_w - frame.width, pad_h - frame.height))
            g_sink = g.add("buffersink")
            g_src.link_to(g_pad)
            g_pad.link_to(g_fill)
            g_fill.link_to(g_sink)
            g.configure()
            g_info = (g, g_src, g_sink)
            state["align_graph"] = g_info
        g_info[1].push(frame)
        img = np.ascontiguousarray(
            g_info[2].pull().to_ndarray(format=image_format)[:frame.height, :frame.width])
    else:
        img = frame.to_ndarray(format=image_format)
    assert img.ndim == 3 and img.shape[2] in (3, 4), \
        "unexpected frame ndarray shape {} (expected HWC)".format(img.shape)
    img = _apply_rotation(img, frame.rotation)
    img = _normalize_pixels(img, state["highbit"])
    if img.shape[2] == 4:
        img = _composite_alpha(img[..., :3], img[..., 3:4], state["pad_rgb"])
    return np.ascontiguousarray(img)


def _video_probe(path, pad_rgb):
    import av
    container = av.open(path)
    stream = container.streams.video[0]
    count = 0
    state = None
    H = W = None
    for frame in container.decode(stream):
        if state is None:
            image_format, highbit, alpha = _decide_video_format(frame)
            state = {"image_format": image_format, "highbit": highbit, "alpha": alpha,
                     "align_graph": None, "pad_rgb": pad_rgb}
        img = _process_video_frame(frame, state, stream)
        if H is None:
            H, W = img.shape[0], img.shape[1]
        elif img.shape[0] != H or img.shape[1] != W:
            container.close()
            raise ValueError("video frame dimension mismatch at frame {}".format(count))
        count += 1
    ar = stream.average_rate
    source_fps = float(ar) if ar is not None else 1.0
    container.close()
    if count == 0:
        raise RuntimeError("no video frames decoded: {}".format(path))
    return count, H, W, source_fps, state["image_format"], state["highbit"], state["alpha"]


def _fast_video_metadata(path):
    import av

    stat = os.stat(path)
    cache_key = (os.path.realpath(path), stat.st_mtime_ns, stat.st_size)
    cached = _LIVE_PROBE_CACHE.get(cache_key)
    if cached is not None:
        _LIVE_PROBE_CACHE.move_to_end(cache_key)
        return cached
    container = av.open(path)
    try:
        stream = container.streams.video[0]
        frame = next(container.decode(stream), None)
        if frame is None:
            raise ValueError("no video frames found: {}".format(path))
        source_fps = float(stream.average_rate or stream.base_rate or stream.guessed_rate or 0)
        if not math.isfinite(source_fps) or source_fps <= 0:
            raise ValueError("source FPS unavailable: {}".format(path))
        duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                    else float(container.duration / av.time_base) if container.duration else 0.0)
        count = int(stream.frames or round(duration * source_fps))
        if count <= 0:
            raise ValueError("source frame count unavailable: {}".format(path))
        width, height = frame.width, frame.height
        if frame.rotation % 180:
            width, height = height, width
        result = (count, width, height, source_fps, stream.codec_context.name)
    finally:
        container.close()
    _LIVE_PROBE_CACHE[cache_key] = result
    while len(_LIVE_PROBE_CACHE) > 16:
        _LIVE_PROBE_CACHE.popitem(last=False)
    return result


def _live_source(source_mode, video_file, path):
    if source_mode == "upload":
        if not video_file:
            raise ValueError("choose a video file")
        source = folder_paths.get_annotated_filepath(video_file)
    elif source_mode == "path":
        source = os.path.abspath(os.path.expanduser((path or "").strip()))
    else:
        raise ValueError("source_mode must be upload or path")
    if not os.path.exists(source):
        raise ValueError("source does not exist: {}".format(source))
    return source


def _live_metadata(data):
    first_frame, last_frame = data["first_frame"], data["last_frame"]
    output_fps = data["output_fps"]
    preset, multiple = data["preset"], int(data["pad_multiple"])
    if (not isinstance(first_frame, int) or isinstance(first_frame, bool) or first_frame < 0
            or not isinstance(last_frame, int) or isinstance(last_frame, bool) or last_frame < -1):
        raise ValueError("invalid first_frame/last_frame")
    if last_frame != -1 and last_frame < first_frame:
        raise ValueError("last_frame must be -1 or >= first_frame")
    if (not isinstance(output_fps, (int, float)) or isinstance(output_fps, bool)
            or not math.isfinite(output_fps) or output_fps <= 0):
        raise ValueError("output_fps must be positive and finite")
    if preset not in ("None", "WAN", "LTX 2.3") or multiple not in (2, 8, 16, 32, 64):
        raise ValueError("invalid preset or pad_multiple")

    source = _live_source(data["source_mode"], data.get("video_file", ""), data.get("path", ""))
    if os.path.isdir(source):
        names = _scan_sequence_dir(source)
        image = _load_sequence_image(os.path.join(source, names[0]), np.zeros(3, dtype=np.float32))
        source_count, source_h, source_w, source_fps, kind = len(names), image.shape[0], image.shape[1], None, "sequence"
        preview_url = None
    else:
        if os.path.splitext(source)[1].lower() not in _VIDEO_EXTS:
            raise ValueError("unsupported video extension: {}".format(source))
        source_count, source_w, source_h, source_fps, codec = _fast_video_metadata(source)
        kind, preview_url = "video", _register_video_source(source, codec)

    snapped_last = _snap_last_frame(first_frame, last_frame, preset)
    range_first, range_last, selected_count, requested_last, requested_count = _resolve_frame_range(
        source_count, first_frame, snapped_last, preset)
    generated_count = _align_count(requested_count, preset)
    generated_w = _padded_dim(source_w, multiple)[0]
    generated_h = _padded_dim(source_h, multiple)[0]
    source_seconds = None if source_fps is None else source_count / source_fps
    generated_seconds = generated_count / float(output_fps)
    source_fps_text = "n/a (image sequence)" if source_fps is None else "{:.3f}".format(source_fps)
    source_seconds_text = "n/a" if source_seconds is None else "{:.3f}s".format(source_seconds)
    status = (
        "ORIGINAL: {} frames / {} | {}x{} | fps {}\n".format(
            source_count, source_seconds_text, source_w, source_h, source_fps_text)
        + "SELECTED: frames {}-{} | {} source frames | snapped last {}\n".format(
            range_first, range_last, selected_count, requested_last)
        + "GENERATED: {} frames / {:.3f}s | {}x{} | fps {} | {}".format(
            generated_count, generated_seconds, generated_w, generated_h, output_fps, preset)
    )
    return {
        "status": status, "kind": kind, "preview_url": preview_url,
        "source_count": source_count, "source_fps": source_fps,
        "source_duration": source_seconds, "source_width": source_w, "source_height": source_h,
        "first_frame": range_first, "source_last_frame": range_last,
        "snapped_last_frame": requested_last, "selected_count": selected_count,
        "generated_count": generated_count, "generated_duration": generated_seconds,
        "generated_width": generated_w, "generated_height": generated_h,
        "output_fps": float(output_fps), "preset": preset,
    }


def _atempo_chain(tempo):
    factors = []
    while tempo > 2.0:
        factors.append(2.0)
        tempo /= 2.0
    while tempo < 0.5:
        factors.append(0.5)
        tempo /= 0.5
    if not math.isclose(tempo, 1.0, rel_tol=1e-9):
        factors.append(tempo)
    return ",".join("atempo={:.12g}".format(factor) for factor in factors)


def _silence_audio(duration, sample_rate=44100, channels=2):
    samples = max(0, round(duration * sample_rate))
    return {"waveform": torch.zeros((1, channels, samples), dtype=torch.float32),
            "sample_rate": sample_rate}


def _selected_audio(path, start, source_duration, target_duration):
    import av

    if not os.path.isfile(path):
        return _silence_audio(target_duration)
    with av.open(path) as container:
        if not container.streams.audio:
            return _silence_audio(target_duration)
        stream = container.streams.audio[0]
        sample_rate = int(stream.codec_context.sample_rate or 44100)
        channels = int(stream.channels or 2)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for selected audio output")
    command = [ffmpeg, "-nostdin", "-loglevel", "error", "-i", path,
               "-ss", "{:.12g}".format(start), "-t", "{:.12g}".format(source_duration),
               "-map", "0:a:0", "-vn"]
    audio_filter = _atempo_chain(source_duration / target_duration)
    if audio_filter:
        command += ["-af", audio_filter]
    command += ["-f", "f32le", "-acodec", "pcm_f32le",
                "-ar", str(sample_rate), "-ac", str(channels), "-"]
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError("ffmpeg audio extraction failed: {}".format(
                result.stderr.decode("utf-8", "replace").strip()))
        output.seek(0)
        samples = np.fromfile(output, dtype="<f4")
    samples = samples[:samples.size // channels * channels]
    waveform = torch.from_numpy(samples.reshape(-1, channels).T.copy()).unsqueeze(0)
    target_samples = max(0, round(target_duration * sample_rate))
    if waveform.shape[-1] < target_samples:
        padded = torch.zeros((1, channels, target_samples), dtype=torch.float32)
        padded[..., :waveform.shape[-1]] = waveform
        waveform = padded
    else:
        waveform = waveform[..., :target_samples]
    return {"waveform": waveform, "sample_rate": sample_rate}


def _allocate_canvas(count_final, Hp, Wp, pad_rgb):
    est_gb = count_final * Hp * Wp * 3 * 4 / (1024.0 ** 3)
    try:
        canvas = np.full((count_final, Hp, Wp, 3), 0.0, dtype=np.float32)
        canvas[:] = pad_rgb
        return canvas
    except (MemoryError, RuntimeError, OSError, ValueError):
        raise RuntimeError(
            "Out of memory: needs ~{:.1f} GB for {} frames @ {}x{} float32 "
            "— reduce frame count or resolution".format(est_gb, count_final, Wp, Hp))


def _video_decode_fill(path, first_frame, last_frame, selected_count, count_final,
                       Hp, Wp, before_h, before_w, image_format, highbit, alpha, pad_rgb):
    import av
    canvas = _allocate_canvas(count_final, Hp, Wp, pad_rgb)
    container = av.open(path)
    stream = container.streams.video[0]
    state = {"image_format": image_format, "highbit": highbit, "alpha": alpha,
             "align_graph": None, "pad_rgb": pad_rgb}
    written = 0
    for source_index, frame in enumerate(container.decode(stream)):
        if source_index < first_frame:
            continue
        if source_index > last_frame:
            break
        img = _process_video_frame(frame, state, stream)
        h, w = img.shape[0], img.shape[1]
        canvas[written, before_h:before_h + h, before_w:before_w + w, :] = img
        written += 1
    container.close()
    if written != selected_count:
        raise RuntimeError("video frame range decoded {} frames, expected {}".format(written, selected_count))
    if written < count_final:
        canvas[written:count_final] = canvas[written - 1]
    return canvas


def _sequence_decode_fill(names, base, count_final, Hp, Wp, before_h, before_w, pad_rgb):
    canvas = _allocate_canvas(count_final, Hp, Wp, pad_rgb)
    idx = 0
    for name in names:
        img = _load_sequence_image(os.path.join(base, name), pad_rgb)
        h, w = img.shape[0], img.shape[1]
        canvas[idx, before_h:before_h + h, before_w:before_w + w, :] = img
        idx += 1
    if idx < count_final and idx > 0:
        canvas[idx:count_final] = canvas[idx - 1]
    return canvas


# ---------------------------------------------------------------------------
# node
# ---------------------------------------------------------------------------

class MediaLoadVideoSequence(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
        files = folder_paths.filter_files_content_types(files, ["video"])
        return io.Schema(
            node_id="MediaLoadVideoSequence",
            display_name="Load Video/Sequence",
            category="media",
            inputs=[
                io.Combo.Input("source_mode", options=["upload", "path"]),
                io.Combo.Input("video_file", options=sorted(files)),
                io.String.Input("path"),
                io.Int.Input("first_frame", default=0, min=0, max=2147483647),
                io.Int.Input("last_frame", default=-1, min=-1, max=2147483647),
                io.Float.Input("output_fps", default=25.0, min=0.001, max=240.0),
                io.Combo.Input("preset", options=["None", "WAN", "LTX 2.3"]),
                io.Combo.Input("pad_multiple", options=["2", "8", "16", "32", "64"]),
                io.Color.Input("pad_color", default="#000000"),
            ],
            outputs=[
                io.Image.Output(),
                io.Float.Output("fps"),
                io.Int.Output("frame_count"),
                io.Int.Output("width"),
                io.Int.Output("height"),
                io.Audio.Output("audio"),
            ],
        )

    @classmethod
    def execute(cls, source_mode, video_file, path, first_frame, last_frame,
                output_fps, preset, pad_multiple, pad_color):
        pad_color = _normalize_pad_color(pad_color)
        pad_rgb = _hex_to_rgb01(pad_color)
        m = int(pad_multiple)
        fps_out = float(output_fps)
        names = None

        if source_mode == "upload":
            src_path = folder_paths.get_annotated_filepath(video_file)
            kind = "video"
            count, H, W, source_fps, image_format, highbit, alpha = _video_probe(src_path, pad_rgb)
        else:
            src_path = path
            if os.path.isdir(src_path):
                kind = "sequence"
                names = sorted(_scan_sequence_dir(src_path), key=_natural_key)
                (H, W, _c) = _probe_sequence_uniform(names, src_path)
                count = len(names)
                source_fps = None
                image_format = highbit = alpha = None
            else:
                kind = "video"
                count, H, W, source_fps, image_format, highbit, alpha = _video_probe(src_path, pad_rgb)

        source_count = count
        snapped_last = _snap_last_frame(first_frame, last_frame, preset)
        range_first, range_last, selected_count, requested_last, requested_count = _resolve_frame_range(
            source_count, first_frame, snapped_last, preset)
        count_final = _align_count(requested_count, preset)
        Wp, before_w, after_w = _padded_dim(W, m)
        Hp, before_h, after_h = _padded_dim(H, m)

        if kind == "video":
            canvas = _video_decode_fill(src_path, range_first, range_last, selected_count,
                                        count_final, Hp, Wp, before_h, before_w,
                                        image_format, highbit, alpha, pad_rgb)
        else:
            selected_names = names[range_first:range_last + 1]
            canvas = _sequence_decode_fill(selected_names, src_path, count_final, Hp, Wp,
                                           before_h, before_w, pad_rgb)

        preview_url = None
        target_duration = count_final / fps_out
        audio = _selected_audio(src_path, range_first / source_fps,
                                selected_count / source_fps, target_duration) if kind == "video" \
            else _silence_audio(target_duration)
        image = torch.from_numpy(canvas)
        src_fps_str = "n/a" if source_fps is None else "{:.3f}".format(source_fps)
        source_seconds = "n/a" if not source_fps else "{:.3f}s".format(source_count / source_fps)
        generated_seconds = "{:.3f}s".format(count_final / fps_out)
        status = (
            "source: {} {}\n".format(kind, os.path.basename(src_path))
            + "original duration: {} frames / {} @ {}x{} (fps: {})\n".format(
                source_count, source_seconds, W, H, src_fps_str)
            + "selected source range: {}-{} ({} frames)\n".format(range_first, range_last, selected_count)
            + "snapped last frame: {}\n".format(requested_last)
            + "generated duration: {} frames ({}) / {} @ {}x{} | output fps: {}".format(
                count_final, preset, generated_seconds, Wp, Hp, fps_out)
        )
        range_info = {
            "source_fps": source_fps,
            "source_count": int(source_count),
            "first_frame": int(range_first),
            "source_last_frame": int(range_last),
            "snapped_last_frame": int(requested_last),
            "selected_count": int(selected_count),
            "generated_count": int(count_final),
            "output_fps": fps_out,
            "preview_url": preview_url,
        }
        return io.NodeOutput(image, fps_out, int(count_final), int(Wp), int(Hp), audio,
                             ui={"text": (status,), "media_range": (range_info,)})

    @classmethod
    def fingerprint_inputs(cls, source_mode, video_file, path, first_frame, last_frame,
                           output_fps, preset, pad_multiple, pad_color):
        pad_color = _normalize_pad_color(pad_color)
        h = hashlib.sha256()
        h.update(str(source_mode).encode("utf-8"))
        h.update(str(preset).encode("utf-8"))
        h.update(str(pad_multiple).encode("utf-8"))
        h.update(str(pad_color).encode("utf-8"))
        h.update(str(first_frame).encode("utf-8"))
        h.update(str(last_frame).encode("utf-8"))
        h.update(str(output_fps).encode("utf-8"))
        if source_mode == "upload":
            fp = folder_paths.get_annotated_filepath(video_file)
            h.update(b"\x00"); h.update(fp.encode("utf-8"))
            if os.path.exists(fp):
                st = os.stat(fp)
                h.update(str(st.st_mtime).encode("utf-8"))
                h.update(str(st.st_size).encode("utf-8"))
        else:
            h.update(b"\x00"); h.update((path or "").encode("utf-8"))
            if os.path.isdir(path):
                for name in sorted(_scan_sequence_dir(path), key=_natural_key):
                    st = os.stat(os.path.join(path, name))
                    h.update(name.encode("utf-8"))
                    h.update(str(st.st_mtime).encode("utf-8"))
                    h.update(str(st.st_size).encode("utf-8"))
            elif os.path.exists(path):
                st = os.stat(path)
                h.update(str(st.st_mtime).encode("utf-8"))
                h.update(str(st.st_size).encode("utf-8"))
        return h.hexdigest()

    @classmethod
    def validate_inputs(cls, source_mode, video_file, path, first_frame, last_frame,
                        output_fps, preset, pad_multiple, pad_color):
        pad_color = _normalize_pad_color(pad_color)
        if (not isinstance(first_frame, int) or isinstance(first_frame, bool) or first_frame < 0):
            return "first_frame must be a non-negative integer"
        if (not isinstance(last_frame, int) or isinstance(last_frame, bool) or last_frame < -1):
            return "last_frame must be -1 or a non-negative integer"
        if last_frame != -1 and last_frame < first_frame:
            return "last_frame must be -1 or greater than or equal to first_frame"
        if (not isinstance(output_fps, (int, float)) or isinstance(output_fps, bool)
                or not math.isfinite(output_fps) or output_fps <= 0):
            return "output_fps must be a positive finite number"
        if not _HEX_RE.match(pad_color or ""):
            return "pad_color must be #RRGGBB hex (got {!r})".format(pad_color)
        if source_mode == "upload":
            input_dir = folder_paths.get_input_directory()
            vids = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
            vids = folder_paths.filter_files_content_types(vids, ["video"])
            if not vids:
                return "no video files in input directory — upload a file or switch source_mode to path"
            if not folder_paths.exists_annotated_filepath(video_file or ""):
                return "Invalid video file: {}".format(video_file)
            return True
        if source_mode == "path":
            if not os.path.exists(path or ""):
                return "path does not exist: {}".format(path)
            if os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if ext not in _VIDEO_EXTS:
                    return "video path must be .mov or .mp4 (got {})".format(ext)
                try:
                    import av
                    c = av.open(path)
                    _ = c.streams.video[0]
                    c.close()
                except Exception as e:
                    return "video not decodable: {}".format(e)
                return True
            if os.path.isdir(path):
                names = sorted(_scan_sequence_dir(path), key=_natural_key)
                if not names:
                    return "no image sequence files (.png/.tif/.tiff/.exr) in directory"
                exts = {os.path.splitext(n)[1].lower() for n in names}
                if len(exts) > 1:
                    return "mixed image extensions in sequence: {}".format(sorted(exts))
                try:
                    _probe_sequence_uniform(names, path)
                    snapped_last = _snap_last_frame(first_frame, last_frame, preset)
                    _resolve_frame_range(len(names), first_frame, snapped_last, preset)
                except ValueError as e:
                    return str(e)
                return True
            return "path is neither a file nor a directory: {}".format(path)
        return "invalid source_mode: {}".format(source_mode)


class MediaLoaderExtension(ComfyExtension):
    async def get_node_list(self):
        return [MediaLoadVideoSequence]


async def comfy_entrypoint() -> MediaLoaderExtension:
    return MediaLoaderExtension()
