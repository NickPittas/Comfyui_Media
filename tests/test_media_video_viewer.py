"""Plain directly-runnable asserts for Media Video Viewer/Saver.

Run from the package directory:
  PYTHONPATH=../.. ../../../python_embeded/bin/python3.12 tests/test_media_video_viewer.py
No pytest. Prints PASS/FAIL/SKIP per test, exits nonzero on any failure.
"""
import os
import sys
import json
import shutil
import asyncio
import zipfile
import tempfile
import traceback
import subprocess
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
COMFY_ROOT = os.path.dirname(os.path.dirname(PKG))  # ../.. from the package dir
if COMFY_ROOT not in sys.path:
    sys.path.insert(0, COMFY_ROOT)

INIT = os.path.join(PKG, "__init__.py")
WEB = os.path.join(PKG, "web", "media_video_viewer.js")

import importlib.util
_spec = importlib.util.spec_from_file_location("cmedia_init", INIT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
node = mod.MediaVideoViewer
viewer = sys.modules["media_video_viewer"]  # module body MediaVideoViewer was imported from

import numpy as np
import torch
from comfy_api.latest import io, VideoComponents, VideoFromComponents


def _run(video=None, **kw):
    media = video if video is not None else torch.rand(2, 8, 8, 3)
    return node.execute(video=media, **kw)


def _sweep(paths):
    """Remove temp artifacts this test created (preview files + saved outputs)."""
    for path in paths:
        if not path:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.unlink(path)


def _preview_files(out):
    """Resolve preview/reference/download tokens to backing files immediately,
    before later runs evict the tokens from the capped _PREVIEWS map."""
    item = out.ui["media_video"][0]
    paths = []
    for key in ("preview_url", "reference_url", "download_url"):
        url = item.get(key)
        path = url and viewer._PREVIEWS.get(url.rsplit("/", 1)[1])
        if path:
            paths.append(path)
    return paths


def _ffprobe_streams(path):
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_name,codec_type,profile,pix_fmt", "-of", "json", path],
                         capture_output=True, text=True)
    return json.loads(out.stdout).get("streams", [])


def _decode_exr(path, w, h):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1",
                          "-f", "rawvideo", "-pixel_format", "gbrpf32le", "pipe:1"],
                         capture_output=True)
    return np.frombuffer(out.stdout, dtype="<f4").reshape(3, h, w).transpose(1, 2, 0)  # G,B,R


# ---------------------------------------------------------------------------

def test_routes_registered():
    import types
    from aiohttp import web

    original = sys.modules.get("server")
    fake = types.ModuleType("server")
    fake.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=web.RouteTableDef()))
    sys.modules["server"] = fake
    try:
        spec = importlib.util.spec_from_file_location("media_video_routes_test", os.path.join(PKG, "media_video_viewer.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        routes = {(route.method, route.path) for route in fake.PromptServer.instance.routes}
        assert ("GET", "/media-video/dirs") in routes, routes
        assert ("GET", "/media-video/preview/{token}") in routes, routes
    finally:
        if original is None:
            del sys.modules["server"]
        else:
            sys.modules["server"] = original
    return "both GET routes registered on ComfyUI's RouteTableDef"


def test_registration():
    ext = mod.MediaLoaderExtension()
    nodes = asyncio.run(ext.get_node_list())
    assert node in nodes, nodes
    schema = node.define_schema()
    assert schema.node_id == "MediaVideoViewer", schema.node_id
    names = [item.id for item in schema.inputs]
    for expected in ("video", "reference", "audio", "fps", "pingpong", "format",
                     "filename_prefix", "output_root", "subfolder", "versioning", "save_output"):
        assert expected in names, (expected, names)
    outputs = [item.id for item in schema.outputs]
    assert any(str(item.io_type) == "VIDEO" for item in schema.outputs), outputs
    assert outputs == ["video", "path"], outputs
    js = open(WEB, encoding="utf-8").read()
    for expected in ("MediaVideoViewer", "media_video", "media-video/dirs", "preview_url", "download_name"):
        assert expected in js, expected
    # Direct-file load of __init__ (no package context) still registers the node.
    assert sys.modules.get("media_video_viewer") is not None
    return "V3 extension lists MediaVideoViewer; schema outputs real VIDEO; JS contract present"


def test_png_sequence():
    before = set(viewer._PREVIEWS)
    made = []
    img = torch.zeros(3, 8, 6, 3)
    img[1, 0, 0, 0] = 0.5
    try:
        out = _run(video=img, fps=12.0, format="PNG sequence", save_output=False,
                   filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        item = out.ui["media_video"][0]
        assert item["saved_path"] is None
        download = viewer._PREVIEWS[item["download_url"].rsplit("/", 1)[1]]  # preview-only: zip lives in temp
        assert download.endswith(".zip") and os.path.dirname(download) == viewer.folder_paths.get_temp_directory()
        assert item["download_name"] == os.path.basename(download) and item["download_name"].endswith(".zip")
        with zipfile.ZipFile(download) as archive:
            assert archive.namelist() == [f"vv_00001.{i:04d}.png" for i in range(1, 4)], archive.namelist()
        assert item["frames"] == 3 and item["fps"] == 12.0
        seq = out.result[1]
        assert os.path.isdir(seq)
        files = sorted(os.listdir(seq))
        assert files == ["vv_00001.0001.png", "vv_00001.0002.png", "vv_00001.0003.png"], files
        from PIL import Image
        decoded = np.asarray(Image.open(os.path.join(seq, files[1])), dtype=np.float32) / 255.0
        assert abs(decoded[0, 0, 0] - 0.5) < 1 / 255.0
        assert decoded.shape == (8, 6, 3)
        video_out = out.result[0]
        assert isinstance(video_out, VideoFromComponents), type(video_out)
        assert video_out.get_components().frame_rate == Fraction(12)
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "PNG sequence in temp, correct names/values, VIDEO output at fps 12"


def test_exr_float():
    if not shutil.which("ffmpeg"):
        return "SKIP: ffmpeg unavailable"
    before = set(viewer._PREVIEWS)
    made = []
    img = torch.zeros(2, 6, 8, 3, dtype=torch.float32)
    img[0, 1, 2] = torch.tensor((1.5, -0.2, 0.333))
    img[1, 3, 4] = torch.tensor((0.25, 2.0, -1.0))
    try:
        out = _run(video=img, fps=10.0, format="EXR sequence", save_output=False,
                   filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        seq = out.result[1]
        files = sorted(os.listdir(seq))
        assert files == ["vv_00001.0001.exr", "vv_00001.0002.exr"], files
        decoded = _decode_exr(os.path.join(seq, files[0]), 8, 6)
        # decoder planes are G,B,R; source is RGB
        assert np.allclose(decoded[..., 0], img[0, :, :, 1].numpy(), atol=1e-6)  # G
        assert np.allclose(decoded[..., 1], img[0, :, :, 0].numpy(), atol=1e-6)  # B
        assert np.allclose(decoded[..., 2], img[0, :, :, 2].numpy(), atol=1e-6)  # R
        decoded2 = _decode_exr(os.path.join(seq, files[1]), 8, 6)
        assert abs(decoded2[3, 4, 0] - 2.0) < 1e-6   # G plane carries out-of-range float
        assert abs(decoded2[3, 4, 2] - (-1.0)) < 1e-6  # B plane negative float
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "EXR float preserved through ffmpeg gbrpf32le roundtrip (HDR >1, negatives)"


def test_mov_prores():
    if not shutil.which("ffprobe"):
        return "SKIP: ffprobe unavailable"
    before = set(viewer._PREVIEWS)
    made = []
    try:
        out = _run(fps=24.0, format="MOV ProRes Proxy", save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        path = out.result[1]
        assert path.lower().endswith(".mov") and os.path.isfile(path)
        streams = _ffprobe_streams(path)
        assert streams[0]["codec_name"] == "prores" and streams[0]["profile"] == "Proxy", streams
        assert streams[0]["pix_fmt"] == "yuv422p10le", streams
        # ProRes 4444 keeps alpha when present (yuva444p10le)...
        out = _run(video=torch.rand(2, 8, 8, 4), format="MOV ProRes 4444", save_output=False,
                   filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        streams = _ffprobe_streams(out.result[1])
        assert streams[0]["pix_fmt"].startswith("yuva444p"), streams  # alpha preserved
        # ...and stays true 4:4:4 for RGB-only.
        out = _run(format="MOV ProRes 4444", save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        streams = _ffprobe_streams(out.result[1])
        assert streams[0]["pix_fmt"].startswith("yuv444p"), streams
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "MOV ProRes Proxy + 4444 alpha/RGB pix_fmts verified via ffprobe"


def test_audio():
    if not shutil.which("ffprobe"):
        return "SKIP: ffprobe unavailable"
    before = set(viewer._PREVIEWS)
    made = []
    t = torch.arange(44100, dtype=torch.float32) / 44100.0
    waveform = torch.sin(2 * 3.14159265 * 440 * t).unsqueeze(0).repeat(2, 1).unsqueeze(0)
    audio = {"waveform": waveform, "sample_rate": 44100}
    try:
        out = _run(video=torch.rand(20, 8, 8, 3), fps=20.0, audio=audio, save_output=False,
                   filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        streams = _ffprobe_streams(out.result[1])
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        assert len(audio_streams) == 1 and audio_streams[0]["codec_name"] == "aac", streams
        components = out.result[0].get_components()
        assert components.audio is audio
        assert components.frame_rate == Fraction(20)
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "user audio muxed as aac; output VIDEO components carry the audio"


def test_roots_traversal():
    with tempfile.TemporaryDirectory() as extra:
        os.makedirs(os.path.join(extra, "sub"))
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(extra, "sub", "link"))
        old = os.environ.get("MEDIA_VIDEO_OUTPUT_ROOTS")
        os.environ["MEDIA_VIDEO_OUTPUT_ROOTS"] = extra
        before = set(viewer._PREVIEWS)
        made = []
        try:
            assert viewer._folder("extra-0", "sub") == os.path.realpath(os.path.join(extra, "sub"))
            assert viewer._folder("output", "") == os.path.realpath(viewer.folder_paths.get_output_directory())
            for bad_root, bad_rel in (("nope", ""), ("extra-9", ""), ("extra-0", "../.."),
                                      ("extra-0", os.path.join(extra, "x")), ("extra-0", "a\\b"),
                                      ("extra-0", "sub/link"), ("extra-0", "..")):
                try:
                    viewer._folder(bad_root, bad_rel)
                    raise AssertionError("escape allowed: {!r} {!r}".format(bad_root, bad_rel))
                except ValueError:
                    pass
            out = _run(fps=12.0, save_output=True, output_root="extra-0", subfolder="sub",
                       filename_prefix="vv")
            made.extend(_preview_files(out)); made.append(out.result[1])
            assert out.result[1].startswith(os.path.realpath(os.path.join(extra, "sub"))), out.result[1]
            assert os.path.isfile(out.result[1])
        finally:
            _sweep(made)
            _sweep(set(viewer._PREVIEWS) - before)
            shutil.rmtree(outside, ignore_errors=True)
            if old is None:
                os.environ.pop("MEDIA_VIDEO_OUTPUT_ROOTS", None)
            else:
                os.environ["MEDIA_VIDEO_OUTPUT_ROOTS"] = old
    return "env alternate roots work; symlinked/relative/absolute/unknown escapes all rejected"


def test_output_type():
    before = set(viewer._PREVIEWS)
    made = []
    img = torch.rand(3, 8, 8, 3)
    try:
        out = _run(video=img, fps=30.0, pingpong=True, save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        video_out = out.result[0]
        assert isinstance(video_out, VideoFromComponents), type(video_out)
        components = video_out.get_components()
        assert components.frame_rate == Fraction(30)
        assert components.audio is None
        assert torch.equal(components.images[0], img[0])
        assert torch.equal(components.images[2], img[2])
        assert torch.equal(components.images[-1], img[1]), "pingpong mirrors inner frames"
        assert len(components.images) == 4, "2n-2 frames loop seamlessly back to frame 0"
        assert out.ui["media_video"][0]["frames"] == 4
        # preview-only single file: download_url tokens the temp render itself;
        # download_name keeps the real extension even though saved_path is None.
        item = out.ui["media_video"][0]
        dl = viewer._PREVIEWS[item["download_url"].rsplit("/", 1)[1]]
        assert dl == out.result[1] and os.path.dirname(dl) == viewer.folder_paths.get_temp_directory()
        assert item["download_name"] == os.path.basename(dl) and item["download_name"].endswith(".mp4")
        # Video input with fps=0 keeps native rate and audio; user audio overrides.
        src_audio = {"waveform": torch.zeros(1, 2, 100), "sample_rate": 8000}
        media = VideoFromComponents(VideoComponents(images=img, frame_rate=Fraction(12),
                                                    audio=src_audio))
        out = _run(video=media, fps=0.0, save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        components = out.result[0].get_components()
        assert components.frame_rate == Fraction(12)
        assert components.audio is src_audio
        assert out.ui["media_video"][0]["fps"] == 12.0
        user_audio = {"waveform": torch.ones(1, 2, 50), "sample_rate": 4000}
        out = _run(video=media, fps=0.0, audio=user_audio, save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        assert out.result[0].get_components().audio is user_audio
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "IMAGE input yields real VIDEO; fps/pingpong/audio reflected in components"


def test_reservation_and_failure_cleanup():
    with tempfile.TemporaryDirectory() as folder:
        path = viewer._available(folder, "video", "mp4")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)  # simulate a concurrent run's exclusive reservation
        assert viewer._available(folder, "video", "mp4") != path, "reservation not visible"
        os.unlink(path)
        sibling = os.path.join(folder, "keep_00001.mp4")
        with open(sibling, "wb") as f:
            f.write(b"keep")
        final = viewer._available(folder, "video", "mp4")
        base, ext = os.path.splitext(final)

        def boom(part):
            with open(part, "wb") as f:
                f.write(b"partial")
            raise RuntimeError("boom")

        try:
            viewer._write(final, False, boom)
            raise AssertionError("encode failure must propagate")
        except RuntimeError:
            pass
        assert not os.path.lexists(final), "failed reservation not cleaned"
        assert not os.path.lexists(base + ".part" + ext), "part file not cleaned"
        with open(sibling, "rb") as f:
            assert f.read() == b"keep", "cleanup must only touch own output"
        seq = viewer._available(folder, "video", "exr", sequence=True)
        try:
            viewer._write(seq, True, lambda d: (_ for _ in ()).throw(OSError("boom")))
            raise AssertionError("sequence failure must propagate")
        except OSError:
            pass
        assert not os.path.exists(seq), "failed sequence dir not cleaned"
        viewer._write(final, False, lambda p: open(p, "wb").write(b"done"))
        with open(final, "rb") as f:
            assert f.read() == b"done", "atomic publish failed"
    return "exclusive reservation pre-encode; failure cleans only own output"


def test_overwrite_failure():
    before = set(viewer._PREVIEWS)
    made = []
    try:
        out1 = _run(fps=12.0, save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out1)); made.append(out1.result[1])
        path1 = out1.result[1]
        with open(path1, "rb") as f:
            original = f.read()
        final = viewer._available(viewer.folder_paths.get_temp_directory(), "vv", "mp4")
        with open(final, "wb") as f:
            f.write(b"donotoverwriteme")
        made.append(final)
        out2 = _run(fps=12.0, save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out2)); made.append(out2.result[1])
        assert out2.result[1] != final and out2.result[1] != path1, "occupied name reused"
        with open(final, "rb") as f:
            assert f.read() == b"donotoverwriteme", "existing file overwritten"
        with open(path1, "rb") as f:
            assert f.read() == original
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "existing output names never overwritten; execute rolls to next free name"


def test_fps_and_prefix_validation():
    before = set(viewer._PREVIEWS)
    made = []
    img = torch.rand(2, 8, 8, 3)
    try:
        for bad in (float("nan"), float("inf"), float("-inf"), -1.0, -0.001):
            try:
                _run(video=img, fps=bad, save_output=False)
                raise AssertionError("fps {!r} accepted".format(bad))
            except ValueError:
                pass
        out = _run(video=img, fps=0.0, save_output=False, filename_prefix="vv")
        made.extend(_preview_files(out)); made.append(out.result[1])
        assert out.ui["media_video"][0]["fps"] == 25.0, "IMAGE fps=0 should default to 25"
        for bad in ("", "..", ".", "a/b", "a\\b", "sub\0dir"):
            try:
                _run(video=img, save_output=False, filename_prefix=bad)
                raise AssertionError("prefix {!r} accepted".format(bad))
            except ValueError:
                pass
    finally:
        _sweep(made)
        _sweep(set(viewer._PREVIEWS) - before)
    return "non-finite/non-positive fps rejected; IMAGE fps=0 defaults 25; bad prefixes rejected"


def test_preview_route_practical():
    async def go():
        from aiohttp import web as aioweb
        from aiohttp.test_utils import TestClient, TestServer
        app = aioweb.Application()
        app.router.add_get("/media-video/preview/{token}", viewer.media_video_preview)
        app.router.add_get("/media-video/dirs", viewer.media_video_dirs)
        client = TestClient(TestServer(app))
        await client.start_server()
        before = set(viewer._PREVIEWS)
        made = []
        try:
            # MOV saved as ProRes; served preview must still be browser H264 MP4.
            out = _run(fps=24.0, format="MOV ProRes Proxy", save_output=False, filename_prefix="vv")
            made.extend(_preview_files(out)); made.append(out.result[1])
            item = out.ui["media_video"][0]
            resp = await client.get(item["preview_url"])
            body = await resp.read()
            assert resp.status == 200, resp.status
            assert resp.headers["Content-Type"].startswith("video/mp4"), resp.headers
            assert body[4:8] == b"ftyp", "preview is not an MP4"
            preview_file = viewer._PREVIEWS[item["preview_url"].rsplit("/", 1)[1]]
            streams = _ffprobe_streams(preview_file)
            assert streams is None or streams[0]["codec_name"] == "h264", streams
            # reference plumbing: separate reference_url serves a seekable MP4 too.
            out = _run(fps=24.0, format="MOV ProRes Proxy", save_output=False,
                       reference=torch.rand(2, 8, 8, 3), filename_prefix="vv")
            made.extend(_preview_files(out)); made.append(out.result[1])
            item = out.ui["media_video"][0]
            assert item["reference_url"], "reference_url missing"
            resp = await client.get(item["reference_url"])
            body = await resp.read()
            assert resp.status == 200 and body[4:8] == b"ftyp", resp.status
            # EXR sequence saved; preview still browser H264 MP4.
            out = _run(fps=10.0, format="EXR sequence", save_output=False, filename_prefix="vv")
            made.extend(_preview_files(out)); made.append(out.result[1])
            item = out.ui["media_video"][0]
            assert item["saved_path"] is None
            resp = await client.get(item["preview_url"])
            body = await resp.read()
            assert resp.status == 200 and body[4:8] == b"ftyp", "EXR preview not MP4"
            # dirs listing route.
            resp = await client.get("/media-video/dirs?root=output&path=")
            data = await resp.json()
            assert resp.status == 200 and data["roots"][0]["id"] == "output", data
            resp = await client.get("/media-video/dirs?root=does-not-exist")
            assert resp.status == 400, resp.status
            resp = await client.get("/media-video/preview/not-a-token")
            assert resp.status == 404, resp.status
        finally:
            _sweep(made)
            _sweep(set(viewer._PREVIEWS) - before)
            await client.close()

    asyncio.run(go())
    return "preview/dirs routes serve real H264 MP4 (faststart ftyp) for MOV and EXR; 404/400 handled"


TESTS = [
    test_routes_registered, test_registration, test_png_sequence, test_exr_float, test_mov_prores, test_audio,
    test_roots_traversal, test_output_type, test_reservation_and_failure_cleanup,
    test_overwrite_failure, test_fps_and_prefix_validation, test_preview_route_practical,
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
