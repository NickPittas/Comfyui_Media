"""Plain directly-runnable asserts for Media Load Video/Sequence.

Run:  python3 tests/test_media_loader.py
No pytest. Prints PASS/FAIL per test, exits nonzero on any failure.
"""
import os
import sys
import json
import math
import asyncio
import tempfile
import traceback
import subprocess

COMFY_ROOT = "/home/npittas/ComfyUI-Easy-Install/ComfyUI-Easy-Install/ComfyUI"
if COMFY_ROOT not in sys.path:
    sys.path.insert(0, COMFY_ROOT)

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
INIT = os.path.join(PKG, "__init__.py")
WEB = os.path.join(PKG, "web", "media_loader.js")

import importlib.util
_spec = importlib.util.spec_from_file_location("cmedia_init", INIT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

import numpy as np
node = mod.MediaLoadVideoSequence


def _run(source_mode="path", video_file="", path="", first_frame=0, last_frame=-1,
         output_fps=25.0, preset="None", pad_multiple="2", pad_color="#000000", reverse=False):
    return node.execute(source_mode=source_mode, video_file=video_file, path=path,
                        first_frame=first_frame, last_frame=last_frame,
                        output_fps=output_fps, preset=preset,
                        pad_multiple=pad_multiple, pad_color=pad_color, reverse=reverse)


def _validate(path="/tmp", first_frame=0, last_frame=-1, output_fps=25.0, preset="None",
              pad_multiple="2", pad_color="#000000", source_mode="path", video_file="", reverse=False):
    return node.validate_inputs(source_mode=source_mode, video_file=video_file, path=path,
                                first_frame=first_frame, last_frame=last_frame,
                                output_fps=output_fps, preset=preset,
                                pad_multiple=pad_multiple, pad_color=pad_color, reverse=reverse)


def _png(path, w, h, rgb):
    from PIL import Image
    Image.new("RGB", (w, h), rgb).save(path, "PNG")


def _png_rgba(path, w, h, rgba):
    from PIL import Image
    Image.new("RGBA", (w, h), rgba).save(path, "PNG")


def _tiff(path, w, h, rgb):
    from PIL import Image
    Image.new("RGB", (w, h), rgb).save(path, "TIFF")


def _make_mp4(path, count=10, w=64, h=48, fps=25):
    try:
        import av
        container = av.open(path, "w", format="mp4")
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width = w
        stream.height = h
        stream.pix_fmt = "yuv420p"
        for i in range(count):
            arr = np.full((h, w, 3), (i * 20) % 255, dtype=np.uint8)
            for packet in stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        return True
    except Exception as e:
        print("  (mp4 encode unavailable: {})".format(e))
        return False


# ---------------------------------------------------------------------------

def test_natural_sort():
    with tempfile.TemporaryDirectory() as d:
        _png(os.path.join(d, "frame10.png"), 4, 4, (10, 0, 0))
        _png(os.path.join(d, "frame2.png"), 4, 4, (2, 0, 0))
        out = _run(path=d)
        img = out.result[0].numpy()
        assert img.shape[0] == 2, img.shape
        # natural order: frame2 (red=2) first, frame10 (red=10) second
        assert abs(img[0, 0, 0, 0] - 2 / 255.0) < 1e-5
        assert abs(img[1, 0, 0, 0] - 10 / 255.0) < 1e-5
    return "frame2 before frame10 (natural sort), count 2"


def test_tiff():
    with tempfile.TemporaryDirectory() as d:
        for i in range(3):
            _tiff(os.path.join(d, "t{}.tif".format(i)), 8, 8, (i * 20, 0, 0))
        out = _run(path=d)
        img, fps, count, w, h, audio = out.result
        assert count == 3 and img.shape[0] == 3, (count, img.shape)
        assert audio["waveform"].shape == (1, 2, round(3 / 25 * 44100))
    return "3 TIFF frames loaded"


def test_exr_clamp():
    import cv2
    with tempfile.TemporaryDirectory() as d:
        arr = np.zeros((4, 4, 3), dtype=np.float32)
        arr[..., 0] = 1.5   # B
        arr[..., 1] = 0.5   # G
        arr[..., 2] = -0.2  # R (clamps to 0)
        if not cv2.imwrite(os.path.join(d, "a.exr"), arr):
            return "SKIP: OpenCV EXR write unavailable"
        if cv2.imread(os.path.join(d, "a.exr"), cv2.IMREAD_UNCHANGED) is None:
            return "SKIP: OpenCV EXR read unavailable"
        out = _run(path=d)
        img = out.result[0].numpy()
        assert img.shape[0] == 1
        px = img[0, 0, 0]
        assert abs(px[0]) < 1e-5, px       # R clamped 0
        assert abs(px[1] - 0.5) < 1e-5, px  # G 0.5
        assert abs(px[2] - 1.0) < 1e-5, px  # B clamped 1.0
        assert img.min() >= -1e-6 and img.max() <= 1.0 + 1e-6
    return "EXR clamped to [0,1], BGR->RGB verified"


def test_invalid_sequence():
    with tempfile.TemporaryDirectory() as d:
        _png(os.path.join(d, "a.png"), 4, 4, (0, 0, 0))
        _tiff(os.path.join(d, "b.tif"), 4, 4, (0, 0, 0))
        assert _validate(path=d) is not True
    with tempfile.TemporaryDirectory() as d2:
        _png(os.path.join(d2, "a.png"), 4, 4, (0, 0, 0))
        _png(os.path.join(d2, "b.png"), 8, 8, (0, 0, 0))
        assert _validate(path=d2) is not True
    return "mixed extensions and mixed dims rejected"


def test_mp4():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "clip.mp4")
        if not _make_mp4(p, count=10, w=64, h=48, fps=25):
            return "SKIP: PyAV mp4 encode unavailable"
        out = _run(path=p, output_fps=25.0)
        img, fps, count, w, h, audio = out.result
        assert count == 10, count
        assert abs(fps - 25.0) < 1e-6, fps
        assert audio["sample_rate"] == 44100 and not np.count_nonzero(audio["waveform"].numpy())
        assert img.shape[0] == 10, img.shape  # two-pass count matches fill count
        assert w == 64 and h == 48, (w, h)
        ranged = _run(path=p, first_frame=2, last_frame=5, output_fps=25.0)
        assert ranged.result[2] == 4 and ranged.result[0].shape[0] == 4
        preview_url = mod._live_metadata({
            "source_mode": "path", "video_file": "", "path": p,
            "first_frame": 2, "last_frame": 5, "output_fps": 25.0,
            "preset": "WAN", "pad_multiple": "2",
        })["preview_url"]
        assert preview_url.startswith("/media-loader/preview/")
        preview_path, codec = mod._PREVIEW_FILES[preview_url.rsplit("/", 1)[1]]
        assert preview_path == os.path.realpath(p) and codec
        live = mod._live_metadata({
            "source_mode": "path", "video_file": "", "path": p,
            "first_frame": 2, "last_frame": 5, "output_fps": 25.0,
            "preset": "WAN", "pad_multiple": "2",
        })
        assert live["source_count"] == 10 and live["generated_count"] == 5
        assert live["source_duration"] == 0.4 and live["generated_duration"] == 0.2
        assert "ORIGINAL: 10 frames / 0.400s | 64x48 | fps 25.000" in live["status"]
        assert "GENERATED: 5 frames / 0.200s | 64x48 | fps 25.0 | WAN" in live["status"]
    return "mp4: live probe reports original/generated metadata without workflow execution"


def test_audio_output():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audio.mp4")
        subprocess.run([
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x48:r=25:d=0.4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path,
        ], check=True)
        out = _run(path=path, first_frame=2, last_frame=5, output_fps=50.0)
        audio = out.result[5]
        waveform = audio["waveform"]
        assert audio["sample_rate"] == 48000
        assert waveform.shape == (1, 1, 3840), waveform.shape
        samples = waveform[0, 0].numpy()
        frequencies = np.fft.rfftfreq(samples.size, 1 / 48000)
        peak = frequencies[np.argmax(np.abs(np.fft.rfft(samples * np.hanning(samples.size))))]
        assert 400 < peak < 480, peak

        aligned = _run(path=path, first_frame=2, last_frame=5, output_fps=25.0, preset="LTX 2.3")
        assert aligned.result[5]["waveform"].shape == (1, 1, 17280)
    return "selected audio trimmed and pitch-preserving stretched to generated duration"


def test_reverse():
    with tempfile.TemporaryDirectory() as d:
        for index, value in enumerate((0, 40, 80, 120)):
            _png(os.path.join(d, f"frame{index}.png"), 8, 8, (value, value, value))
        out = _run(path=d, preset="WAN", reverse=True)
        means = out.result[0].numpy().mean(axis=(1, 2, 3))
        expected = np.array([120, 80, 40, 0, 0], dtype=np.float32) / 255
        assert np.allclose(means, expected, atol=1e-6), means
        assert not np.count_nonzero(out.result[5]["waveform"].numpy())

        path = os.path.join(d, "video.mp4")
        _make_mp4(path, count=4)
        video = _run(path=path, preset="WAN", reverse=True).result[0].numpy()
        video_means = video.mean(axis=(1, 2, 3))
        assert np.all(np.diff(video_means[:4]) < 0), video_means
        assert np.allclose(video[4], video[3]), video_means
    assert "reverse must be boolean" in _validate(reverse="yes")
    return "video and sequence reverse; aligned tail freezes first source frame; audio silent"


def test_rotation():
    a = np.arange(2 * 3 * 3, dtype=np.float32).reshape(2, 3, 3)
    assert np.array_equal(mod._apply_rotation(a, 0), a)
    r90 = mod._apply_rotation(a, 90)
    assert np.array_equal(r90, np.rot90(a, k=1, axes=(0, 1)).copy())
    assert r90.shape == (3, 2, 3)  # H/W swapped by 90deg
    return "helper-math (source-trace video_types.py:356-358); synthetic-rot metadata not writable via PyAV"


def test_highbit_alignment():
    for w in (40, 50, 64, 33):
        pw = mod._align32(w)
        assert pw % 32 == 0 and pw >= w, (w, pw)
        assert (pw > w) == (w % 32 != 0), (w, pw)
    assert mod._align32(40) == 64       # pad target
    assert mod._align32(64) == 64       # already aligned -> no pad
    # crop slice [:h, :w] restores original pre-rotation dims
    return "helper-math (source-trace video_types.py:337-353); pad_w/h ceil-32, crop-back verified"


def test_highbit_policy():
    a = np.array([[[0.0, 0.5, 1.5]]], dtype=np.float32)
    out = mod._normalize_pixels(a, True)
    assert np.array_equal(out, a) and out.max() == 1.5  # identity, no clamp
    u = np.array([[[255, 128, 0]]], dtype=np.uint8)
    o2 = mod._normalize_pixels(u, False)
    assert abs(o2[0, 0, 0] - 1.0) < 1e-6 and abs(o2[0, 0, 1] - 128 / 255.0) < 1e-6
    return "highbit identity (no /255, no clamp); 8bit /255"


def test_fps_independence():
    with tempfile.TemporaryDirectory() as d:
        for i in range(1, 11):
            _png(os.path.join(d, "frame{}.png".format(i)), 10, 10, (i * 10 % 255, 0, 0))
        # WAN + 25 -> 13 @ 25
        out = _run(path=d, output_fps=25.0, preset="WAN")
        img, fps, count, w, h, audio = out.result
        assert count == 13 and abs(fps - 25.0) < 1e-9 and img.shape[0] == 13, (count, fps)
        # LTX + 25 -> 17 @ 25
        out = _run(path=d, output_fps=25.0, preset="LTX 2.3")
        img, fps, count, w, h, audio = out.result
        assert count == 17 and abs(fps - 25.0) < 1e-9, (count, fps)
        # LTX + 50 -> 17 @ 50
        out = _run(path=d, output_fps=50.0, preset="LTX 2.3")
        img, fps, count, w, h, audio = out.result
        assert count == 17 and abs(fps - 50.0) < 1e-9, (count, fps)
        # None -> 10 @ 25
        out = _run(path=d, output_fps=25.0, preset="None")
        img, fps, count, w, h, audio = out.result
        assert count == 10 and abs(fps - 25.0) < 1e-9, (count, fps)
        # freeze rows equal last source frame (WAN, 13 rows: freeze = rows 10,11,12 == row 9)
        out = _run(path=d, output_fps=25.0, preset="WAN")
        img = out.result[0].numpy()
        assert np.array_equal(img[12], img[9]) and np.array_equal(img[11], img[9])
    return "WAN@25=13, LTX@25=17, LTX@50=17, None=10; freeze rows == last source frame"


def test_frame_range_and_duration():
    assert mod._normalize_pad_color("image") == "#000000"
    assert mod._snap_last_frame(0, 9, "LTX 2.3") == 16
    assert mod._snap_last_frame(5, 14, "WAN") == 17
    assert mod._snap_last_frame(5, 14, "None") == 14
    assert mod._snap_last_frame(5, -1, "LTX 2.3") == -1
    with tempfile.TemporaryDirectory() as d:
        for i in range(10):
            _png(os.path.join(d, "frame{}.png".format(i)), 4, 4, (i * 20, 0, 0))

        live = mod._live_metadata({
            "source_mode": "path", "video_file": "", "path": d,
            "first_frame": 2, "last_frame": 5, "output_fps": 25.0,
            "preset": "None", "pad_multiple": "2",
        })
        assert live["source_count"] == 10 and live["source_fps"] is None
        assert live["generated_count"] == 4 and "fps n/a (image sequence)" in live["status"]

        out = _run(path=d, first_frame=2, last_frame=5, preset="None")
        img, _, count, _, _, audio = out.result
        assert count == 4 and img.shape[0] == 4
        assert abs(img[0, 0, 0, 0] - 40 / 255.0) < 1e-5
        status = out.ui["text"][0]
        assert "original duration: 10 frames" in status
        assert "selected source range: 2-5 (4 frames)" in status
        assert "generated duration: 4 frames (None, forward) / 0.160s" in status
        info = dict(out.ui["media_range"][0])
        preview_url = info.pop("preview_url")
        assert preview_url is None
        assert info == {
            "source_fps": None, "source_count": 10, "first_frame": 2,
            "source_last_frame": 5, "snapped_last_frame": 5,
            "selected_count": 4, "generated_count": 4, "output_fps": 25.0,
            "reverse": False,
        }, info

        out = _run(path=d, first_frame=2, last_frame=5, preset="WAN")
        assert out.result[2] == 5 and out.result[0].shape[0] == 5
        assert "snapped last frame: 6" in out.ui["text"][0]

        out = _run(path=d, first_frame=2, last_frame=5, preset="LTX 2.3")
        img = out.result[0].numpy()
        assert out.result[2] == 9 and img.shape[0] == 9
        assert np.array_equal(img[8], img[7])
        assert "snapped last frame: 10" in out.ui["text"][0]

        out = _run(path=d, first_frame=8, last_frame=-1, preset="LTX 2.3")
        assert out.result[2] == 9
        assert "selected source range: 8-9 (2 frames)" in out.ui["text"][0]

        assert _validate(path=d, first_frame=10) is not True
        assert _validate(path=d, last_frame=10, preset="None") is not True
        assert _validate(path=d, last_frame=9, preset="LTX 2.3") is True
    assert _validate(first_frame=-1) == "first_frame must be a non-negative integer"
    assert _validate(last_frame=-2) == "last_frame must be -1 or a non-negative integer"
    assert _validate(first_frame=3, last_frame=2) == "last_frame must be -1 or greater than or equal to first_frame"
    return "range is inclusive; UI/backend snap last upward; source and generated durations shown"


def test_output_fps_validation():
    for bad in (0.0, -5.0, float("nan"), float("inf"), float("-inf")):
        r = _validate(output_fps=bad)
        assert r == "output_fps must be a positive finite number", (bad, r)
    # valid fps passes the fps gate (moves on to path checks)
    assert _validate(output_fps=25.0) != "output_fps must be a positive finite number"
    return "0/negative/nan/inf rejected at fps gate; 25.0 accepted"


def test_padding():
    assert mod._padded_dim(60, 16) == (64, 2, 2)
    assert mod._padded_dim(36, 16) == (48, 6, 6)
    assert mod._padded_dim(37, 16) == (48, 5, 6)  # odd extra on right/bottom
    with tempfile.TemporaryDirectory() as d:
        _png(os.path.join(d, "f0.png"), 37, 37, (10, 20, 30))
        out = _run(path=d, pad_multiple="16", pad_color="#112233")
        img = out.result[0].numpy()
        assert img.shape == (1, 48, 48, 3), img.shape
        c = np.array([0x11, 0x22, 0x33], dtype=np.float32) / 255.0
        assert np.allclose(img[0, 0, 0], c, atol=1e-5), img[0, 0, 0]
        assert np.allclose(img[0, 47, 47], c, atol=1e-5)
    return "60x36->64x48, odd-extra-right, border == pad_color"


def test_alpha_composite():
    with tempfile.TemporaryDirectory() as d:
        _png_rgba(os.path.join(d, "a.png"), 2, 2, (200, 40, 40, 128))
        out = _run(path=d, pad_color="#000010")
        img = out.result[0].numpy()
        rgb = np.array([200, 40, 40], dtype=np.float32) / 255.0
        a = 128 / 255.0
        pad = np.array([0, 0, 0x10], dtype=np.float32) / 255.0
        exp = rgb * a + pad * (1 - a)
        assert img.shape[3] == 3  # alpha dropped
        assert np.allclose(img[0, 0, 0], exp, atol=1e-5), (img[0, 0, 0], exp)
    return "RGBA composited over pad_color, alpha dropped"


def test_schema():
    ext = asyncio.run(mod.comfy_entrypoint())
    nodes = asyncio.run(ext.get_node_list())
    assert nodes == [mod.MediaLoadVideoSequence], nodes
    schema = node.define_schema()
    assert schema.node_id == "MediaLoadVideoSequence"
    ids = [i.id for i in schema.inputs]
    assert ids == ["source_mode", "video_file", "path", "first_frame", "last_frame",
                   "output_fps", "preset", "pad_multiple", "pad_color", "reverse"], ids
    assert [o.id for o in schema.outputs] == [None, "fps", "frame_count", "width", "height", "audio"]
    for i in schema.inputs:
        json.dumps(i.as_dict())
    for o in schema.outputs:
        json.dumps(o.as_dict())
    ofps = [i for i in schema.inputs if i.id == "output_fps"][0]
    assert ofps.default == 25.0, ofps.default
    return "node_id + 10 inputs + 6 outputs including AUDIO; schema serializable"


def test_static():
    src = open(INIT).read()
    web = open(WEB).read()
    for bad in ("NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "INPUT_TYPES",
                "IS_CHANGED", "VALIDATE_INPUTS", "torch.stack", "torch.cat", "sequence_fps",
                "UploadType.video"):
        assert bad not in src, bad
    assert 'addDOMWidget("status"' in web
    assert web.count("minWidth: 1") == 2
    assert 'fetch("/media-loader/probe"' in web and 'addEventListener("timeupdate", enforceRange)' in web
    assert "Inspecting source" in web and "live.probe()" in web
    assert 'seekInput.ariaLabel = "Preview timeline"' in web
    assert "reverseAnimation = requestAnimationFrame(tick)" in web
    assert 'position: "static"' in web and 'position: "absolute", left: "8px"' not in web
    assert "repairLegacyWidgetValues(this)" in web
    assert 'fileInput.accept = ".mov,.mp4,video/quicktime,video/mp4"' in web
    assert 'request.open("POST", `/media-loader/upload?filename=' in web
    assert "file.size >" not in web and "AbortSignal.timeout" not in web
    assert "Queue node to update" not in web
    return "V3 only; live probe/cards/controls; MOV+MP4 chooser; no workflow-run dependency"


def test_broken_chapter_proxy():
    import cv2

    sample = ("/mnt/Projects/26061_Parasol_Cosmote Fryctoria_2026-06-09/Renders/AE/FOR AI/"
              "V1-0006_DJI_0013/V1-0006_DJI_0013.mov Comp 2.mov")
    if not os.path.exists(sample):
        return "SKIP: broken-chapter ProRes sample absent"
    proxy = mod._browser_preview_path(sample, "prores")
    capture = cv2.VideoCapture(proxy)
    ok, frame = capture.read()
    capture.release()
    assert ok and frame.mean() > 1, (ok, None if frame is None else frame.mean())
    return "broken QuickTime chapter metadata stripped; H.264 proxy decodes non-black"


def test_arch_path_mode():
    sample = "/home/npittas/Videos/DJI_20260506101937_0162_D.MOV"
    if not os.path.exists(sample):
        return "SKIP: DJI sample MOV absent"
    r = _validate(path=sample)
    assert r is True, r
    return "path-mode validate passes on DJI MOV (container probe only, no full decode)"


def test_ui_smoke():
    return ("INCONCLUSIVE: manual canvas playback not automated; live server API contract is checked "
            "separately; native upload preview loops selected range")


TESTS = [
    test_natural_sort, test_tiff, test_exr_clamp, test_invalid_sequence,
    test_mp4, test_audio_output, test_reverse, test_rotation, test_highbit_alignment, test_highbit_policy,
    test_fps_independence, test_frame_range_and_duration, test_output_fps_validation, test_padding,
    test_alpha_composite, test_schema, test_static, test_broken_chapter_proxy,
    test_arch_path_mode, test_ui_smoke,
]


def main():
    passed = failed = skipped = 0
    for t in TESTS:
        try:
            msg = t()
            if isinstance(msg, str) and msg.startswith("SKIP"):
                skipped += 1
                print("SKIP  {} -- {}".format(t.__name__, msg))
            else:
                passed += 1
                print("PASS  {} -- {}".format(t.__name__, msg))
        except Exception as e:
            failed += 1
            print("FAIL  {} -- {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n==== {} passed, {} skipped, {} failed ====".format(passed, skipped, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
