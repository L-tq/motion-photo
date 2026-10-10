"""Tests for motion_photo.py.

Run with:  python test_motion_photo.py

All fixtures (a minimal valid JPEG and structurally valid MP4 data) are
synthesised in-memory, so no external media files are required.
"""

import base64
import contextlib
import io
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import motion_photo as mp

# Canonical 1x1 JPEG (SOI, APP0/JFIF, DQT, SOF0, DHT, SOS, entropy, EOI).
MINIMAL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwcJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDs0NDT/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)


def fake_mp4(total: int, seed: int = 1) -> bytes:
    """MP4-looking data: a 24-byte 'ftyp' box followed by deterministic filler."""
    head = (
        (24).to_bytes(4, "big")
        + b"ftyp"
        + b"isom"
        + (0).to_bytes(4, "big")
        + b"isomiso2"
    )
    assert total >= len(head)
    filler = bytes((i * 131 + seed * 17) & 0xFF for i in range(total - len(head)))
    return head + filler


def exif_app1() -> bytes:
    """A minimal EXIF APP1 segment (TIFF header + one IFD entry)."""
    tiff = b"II\x2a\x00" + (8).to_bytes(4, "little")
    ifd = (
        (1).to_bytes(2, "little")                       # IFD entry count
        + struct.pack("<HHIHH", 0x0112, 3, 1, 1, 0)     # Orientation = 1
        + (0).to_bytes(4, "little")                     # next-IFD offset
    )
    payload = b"Exif\x00\x00" + tiff + ifd
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def jpeg_with_exif() -> bytes:
    """The minimal JPEG with an EXIF APP1 inserted after its leading JFIF APP0."""
    events = mp.scan_pre_sos_segments(MINIMAL_JPEG)
    assert events[0][1] == 0xE0  # first segment is APP0 JFIF
    app0_end = events[0][3]
    return MINIMAL_JPEG[:app0_end] + exif_app1() + MINIMAL_JPEG[app0_end:]


def xmp_of(data: bytes):
    """Extract the motion-photo XMP text from assembled output bytes."""
    xmp = mp.extract_xmp(data)
    if xmp is None:
        raise AssertionError("no XMP APP1 segment found")
    return xmp


def xmp_segments(data: bytes):
    return [
        (mpos, seg_end)
        for kind, marker, mpos, seg_end in mp.scan_pre_sos_segments(data)
        if kind == "segment" and marker == 0xE1
        and data[mpos + 4:seg_end].startswith(mp.XMP_NS)
    ]


def exif_segments(data: bytes):
    return [
        (mpos, seg_end)
        for kind, marker, mpos, seg_end in mp.scan_pre_sos_segments(data)
        if kind == "segment" and marker == 0xE1
        and data[mpos + 4:seg_end].startswith(mp.EXIF_NS)
    ]


def box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def tkhd_box(width: int, height: int, rotate: bool = False, version: int = 0) -> bytes:
    identity = [0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000]
    rotated = [0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000]
    payload = (
        (b"\x01" if version == 1 else b"\x00") + b"\x00\x00\x00"  # version/flags
        + b"\x00" * (16 if version == 1 else 8)   # created/modified
        + struct.pack(">I", 1)                     # track_id
        + b"\x00" * 4                              # reserved
        + b"\x00" * (8 if version == 1 else 4)     # duration
        + b"\x00" * 8                              # reserved
        + struct.pack(">hhhh", 0, 0, 0x0100, 0)    # layer/alt/volume/reserved
        + b"".join(struct.pack(">i", v) for v in (rotated if rotate else identity))
        + struct.pack(">II", width << 16, height << 16)
    )
    return box(b"tkhd", payload)


def make_moov_mp4(duration_s: float, timescale: int = 1000, audio: bool = True,
                  version: int = 0, width: int = 0, height: int = 0,
                  rotate: bool = False, tkhd_version: int = 0) -> bytes:
    """MP4-looking bytes with a real moov box; optional video track dimensions."""
    ticks = int(duration_s * timescale)
    if version == 1:
        mvhd = b"\x01\x00\x00\x00" + b"\x00" * 16 + struct.pack(">IQ", timescale, ticks)
    else:
        mvhd = b"\x00" * 12 + struct.pack(">II", timescale, ticks)
    video_payload = (
        tkhd_box(width, height, rotate=rotate, version=tkhd_version)
        if width and height else b""
    ) + box(b"mdia", box(b"hdlr", b"\x00" * 8 + b"vide"))
    video_trak = box(b"trak", video_payload)
    audio_trak = box(b"trak", box(b"mdia", box(b"hdlr", b"\x00" * 8 + b"soun")))
    moov = box(b"moov", box(b"mvhd", mvhd) + video_trak + (audio_trak if audio else b""))
    ftyp = (24).to_bytes(4, "big") + b"ftypisom" + b"\x00\x00\x00\x00" + b"isomiso2"
    return ftyp + moov


FAKE_FFMPEG_SCRIPT = r"""
import sys
from pathlib import Path
args = sys.argv[1:]
dst = args[-1]
payload = ((24).to_bytes(4, "big") + b"ftyp" + b"isom" + b"FAKEFFMPEG"
           + b"\nARGS:" + " ".join(args).encode())
if dst == "-":  # piped raw output (e.g. the bar-detection frame decoder)
    sys.stdout.buffer.write(payload)
elif dst.lower().endswith((".jpg", ".jpeg")):
    # still-image conversion: a real JPEG carrying the recorded argv in a COM
    jpg = JPEG_BYTES
    note = b"ARGS:" + " ".join(args).encode()
    com = b"\xff\xfe" + (len(note) + 2).to_bytes(2, "big") + note
    Path(dst).write_bytes(jpg[:2] + com + jpg[2:])
else:
    Path(dst).write_bytes(payload)
"""


def write_fake_ffmpeg(directory: Path) -> Path:
    """Install a fake ffmpeg into ``directory`` and return its path."""
    script = directory / "_fake_ffmpeg.py"
    script.write_text(FAKE_FFMPEG_SCRIPT.replace("JPEG_BYTES", repr(MINIMAL_JPEG)))
    if os.name == "nt":
        bat = directory / "ffmpeg.bat"
        bat.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script))
        return bat
    sh = directory / "ffmpeg"
    sh.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
    sh.chmod(0o755)
    return sh


def png_bytes(width=100, height=50, color_type=2, trns=False):
    """Minimal PNG: signature + IHDR (+ optional tRNS) + IDAT + IEND."""
    def chunk(ctype, payload=b""):
        return len(payload).to_bytes(4, "big") + ctype + payload + b"\x00\x00\x00\x00"

    ihdr = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + b"\x08" + bytes([color_type]) + b"\x00\x00\x00")
    parts = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", ihdr)]
    if trns:
        parts.append(chunk(b"tRNS", b"\x00\x00\x00\x00"))
    parts.append(chunk(b"IDAT", b"\x00" * 16))
    parts.append(chunk(b"IEND"))
    return b"".join(parts)


class FixtureTests(unittest.TestCase):
    def test_fixtures_are_valid(self):
        self.assertTrue(MINIMAL_JPEG.startswith(b"\xff\xd8\xff\xe0"))
        self.assertTrue(MINIMAL_JPEG.endswith(b"\xff\xd9"))
        self.assertGreater(mp.find_jpeg_eoi(MINIMAL_JPEG), 0)
        self.assertTrue(mp.looks_like_mp4(fake_mp4(64)))
        # the EXIF variant must still parse and keep its JFIF first
        with_exif = jpeg_with_exif()
        events = mp.scan_pre_sos_segments(with_exif)
        self.assertEqual(events[0][1], 0xE0)
        self.assertEqual(events[1][1], 0xE1)
        self.assertTrue(with_exif.startswith(b"\xff\xd8\xff\xe0"))
        self.assertTrue(with_exif.endswith(b"\xff\xd9"))

    def test_image_dimensions(self):
        width, height = mp.image_dimensions(MINIMAL_JPEG)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)


class CreateTests(unittest.TestCase):
    def test_roundtrip_minimal_jpeg(self):
        video = fake_mp4(2048)
        out = mp.create_motion_photo(MINIMAL_JPEG, video)
        self.assertTrue(out.startswith(b"\xff\xd8"))
        eoi = mp.find_jpeg_eoi(out)
        self.assertEqual(out[eoi:], video)  # video appended verbatim after EOI
        self.assertEqual(len(xmp_segments(out)), 1)

        items = mp.parse_container_items(xmp_of(out))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["Semantic"], "Primary")
        self.assertEqual(items[0]["Mime"], "image/jpeg")
        self.assertEqual(items[1]["Semantic"], "MotionPhoto")
        self.assertEqual(items[1]["Mime"], "video/mp4")
        self.assertEqual(int(items[0]["Length"]), eoi)  # primary == JPEG size incl. XMP
        self.assertEqual(int(items[1]["Length"]), len(video))
        self.assertEqual(len(out), eoi + len(video))

        micro = mp.parse_micro_video(xmp_of(out))
        self.assertEqual(micro, {"Version": 1, "Offset": len(video)})

    def test_xmp_inserted_after_jfif_and_exif(self):
        source = jpeg_with_exif()
        exif_span = exif_segments(source)[0]
        out = mp.create_motion_photo(source, fake_mp4(512))
        xmp_span = xmp_segments(out)[0]
        self.assertGreaterEqual(xmp_span[0], exif_span[1])  # XMP comes after EXIF
        self.assertEqual(len(exif_segments(out)), 1)   # EXIF preserved intact
        self.assertTrue(out.startswith(b"\xff\xd8\xff\xe0"))  # JFIF still first

    def test_replaces_existing_xmp_and_video(self):
        first, second = fake_mp4(1024), fake_mp4(4096, seed=2)
        out1 = mp.create_motion_photo(MINIMAL_JPEG, first)
        out2 = mp.create_motion_photo(out1, second)  # feed output back as image
        eoi = mp.find_jpeg_eoi(out2)
        self.assertEqual(out2[eoi:], second)  # old video replaced, not kept
        self.assertEqual(len(xmp_segments(out2)), 1)  # exactly one XMP segment
        items = mp.parse_container_items(xmp_of(out2))
        self.assertEqual(int(items[0]["Length"]), eoi)
        self.assertEqual(int(items[1]["Length"]), len(second))
        self.assertNotIn(first, out2)

    def test_garbage_trailer_is_dropped(self):
        dirty = MINIMAL_JPEG + b"NOT-A-VIDEO-TRAILER" * 10
        out = mp.create_motion_photo(dirty, fake_mp4(300))
        eoi = mp.find_jpeg_eoi(out)
        self.assertEqual(out[eoi:], fake_mp4(300))
        self.assertNotIn(b"NOT-A-VIDEO-TRAILER", out)

    def test_length_consistency_across_digit_boundaries(self):
        # the primary Item:Length embeds its own rendered width; stress it
        for size in (24, 100, 1000, 9999, 10000, 65535, 65536, 100000):
            with self.subTest(video_size=size):
                out = mp.create_motion_photo(MINIMAL_JPEG, fake_mp4(size))
                items = mp.parse_container_items(xmp_of(out))
                primary, motion = int(items[0]["Length"]), int(items[1]["Length"])
                self.assertEqual(primary + motion, len(out))
                self.assertEqual(out[primary - 2:primary], b"\xff\xd9")
                self.assertEqual(mp.parse_micro_video(xmp_of(out))["Offset"], size)

    def test_build_xmp_app1_self_consistent(self):
        segment = mp.build_xmp_app1(len(MINIMAL_JPEG), 1000)
        assembled = MINIMAL_JPEG[:2] + segment + MINIMAL_JPEG[2:]
        items = mp.parse_container_items(mp.extract_xmp(assembled))
        self.assertEqual(int(items[0]["Length"]), len(assembled))

    def test_invalid_inputs(self):
        with self.assertRaises(mp.MotionPhotoError):
            mp.create_motion_photo(b"GIF89a" + b"\x00" * 20, fake_mp4(64))
        with self.assertRaises(mp.MotionPhotoError):
            mp.create_motion_photo(MINIMAL_JPEG[:-2], fake_mp4(64))  # no EOI
        with self.assertRaises(mp.MotionPhotoError):
            mp.create_motion_photo(MINIMAL_JPEG, b"")  # empty video
        with self.assertRaises(mp.MotionPhotoError):
            mp.wrap_xmp_app1(b"x" * 70000)  # over the APP1 size limit


class LookLikeMp4Tests(unittest.TestCase):
    def test_ftyp_detection(self):
        self.assertTrue(mp.looks_like_mp4(fake_mp4(24)))
        self.assertFalse(mp.looks_like_mp4(b"abcd" * 3))
        self.assertFalse(mp.looks_like_mp4(b"\x00\x00\x00\x10ftyp"))  # too short
        self.assertFalse(mp.looks_like_mp4(b""))


class InspectTests(unittest.TestCase):
    def test_inspect_assembled_file(self):
        video = fake_mp4(1536)
        out = mp.create_motion_photo(jpeg_with_exif(), video)
        report = mp.inspect_motion_photo(out)
        self.assertIsNotNone(report["xmp"])
        self.assertEqual(report["still_end"], len(out) - len(video))
        self.assertEqual(report["trailer_len"], len(video))
        self.assertTrue(report["checks"])
        for label, ok in report["checks"]:
            self.assertTrue(ok, label)

    def test_inspect_plain_jpeg(self):
        report = mp.inspect_motion_photo(MINIMAL_JPEG)
        self.assertIsNone(report["xmp"])
        self.assertFalse(all(ok for _label, ok in report["checks"]))

    def test_inspect_detects_wrong_offsets(self):
        out = bytearray(mp.create_motion_photo(MINIMAL_JPEG, fake_mp4(512)))
        # corrupt the container primary length by one byte region: rebuild with
        # a truncated trailer so declared sizes no longer match the file
        broken = bytes(out[: mp.find_jpeg_eoi(out) + 100])
        report = mp.inspect_motion_photo(broken)
        self.assertFalse(all(ok for _label, ok in report["checks"]))


class CliTests(unittest.TestCase):
    def test_create_and_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            video = Path(tmp) / "clip.mp4"
            image.write_bytes(jpeg_with_exif())
            video.write_bytes(fake_mp4(777))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(video)])
            self.assertEqual(code, 0)
            output = Path(tmp) / "photo_motion.jpg"
            self.assertTrue(output.exists())
            result = output.read_bytes()
            self.assertEqual(result[mp.find_jpeg_eoi(result):], fake_mp4(777))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main(["--info", str(output)])
            self.assertEqual(code, 0)
            self.assertIn("MotionPhoto", stdout.getvalue())
            self.assertIn("Primary", stdout.getvalue())       # container directory
            self.assertNotIn("MicroVideo", stdout.getvalue())  # honor default

    def test_info_plain_jpeg_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.jpg"
            plain.write_bytes(MINIMAL_JPEG)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main(["--info", str(plain)])
            self.assertEqual(code, 1)
            self.assertIn("not a motion photo", stdout.getvalue())

    def test_bad_file_errors(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = mp.main(["nope.jpg", "novid.mp4"])
        self.assertEqual(code, 2)

    def test_format_google_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            video = Path(tmp) / "clip.mp4"
            image.write_bytes(MINIMAL_JPEG)
            video.write_bytes(fake_mp4(500))
            out = Path(tmp) / "out.jpg"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(video),
                                "--format", "google", "-o", str(out)])
            self.assertEqual(code, 0)
            result = out.read_bytes()
            self.assertIn("MicroVideo", xmp_of(result))
            report = mp.inspect_motion_photo(result)
            self.assertTrue(all(ok for _label, ok in report["checks"]))

    def test_subprocess_smoke(self):
        script = Path(__file__).resolve().parent / "motion_photo.py"
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "img.jpg"
            video = Path(tmp) / "vid.mp4"
            image.write_bytes(MINIMAL_JPEG)
            video.write_bytes(fake_mp4(1500))
            run = subprocess.run(
                [sys.executable, str(script), str(image), str(video)],
                capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            output = Path(tmp) / "img_motion.jpg"
            self.assertTrue(output.exists())
            run = subprocess.run(
                [sys.executable, str(script), "--info", str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("[ok]", run.stdout)


class Mp4ProbeTests(unittest.TestCase):
    def test_duration_and_audio(self):
        probe = mp.probe_mp4(make_moov_mp4(10.0))
        self.assertAlmostEqual(probe["duration"], 10.0)
        self.assertTrue(probe["has_audio"])

    def test_version1_without_audio(self):
        probe = mp.probe_mp4(make_moov_mp4(7.5, audio=False, version=1, timescale=600))
        self.assertAlmostEqual(probe["duration"], 7.5)
        self.assertFalse(probe["has_audio"])

    def test_no_moov(self):
        probe = mp.probe_mp4(fake_mp4(200))
        self.assertIsNone(probe["duration"])
        self.assertFalse(probe["has_audio"])

    def test_video_display_dimensions(self):
        probe = mp.probe_mp4(make_moov_mp4(10.0, width=832, height=1280))
        self.assertEqual((probe["width"], probe["height"]), (832, 1280))

    def test_rotated_dimensions_swapped(self):
        probe = mp.probe_mp4(make_moov_mp4(10.0, width=1920, height=1080, rotate=True))
        self.assertEqual((probe["width"], probe["height"]), (1080, 1920))

    def test_tkhd_version1(self):
        probe = mp.probe_mp4(make_moov_mp4(10.0, width=640, height=480, tkhd_version=1))
        self.assertEqual((probe["width"], probe["height"]), (640, 480))

    def test_dimensions_absent(self):
        probe = mp.probe_mp4(make_moov_mp4(10.0))
        self.assertIsNone(probe["width"])
        self.assertIsNone(probe["height"])


class AnalyzeBlackBarsTests(unittest.TestCase):
    @staticmethod
    def frame(width=40, height=30, bars=(0, 0, 0, 0), content=100):
        top, bottom, left, right = bars
        rows = []
        for y in range(height):
            if y < top or y >= height - bottom:
                rows.append(bytes([5]) * width)
                continue
            row = [content] * width
            for x in range(left):
                row[x] = 5
            for x in range(width - right, width):
                row[x] = 5
            rows.append(bytes(row))
        return b"".join(rows)

    def test_detects_uniform_bars(self):
        bars = mp.analyze_black_bars(self.frame(bars=(6, 4, 6, 0)), 40, 30)
        self.assertEqual(bars, (6, 4, 6, 0))

    def test_dark_textured_content_is_not_a_bar(self):
        # mean ~40 (dark) but heavily textured -> real content, not a bar
        row = bytes((v * 7) % 60 + 10 for v in range(40))
        frame = row * 30
        self.assertEqual(mp.analyze_black_bars(frame, 40, 30), (0, 0, 0, 0))

    def test_all_black_frame_is_clamped(self):
        bars = mp.analyze_black_bars(bytes(40 * 30), 40, 30)
        self.assertEqual(bars, (6, 6, 8, 8))  # capped at 20% per side, even

    def test_clean_frame_has_no_bars(self):
        self.assertEqual(mp.analyze_black_bars(self.frame(), 40, 30), (0, 0, 0, 0))

    def test_narrow_strips_ignored(self):
        # 3 px bar < BAR_MIN_PIXELS -> left alone entirely
        bars = mp.analyze_black_bars(self.frame(bars=(3, 0, 0, 0)), 40, 30)
        self.assertEqual(bars, (0, 0, 0, 0))


class CommandBuilderTests(unittest.TestCase):
    def test_atempo_chain(self):
        self.assertEqual(mp.build_atempo_chain(1.5), "atempo=1.500000")
        self.assertEqual(mp.build_atempo_chain(3.5), "atempo=2.000000,atempo=1.750000")
        stages = [float(p.split("=")[1]) for p in mp.build_atempo_chain(8.0).split(",")]
        self.assertEqual(len(stages), 3)
        self.assertTrue(all(s <= 2.0 for s in stages))
        self.assertAlmostEqual(math.prod(stages), 8.0, places=4)

    def _cmd(self, **kwargs):
        return " ".join(mp.build_transcode_cmd(Path("ffmpeg"), Path("in.mp4"),
                                               Path("out.mp4"), **kwargs))

    def test_speed_only_command(self):
        joined = self._cmd(speed=3.5, compress=False, has_audio=True)
        self.assertIn("-vf setpts=PTS/3.500000", joined)
        self.assertIn("-af atempo=2.000000,atempo=1.750000", joined)
        self.assertIn("-crf 18", joined)
        self.assertIn("-r 30", joined)
        self.assertNotIn("scale=", joined)

    def test_compress_only_command(self):
        joined = self._cmd(speed=None, compress=True, has_audio=False)
        self.assertIn(
            "scale='min(1440,iw)':'min(1440,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2",
            joined,
        )
        self.assertIn("-crf 28", joined)
        self.assertNotIn("setpts", joined)
        self.assertNotIn(" -af ", joined)
        self.assertNotIn(" -r 30", joined)

    def test_no_audio_omits_atempo(self):
        joined = self._cmd(speed=2.0, compress=True, has_audio=False)
        self.assertNotIn(" -af ", joined)
        self.assertIn("-map 0:a:0?", joined)  # audio mapping stays optional

    def test_match_aspect_crop_filter(self):
        joined = self._cmd(speed=None, compress=True, has_audio=False,
                           match_aspect=0.666667)
        self.assertIn(
            "crop='2*trunc(min(iw,ih*0.666667)/2+0.5)':"
            "'2*trunc(min(ih,iw/0.666667)/2+0.5)'",
            joined,
        )
        self.assertLess(joined.index("crop="), joined.index("scale="))

    def test_match_aspect_stretch_filter(self):
        joined = self._cmd(speed=None, compress=False, has_audio=False,
                           match_aspect=0.666667, match_mode="stretch")
        self.assertIn(
            "scale='2*trunc(min(iw,ih*0.666667)/2+0.5)':"
            "'2*trunc(min(ih,iw/0.666667)/2+0.5)'",
            joined,
        )
        self.assertNotIn("crop=", joined)  # same window, squeezed not trimmed

    def test_trim_crop_filter(self):
        joined = self._cmd(speed=2.0, compress=False, has_audio=False, trim=(10, 0, 4, 2))
        self.assertIn("crop=iw-6:ih-10:4:10", joined)
        # the edge trim runs before any aspect window
        joined_all = self._cmd(speed=2.0, compress=True, has_audio=False,
                               match_aspect=0.666667, trim=(10, 0, 4, 2))
        self.assertLess(joined_all.index("crop=iw-6"),
                        joined_all.index("min(iw,ih*0.666667)"))

    def test_volume_zero_drops_audio(self):
        joined = self._cmd(speed=None, compress=True, has_audio=True, volume=0.0)
        self.assertIn("-an", joined)
        self.assertNotIn("0:a:0?", joined)
        self.assertNotIn(" -af ", joined)
        self.assertNotIn("-b:a", joined)

    def test_volume_scales_audio(self):
        joined = self._cmd(speed=3.5, compress=False, has_audio=True, volume=0.5)
        self.assertIn("-af atempo=2.000000,atempo=1.750000,volume=0.5", joined)
        self.assertIn("-b:a 128k", joined)
        self.assertNotIn("-an", joined)

    def test_volume_one_keeps_plain_audio(self):
        joined = self._cmd(speed=None, compress=True, has_audio=True, volume=1.0)
        self.assertIn("-map 0:a:0?", joined)
        self.assertIn("-b:a 96k", joined)
        self.assertNotIn("-an", joined)
        self.assertNotIn("volume=", joined)

    def test_audio_remux_mute_command(self):
        joined = " ".join(mp.build_audio_remux_cmd(
            Path("ffmpeg"), Path("in.mp4"), Path("out.mp4"), volume=0.0))
        self.assertIn("-map 0:v:0 -c:v copy -an", joined)
        self.assertNotIn("-af", joined)
        self.assertNotIn("libx264", joined)
        self.assertIn("-movflags +faststart", joined)

    def test_audio_remux_scale_command(self):
        joined = " ".join(mp.build_audio_remux_cmd(
            Path("ffmpeg"), Path("in.mp4"), Path("out.mp4"), volume=0.7))
        self.assertIn("-map 0:v:0 -c:v copy", joined)
        self.assertIn("-af volume=0.7", joined)
        self.assertIn("-c:a aac -b:a 128k", joined)
        self.assertNotIn("-an", joined)
        self.assertNotIn("libx264", joined)


class ProcessVideoTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ffmpeg = write_fake_ffmpeg(self.dir)

    def test_speeds_up_long_video(self):
        video = self.dir / "long.mp4"
        video.write_bytes(make_moov_mp4(10.0))
        processed, report = mp.process_video(video, self.ffmpeg, target_duration=3.0)
        self.assertTrue(report["changed"])
        self.assertAlmostEqual(report["speed"], 10 / 3, places=3)
        self.assertTrue(mp.looks_like_mp4(processed))
        self.assertIn(b"setpts=PTS/3.333333", processed)
        self.assertIn(b"atempo=2.000000,atempo=1.666667", processed)
        self.assertIn(b"-r 30", processed)

    def test_short_video_left_unchanged(self):
        video = self.dir / "short.mp4"
        original = make_moov_mp4(2.0)
        video.write_bytes(original)
        processed, report = mp.process_video(video, self.ffmpeg, target_duration=3.0)
        self.assertFalse(report["changed"])
        self.assertIsNone(report["speed"])
        self.assertEqual(processed, original)

    def test_compress_only(self):
        video = self.dir / "any.mp4"
        video.write_bytes(make_moov_mp4(4.0))
        processed, report = mp.process_video(video, self.ffmpeg, compress=True)
        self.assertTrue(report["changed"])
        self.assertIsNone(report["speed"])
        self.assertIn(b"scale='min(1440,iw)'", processed)
        self.assertIn(b"-crf 28", processed)
        self.assertNotIn(b"setpts", processed)

    def test_speed_without_audio_has_no_atempo(self):
        video = self.dir / "silent.mp4"
        video.write_bytes(make_moov_mp4(9.0, audio=False))
        processed, _ = mp.process_video(video, self.ffmpeg, target_duration=3.0)
        self.assertNotIn(b"atempo", processed)

    def test_unprobeable_duration_errors(self):
        video = self.dir / "broken.mp4"
        video.write_bytes(fake_mp4(300))  # ftyp but no moov
        with self.assertRaises(mp.MotionPhotoError):
            mp.process_video(video, self.ffmpeg, target_duration=3.0)

    def test_match_aspect_alone_reencodes(self):
        video = self.dir / "any.mp4"
        video.write_bytes(make_moov_mp4(4.0))
        processed, report = mp.process_video(video, self.ffmpeg, match_aspect=0.6667)
        self.assertTrue(report["changed"])
        self.assertIn(b"crop=", processed)
        self.assertIn(b"-crf 18", processed)  # aspect-only keeps high quality
        self.assertNotIn(b"scale=", processed)

    def test_volume_zero_mutes_when_reencoding(self):
        video = self.dir / "any.mp4"
        video.write_bytes(make_moov_mp4(4.0))  # has an audio track
        processed, report = mp.process_video(video, self.ffmpeg, compress=True,
                                             volume=0.0)
        self.assertTrue(report["changed"])
        self.assertEqual(report["audio"], "muted")
        self.assertIn(b"-an", processed)
        self.assertNotIn(b"-b:a", processed)
        self.assertNotIn(b"atempo", processed)

    def test_volume_scales_when_reencoding(self):
        video = self.dir / "long.mp4"
        video.write_bytes(make_moov_mp4(10.0))
        processed, report = mp.process_video(video, self.ffmpeg, target_duration=3.0,
                                             volume=0.4)
        self.assertEqual(report["audio"], 0.4)
        self.assertIn(b"atempo=2.000000,atempo=1.666667,volume=0.4", processed)
        self.assertIn(b"-b:a 128k", processed)

    def test_audio_pass_alone_remuxes_without_reencoding(self):
        video = self.dir / "any.mp4"
        video.write_bytes(make_moov_mp4(4.0))
        processed, report = mp.process_video(video, self.ffmpeg, volume=0.5,
                                             audio_pass=True)
        self.assertTrue(report["changed"])
        self.assertIsNone(report["speed"])
        self.assertEqual(report["audio"], 0.5)
        self.assertIn(b"-c:v copy", processed)  # video stream untouched
        self.assertIn(b"volume=0.5", processed)
        self.assertNotIn(b"libx264", processed)

    def test_audio_pass_with_original_volume_is_a_no_op(self):
        video = self.dir / "any.mp4"
        original = make_moov_mp4(4.0)
        video.write_bytes(original)
        processed, report = mp.process_video(video, self.ffmpeg, volume=1.0,
                                             audio_pass=True)
        self.assertFalse(report["changed"])
        self.assertIsNone(report["audio"])
        self.assertEqual(processed, original)

    def test_volume_without_audio_pass_needs_other_processing(self):
        # a bare volume level never forces an ffmpeg pass on its own
        video = self.dir / "any.mp4"
        original = make_moov_mp4(4.0)
        video.write_bytes(original)
        processed, report = mp.process_video(video, self.ffmpeg, volume=0.5)
        self.assertFalse(report["changed"])
        self.assertEqual(processed, original)

    def test_missing_ffmpeg_errors(self):
        with self.assertRaises(mp.MotionPhotoError) as ctx:
            mp.resolve_ffmpeg(str(self.dir / "nope" / "ffmpeg.exe"))
        self.assertIn("not found", str(ctx.exception))


class CliProcessingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fake_ffmpeg = write_fake_ffmpeg(self.dir)
        self.image = self.dir / "photo.jpg"
        self.image.write_bytes(jpeg_with_exif())

    def _video(self, name="clip.mp4", duration=10.0, **kwargs):
        video = self.dir / name
        video.write_bytes(make_moov_mp4(duration, **kwargs))
        return video

    def _trailer(self, name="photo_motion.jpg"):
        output = (self.dir / name).read_bytes()
        return output[mp.find_jpeg_eoi(output):]

    def test_duration_flag_speeds_up(self):
        video = self._video()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--duration",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        output = (self.dir / "photo_motion.jpg").read_bytes()
        trailer = output[mp.find_jpeg_eoi(output):]
        self.assertIn(b"setpts=PTS/3.333333", trailer)
        self.assertIn(b"-crf 18", trailer)  # speed-only keeps high quality
        self.assertIn("sped up 3.33x to 3s", stdout.getvalue())

    def test_compress_flag_reencodes(self):
        video = self._video(duration=3.0)  # short enough: no speed-up
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--compress",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        output = (self.dir / "photo_motion.jpg").read_bytes()
        trailer = output[mp.find_jpeg_eoi(output):]
        self.assertIn(b"scale='min(1440,iw)'", trailer)
        self.assertIn(b"-crf 28", trailer)
        self.assertNotIn(b"setpts", trailer)

    def test_compress_mutes_by_default(self):
        video = self._video(duration=3.0)  # has an audio track
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--compress",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        trailer = self._trailer()
        self.assertIn(b"-an", trailer)
        self.assertNotIn(b"-b:a", trailer)
        self.assertIn("audio muted", stdout.getvalue())

    def test_volume_one_keeps_original_sound(self):
        video = self._video(duration=3.0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--compress",
                            "--volume", "1", "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        trailer = self._trailer()
        self.assertIn(b"-map 0:a:0?", trailer)
        self.assertIn(b"-b:a 96k", trailer)
        self.assertNotIn("audio muted", stdout.getvalue())

    def test_volume_level_alone_runs_audio_only_pass(self):
        video = self._video(duration=3.0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--volume", "0.5",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        trailer = self._trailer()
        self.assertIn(b"-c:v copy", trailer)
        self.assertIn(b"volume=0.5", trailer)
        self.assertNotIn(b"libx264", trailer)
        self.assertIn("audio volume set to 0.5", stdout.getvalue())

    def test_volume_zero_alone_mutes_without_reencoding(self):
        video = self._video(duration=3.0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--volume", "0",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        trailer = self._trailer()
        self.assertIn(b"-c:v copy", trailer)
        self.assertIn(b"-an", trailer)
        self.assertIn("audio muted", stdout.getvalue())

    def test_volume_one_alone_needs_no_ffmpeg(self):
        video = self._video(duration=3.0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            # a bogus --ffmpeg path would fail hard if any pass were attempted
            code = mp.main([str(self.image), str(video), "--volume", "1",
                            "--ffmpeg", str(self.dir / "absent.exe")])
        self.assertEqual(code, 0)
        self.assertEqual(self._trailer(), video.read_bytes())  # untouched embed

    def test_negative_volume_fails(self):
        video = self._video(duration=3.0)
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            mp.main([str(self.image), str(video), "--volume", "-0.5",
                     "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(ctx.exception.code, 2)

    def test_short_video_reports_unchanged(self):
        video = self._video(duration=2.0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--duration",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn("embedded unchanged", stdout.getvalue())
        output = (self.dir / "photo_motion.jpg").read_bytes()
        self.assertEqual(output[mp.find_jpeg_eoi(output):], video.read_bytes())

    def test_missing_ffmpeg_fails(self):
        video = self._video()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = mp.main([str(self.image), str(video), "--compress",
                            "--ffmpeg", str(self.dir / "absent.exe")])
        self.assertEqual(code, 2)
        self.assertIn("not found", stderr.getvalue())

    def test_bad_duration_fails(self):
        video = self._video()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = mp.main([str(self.image), str(video), "--duration", "0",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 2)
        self.assertIn("positive", stderr.getvalue())

    def test_aspect_cropped_by_default(self):
        video = self._video(duration=2.0, width=832, height=1280)  # AR 0.65 vs 1x1 photo
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn("cropped to the photo's aspect ratio", stdout.getvalue())
        self.assertIn(b"crop=", self._trailer())
        self.assertIn(b"-crf 18", self._trailer())  # crop-only keeps high quality

    def test_no_match_aspect_opts_out(self):
        video = self._video(duration=2.0, width=832, height=1280)
        with contextlib.redirect_stdout(io.StringIO()):
            code = mp.main([str(self.image), str(video), "--no-match-aspect"])
        self.assertEqual(code, 0)
        self.assertEqual(self._trailer(), video.read_bytes())  # embedded untouched

    def test_matching_aspect_skips_ffmpeg_entirely(self):
        video = self._video(duration=2.0, width=1000, height=1000)  # AR 1 == 1x1 photo
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            # a bogus --ffmpeg path would fail hard if any processing were attempted
            code = mp.main([str(self.image), str(video),
                            "--ffmpeg", str(self.dir / "absent.exe")])
        self.assertEqual(code, 0)
        self.assertIn("aspect already matches", stdout.getvalue())
        self.assertEqual(self._trailer(), video.read_bytes())

    def test_stretch_method(self):
        video = self._video(duration=2.0, width=832, height=1280)  # AR 0.65 vs 1x1 photo
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--stretch",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn("stretched to the photo's aspect ratio", stdout.getvalue())
        trailer = self._trailer()
        self.assertIn(b"scale='2*trunc(min(iw,ih*1.000000)/2+0.5)'", trailer)
        self.assertNotIn(b"crop=", trailer)
        self.assertIn(b"-crf 18", trailer)  # still a quality-preserving re-encode

    def test_stretch_requires_matching(self):
        video = self._video(duration=2.0)
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            mp.main([str(self.image), str(video), "--stretch", "--no-match-aspect"])
        self.assertEqual(ctx.exception.code, 2)

    def test_manual_crop(self):
        video = self._video(duration=2.0, width=1000, height=1000)  # matches 1x1 photo
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(self.image), str(video), "--crop", "8:0:4:0",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn("trimmed edges (8 px top, 4 px left)", stdout.getvalue())
        self.assertIn(b"crop=iw-4:ih-8:4:8", self._trailer())

    def test_manual_crop_rounds_up_to_even(self):
        video = self._video(duration=2.0, width=1000, height=1000)
        with contextlib.redirect_stdout(io.StringIO()):
            code = mp.main([str(self.image), str(video), "--crop", "7:0:3:0",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn(b"crop=iw-4:ih-8:4:8", self._trailer())  # 7->8, 3->4

    def test_manual_crop_parse_error(self):
        video = self._video(duration=2.0)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = mp.main([str(self.image), str(video), "--crop", "8:0",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 2)
        self.assertIn("TOP:BOTTOM:LEFT:RIGHT", stderr.getvalue())

    def test_manual_crop_too_large(self):
        video = self._video(duration=2.0, width=832, height=1280)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = mp.main([str(self.image), str(video), "--crop", "1300:0:0:0",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 2)
        self.assertIn("entire frame", stderr.getvalue())

    def test_crop_bars_unanalyzable_warns_and_skips(self):
        video = self.dir / "weird.mp4"
        video.write_bytes(fake_mp4(300))  # ftyp but no moov: dims unknown
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mp.main([str(self.image), str(video), "--crop-bars",
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn("could not analyse", stderr.getvalue())
        self.assertEqual(self._trailer(), video.read_bytes())  # embedded unchanged

    def test_mismatch_without_ffmpeg_mentions_opt_out(self):
        video = self._video(duration=2.0, width=832, height=1280)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = mp.main([str(self.image), str(video),
                            "--ffmpeg", str(self.dir / "absent.exe")])
        self.assertEqual(code, 2)
        self.assertIn("--no-match-aspect", stderr.getvalue())


class StillConversionTests(unittest.TestCase):
    """Non-JPEG stills (PNG above all) are converted to JPEG via ffmpeg."""

    def test_detect_still_format(self):
        cases = [
            (png_bytes(), "PNG"),
            (b"RIFF$\x00\x00\x00WEBPVP8 ", "WebP"),
            (b"GIF89a\x01\x02\x03", "GIF"),
            (b"GIF87a\x01\x02\x03", "GIF"),
            (b"BM6\x00\x00\x00" + b"\x00" * 30, "BMP"),
            (b"II*\x00\x08\x00\x00\x00", "TIFF"),
            (b"MM\x00*\x08\x00", "TIFF"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 16, None),   # JPEG: no conversion
            (b"RIFF$\x00\x00\x00WAVEfmt ", None),         # audio, not a still
            (b"just some text", None),
            (b"", None),
        ]
        for data, expected in cases:
            self.assertEqual(mp.detect_still_format(data), expected, data[:12])

    def test_png_has_alpha(self):
        self.assertTrue(mp.png_has_alpha(png_bytes(color_type=6)))   # RGBA
        self.assertTrue(mp.png_has_alpha(png_bytes(color_type=4)))   # gray+alpha
        self.assertFalse(mp.png_has_alpha(png_bytes(color_type=2)))  # RGB
        self.assertFalse(mp.png_has_alpha(png_bytes(color_type=0)))  # grayscale
        self.assertTrue(mp.png_has_alpha(png_bytes(color_type=3, trns=True)))
        self.assertFalse(mp.png_has_alpha(png_bytes(color_type=3)))
        self.assertTrue(mp.png_has_alpha(png_bytes(color_type=2, trns=True)))
        self.assertFalse(mp.png_has_alpha(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20))
        self.assertFalse(mp.png_has_alpha(b"not a png at all"))

    def test_png_ihdr_dims(self):
        self.assertEqual(mp._png_ihdr(png_bytes(width=640, height=480)),
                         (640, 480, 2))
        self.assertIsNone(mp._png_ihdr(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20))


class PngInputCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fake_ffmpeg = write_fake_ffmpeg(self.dir)
        self.video = self.dir / "clip.mp4"
        self.video.write_bytes(make_moov_mp4(3.0))  # no dims: aspect check skipped

    def _still(self, name="photo_motion.jpg"):
        output = (self.dir / name).read_bytes()
        return output[:mp.find_jpeg_eoi(output)]

    def test_png_input_converted_and_embedded(self):
        png = self.dir / "photo.png"
        png.write_bytes(png_bytes(color_type=2))  # opaque: plain re-encode
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mp.main([str(png), str(self.video),
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        self.assertIn("converted PNG -> JPEG", stdout.getvalue())
        still = self._still()
        self.assertTrue(still.startswith(mp.SOI))
        self.assertIn(b"ARGS:", still)          # produced by the fake converter
        self.assertIn(b"-q:v 2", still)         # recorded argv: high quality
        self.assertIn(b"yuvj420p", still)
        self.assertNotIn(b"filter_complex", still)
        output = (self.dir / "photo_motion.jpg").read_bytes()
        self.assertEqual(output[mp.find_jpeg_eoi(output):], self.video.read_bytes())

    def test_transparent_png_gets_flatten_filter(self):
        png = self.dir / "alpha.png"
        png.write_bytes(png_bytes(width=64, height=32, color_type=6))
        with contextlib.redirect_stdout(io.StringIO()):
            code = mp.main([str(png), str(self.video),
                            "--ffmpeg", str(self.fake_ffmpeg)])
        self.assertEqual(code, 0)
        still = self._still("alpha_motion.jpg")
        self.assertIn(b"filter_complex", still)
        self.assertIn(b"color=c=white:s=64x32", still)  # flattened onto white
        self.assertIn(b"overlay", still)

    def test_png_without_ffmpeg_fails_with_hint(self):
        png = self.dir / "photo.png"
        png.write_bytes(png_bytes())
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            code = mp.main([str(png), str(self.video),
                            "--ffmpeg", str(self.dir / "absent.exe")])
        self.assertEqual(code, 2)
        self.assertIn("ffmpeg", stderr.getvalue())

    def test_unsupported_image_format_fails(self):
        raw = self.dir / "photo.dat"
        raw.write_bytes(b"\x00\x01\x02 not an image")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            code = mp.main([str(raw), str(self.video)])
        self.assertEqual(code, 2)
        self.assertIn("not a JPEG", stderr.getvalue())


class AppleFormatTests(unittest.TestCase):
    ASSET = "B96E1C6B-4C8E-4A32-9F0D-1234567890AB"

    @staticmethod
    def find_child(data, start, end, box_type):
        for found, s, e in mp._iter_boxes(data, start, end):
            if found == box_type:
                return s, e
        return None

    def parse_exif(self, jpeg):
        for kind, marker, mpos, seg_end in mp.scan_pre_sos_segments(jpeg):
            if kind == "segment" and marker == 0xE1 \
                    and jpeg[mpos + 4:seg_end].startswith(mp.EXIF_NS):
                return jpeg[mpos + 10:seg_end]
        return None

    def test_exif_app1_structure(self):
        seg = mp.build_apple_exif_app1(self.ASSET)
        self.assertTrue(seg.startswith(b"\xff\xe1"))
        self.assertEqual(int.from_bytes(seg[2:4], "big") + 2, len(seg))
        tiff = seg[10:]
        self.assertTrue(tiff.startswith(b"MM\x00\x2a"))
        count = int.from_bytes(tiff[8:10], "big")
        self.assertEqual(count, 2)
        entries = []
        for i in range(count):
            raw = tiff[10 + 12 * i:22 + 12 * i]
            entries.append((int.from_bytes(raw[0:2], "big"),
                            int.from_bytes(raw[2:4], "big"),
                            int.from_bytes(raw[4:8], "big"),
                            int.from_bytes(raw[8:12], "big")))
        self.assertEqual([e[0] for e in entries], [0x010F, 0x927C])
        make_off = entries[0][3]
        self.assertEqual(tiff[make_off:make_off + 6], b"Apple\x00")
        tag, _type, length, mn_off = entries[1]
        self.assertEqual(tag, 0x927C)
        makernote = tiff[mn_off:mn_off + length]
        self.assertTrue(makernote.startswith(mp.APPLE_MAKER_HEADER))
        inner = makernote[len(mp.APPLE_MAKER_HEADER):]
        self.assertTrue(inner.startswith(b"MM\x00\x2a"))
        self.assertEqual(int.from_bytes(inner[8:10], "big"), 1)
        self.assertEqual(int.from_bytes(inner[10:12], "big"), 0x0011)  # MakerApple 17
        self.assertEqual(int.from_bytes(inner[12:14], "big"), 2)       # ASCII
        vlen = int.from_bytes(inner[14:18], "big")
        voff = int.from_bytes(inner[18:22], "big")
        self.assertEqual(inner[voff:voff + vlen], self.ASSET.encode() + b"\0")

    def test_photo_carries_uuid_and_orientation(self):
        photo = mp.create_apple_photo(jpeg_with_exif(), self.ASSET)
        self.assertTrue(photo.startswith(b"\xff\xd8"))
        self.assertTrue(photo.endswith(b"\xff\xd9"))  # no video trailer
        tiff = self.parse_exif(photo)
        self.assertIsNotNone(tiff)
        count = int.from_bytes(tiff[8:10], "big")
        self.assertEqual(count, 3)  # Make, Orientation (carried over), MakerNote
        tags = [int.from_bytes(tiff[10 + 12 * i:12 + 12 * i], "big") for i in range(count)]
        self.assertEqual(tags, [0x010F, 0x0112, 0x927C])
        self.assertIn(self.ASSET.encode(), photo)
        # no stale Google motion-photo XMP
        self.assertIsNone(mp.extract_xmp(photo))

    def test_photo_from_google_output_strips_xmp_and_trailer(self):
        google = mp.create_motion_photo(MINIMAL_JPEG, fake_mp4(300))
        photo = mp.create_apple_photo(google, self.ASSET)
        self.assertIsNone(mp.extract_xmp(photo))
        self.assertTrue(photo.endswith(b"\xff\xd9"))
        self.assertNotIn(fake_mp4(300), photo)

    def test_mov_atoms_and_brand(self):
        video = make_moov_mp4(10.0, width=832, height=1280)
        mov = mp.build_apple_mov(video, self.ASSET)
        self.assertEqual(mov[8:12], b"qt  ")  # ftyp major brand switched
        moov = self.find_child(mov, 0, len(mov), b"moov")
        self.assertIsNotNone(moov)
        moov_s, moov_e = moov
        self.assertEqual(int.from_bytes(mov[moov_s - 8:moov_s - 4], "big"),
                         moov_e - (moov_s - 8))  # moov size field consistent
        udta = self.find_child(mov, moov_s, moov_e, b"udta")
        self.assertIsNotNone(udta)
        udta_s, udta_e = udta
        meta = self.find_child(mov, udta_s, udta_e, b"meta")
        self.assertIsNotNone(meta)
        meta_s, meta_e = meta
        self.assertEqual(int.from_bytes(mov[meta_s:meta_s + 4], "big"), 0)  # fullbox
        hdlr = self.find_child(mov, meta_s + 4, meta_e, b"hdlr")
        self.assertEqual(mov[hdlr[0] + 8:hdlr[0] + 12], b"mdta")
        keys = self.find_child(mov, meta_s + 4, meta_e, b"keys")
        self.assertIn(mp.APPLE_CONTENT_KEY, mov[keys[0]:keys[1]])
        ilst = self.find_child(mov, meta_s + 4, meta_e, b"ilst")
        self.assertIsNotNone(ilst)
        data_box = self.find_child(mov, ilst[0] + 4, ilst[1], b"data")
        self.assertIsNotNone(data_box)
        value = mov[data_box[0]:data_box[1]]
        self.assertEqual(value[8:], self.ASSET.encode())  # after type+locale
        self.assertEqual(len(mov), len(video) + (udta_e - (udta_s - 8)))

    @staticmethod
    def build_mp4_with_stco(moov_first=True):
        def assemble(chunk_offset):
            ftyp = (24).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 4 + b"isomiso2"
            stco = box(b"stco", (0).to_bytes(4, "big") + (1).to_bytes(4, "big")
                       + chunk_offset.to_bytes(4, "big"))
            trak = box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", stco))))
            moov = box(b"moov", box(b"mvhd", b"\x00" * 20) + trak)
            mdat = box(b"mdat", b"\x00" * 64)
            return ftyp + (moov + mdat if moov_first else mdat + moov)

        # sizes are offset-independent, so probe once to locate the mdat payload
        probe = assemble(0)
        mdat_payload = next(s for t, s, _e in mp._iter_boxes(probe, 0, len(probe))
                            if t == b"mdat")
        chunk = mdat_payload + 16  # a chunk address inside the mdat payload
        return assemble(chunk), chunk

    def read_stco(self, data):
        for t, s, e in mp._iter_boxes(data, 0, len(data)):
            if t != b"moov":
                continue
            for t2, s2, e2 in mp._iter_boxes(data, s, e):
                if t2 == b"trak":
                    for t3, s3, e3 in mp._iter_boxes(data, s2, e2):
                        if t3 == b"mdia":
                            for t4, s4, e4 in mp._iter_boxes(data, s3, e3):
                                if t4 == b"minf":
                                    for t5, s5, e5 in mp._iter_boxes(data, s4, e4):
                                        if t5 == b"stbl":
                                            for t6, s6, e6 in mp._iter_boxes(data, s5, e5):
                                                if t6 == b"stco":
                                                    return int.from_bytes(
                                                        data[s6 + 8:s6 + 12], "big")
        return None

    def test_stco_shifted_when_mdat_after_moov(self):
        video, chunk = self.build_mp4_with_stco(moov_first=True)
        mov = mp.build_apple_mov(video, self.ASSET)
        delta = len(mov) - len(video)
        self.assertGreater(delta, 0)
        self.assertEqual(self.read_stco(mov), chunk + delta)

    def test_stco_untouched_when_mdat_before_moov(self):
        video, chunk = self.build_mp4_with_stco(moov_first=False)
        mov = mp.build_apple_mov(video, self.ASSET)
        self.assertEqual(self.read_stco(mov), chunk)

    def test_cli_apple_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            video = Path(tmp) / "clip.mp4"
            image.write_bytes(jpeg_with_exif())
            video.write_bytes(make_moov_mp4(4.0, width=640, height=640))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(video), "--format", "apple"])
            self.assertEqual(code, 0)
            self.assertIn("ContentIdentifier", stdout.getvalue())
            photo = Path(tmp) / "photo_live.jpg"
            movie = Path(tmp) / "photo_live.mov"
            self.assertTrue(photo.exists() and movie.exists())
            photo_bytes = photo.read_bytes()
            self.assertIn(b"Apple iOS", photo_bytes)
            self.assertTrue(photo_bytes.endswith(b"\xff\xd9"))
            self.assertEqual(movie.read_bytes()[8:12], b"qt  ")
            # the UUID must match across the pair
            uuid_match = re.search(rb"Apple iOS\x00\x01.*?"
                                   rb"([0-9A-F-]{36})\x00", photo_bytes, re.DOTALL)
            self.assertIsNotNone(uuid_match)
            asset = uuid_match.group(1)
            self.assertIn(asset, movie.read_bytes())


class HonorFormatTests(unittest.TestCase):
    """Layout verified against a real 2026 Honor capture: Android Motion Photo
    v1 XMP (Primary Length=0, exact MotionPhoto length), MP4 as the file tail,
    no vendor trailer."""

    def test_honor_photo_layout(self):
        video = fake_mp4(500)
        out = mp.create_honor_photo(MINIMAL_JPEG, video, duration=3.0)
        eoi = mp.find_jpeg_eoi(out)
        self.assertEqual(out[eoi:], video)  # MP4 is the exact tail, no marker
        xmp = mp.extract_xmp(out)
        self.assertIn('GCamera:MotionPhoto="1"', xmp)
        self.assertIn('GCamera:MotionPhotoVersion="1"', xmp)
        self.assertIn('GCamera:MotionPhotoPresentationTimestampUs="1500000"', xmp)
        self.assertIn('GCamera:MotionPhotoVideoTrimEndUs="3000000"', xmp)
        self.assertIn('Item:Length="0"', xmp)          # Primary extends to next item
        self.assertIn(f'Item:Length="{len(video)}"', xmp)
        self.assertNotIn("MicroVideo", xmp)            # legacy fields would confuse it
        report = mp.inspect_motion_photo(out)
        self.assertTrue(report["checks"])
        for label, ok in report["checks"]:
            self.assertTrue(ok, label)
        self.assertIsNone(report["micro_video"])

    def test_honor_photo_without_duration(self):
        out = mp.create_honor_photo(MINIMAL_JPEG, fake_mp4(300))
        xmp = mp.extract_xmp(out)
        self.assertIn('GCamera:MotionPhotoPresentationTimestampUs="0"', xmp)

    def test_honor_idempotent(self):
        video = fake_mp4(400)
        once = mp.create_honor_photo(MINIMAL_JPEG, video)
        twice = mp.create_honor_photo(once, video)
        self.assertEqual(len(mp.extract_xmp(twice)), len(mp.extract_xmp(once)))
        self.assertEqual(twice[mp.find_jpeg_eoi(twice):], video)

    def test_real_honor_sample_checks(self):
        sample = Path(r"C:\Users\wcbos\Downloads\IMG_20261004_144401.jpg")
        if not sample.exists():
            self.skipTest("real Honor sample not available")
        report = mp.inspect_motion_photo(sample.read_bytes())
        self.assertTrue(report["checks"])
        for label, ok in report["checks"]:
            self.assertTrue(ok, label)

    def test_cli_honor(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            video = Path(tmp) / "clip.mp4"
            image.write_bytes(jpeg_with_exif())
            video.write_bytes(make_moov_mp4(3.0, width=640, height=640))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(video), "--format", "honor"])
            self.assertEqual(code, 0)
            self.assertIn("Motion photo written", stdout.getvalue())
            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            self.assertNotIn(b"LIVE_", output[-64:])
            self.assertIn(b'GCamera:MotionPhoto="1"', output)


def _find_real_ffmpeg():
    candidate = os.environ.get("MOTION_PHOTO_FFMPEG") or shutil.which("ffmpeg")
    if candidate:
        return candidate
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


REAL_FFMPEG = _find_real_ffmpeg()


def build_exif_app1(ifd0_entries, exif_entries=None, endian="little"):
    """EXIF APP1 segment built from (tag, exif_type, value_bytes) entry lists.

    Values longer than 4 bytes go to the TIFF data area (out-of-line, so value
    offset arithmetic is exercised), and an Exif sub-IFD is linked via tag
    0x8769 when ``exif_entries`` is given.
    """
    order = "little" if endian == "little" else "big"
    e16 = lambda v: v.to_bytes(2, order)
    e32 = lambda v: v.to_bytes(4, order)
    size_of = {1: 1, 2: 1, 3: 2, 4: 4, 7: 1}
    if exif_entries:
        ifd0_entries = list(ifd0_entries) + [(0x8769, 4, b"SUBIFD")]
    ifd0_size = 2 + 12 * len(ifd0_entries) + 4
    exif_size = 2 + 12 * len(exif_entries) + 4 if exif_entries else 0
    exif_ifd_at = 8 + ifd0_size
    data_at = exif_ifd_at + exif_size
    blobs = bytearray()

    def field(value):
        if len(value) <= 4:
            return (value + b"\x00" * 4)[:4]
        offset = data_at + len(blobs)
        blobs.extend(value + (b"\x00" if len(value) % 2 else b""))
        return e32(offset)

    def block(entries):
        out = e16(len(entries))
        for tag, exif_type, value in entries:
            if tag == 0x8769:
                out += e16(tag) + e16(4) + e32(1) + e32(exif_ifd_at)
                continue
            out += (e16(tag) + e16(exif_type)
                    + e32(len(value) // size_of[exif_type]) + field(value))
        return out + e32(0)

    tiff = ((b"II" if order == "little" else b"MM") + e16(42) + e32(8)
            + block(ifd0_entries) + (block(exif_entries) if exif_entries else b"")
            + bytes(blobs))
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def com_segment(text: bytes) -> bytes:
    return b"\xff\xfe" + (len(text) + 2).to_bytes(2, "big") + text


def app11_segment(payload: bytes) -> bytes:
    return b"\xff\xeb" + (len(payload) + 2).to_bytes(2, "big") + payload


def jpeg_with_extra(*segments: bytes) -> bytes:
    """The minimal JPEG with extra segments inserted after its leading JFIF APP0."""
    events = mp.scan_pre_sos_segments(MINIMAL_JPEG)
    assert events[0][1] == 0xE0
    app0_end = events[0][3]
    return MINIMAL_JPEG[:app0_end] + b"".join(segments) + MINIMAL_JPEG[app0_end:]


def marker_payloads(data: bytes, marker: int) -> list:
    return [data[mpos + 4:seg_end]
            for kind, m, mpos, seg_end in mp.scan_pre_sos_segments(data)
            if kind == "segment" and m == marker]


class AiMetadataTests(unittest.TestCase):
    @staticmethod
    def exif_entry(data, tag):
        for entry in mp.iter_exif_entries(data):
            if entry["tag"] == tag:
                return entry
        raise AssertionError(f"EXIF tag {tag:#06x} not found")

    def test_clean_image_untouched(self):
        jpeg = jpeg_with_exif()
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        self.assertEqual(notes, [])
        self.assertEqual(cleaned, jpeg)

    def test_exif_software_blanked_in_place(self):
        original = b"Stable Diffusion XL\x00"
        exif = build_exif_app1([
            (0x010F, 2, b"TestCam\x00"),      # Make
            (0x0112, 3, b"\x06\x00"),         # Orientation = 6 (inline)
            (0x0131, 2, original),            # Software (out-of-line)
        ])
        jpeg = jpeg_with_extra(exif)
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        self.assertEqual(len(notes), 1)
        self.assertIn("Software", notes[0])
        self.assertIn("stable diffusion", notes[0])
        self.assertEqual(self.exif_entry(cleaned, 0x0131)["value"], b"\x00" * len(original))
        self.assertEqual(self.exif_entry(cleaned, 0x010F)["value"], b"TestCam\x00")
        self.assertEqual(self.exif_entry(cleaned, 0x0112)["value"], b"\x06\x00")
        self.assertEqual(len(cleaned), len(jpeg))  # blanked in place: layout unchanged
        self.assertEqual(mp.image_dimensions(cleaned), mp.image_dimensions(jpeg))
        self.assertEqual(mp.find_ai_markers(cleaned), [])

    def test_exif_big_endian_and_user_comment_unicode(self):
        comment = b"UNICODE\x00" + "Midjourney prompt".encode("utf-16-le")
        exif = build_exif_app1(
            [(0x0112, 3, b"\x00\x06"), (0x0131, 2, b"\xe5\x8d\xb3\xe6\xa2\xa6\x00")],
            exif_entries=[(0x9286, 7, comment)],
            endian="big",
        )
        jpeg = jpeg_with_extra(exif)
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        self.assertEqual(self.exif_entry(cleaned, 0x9286)["value"], b"\x00" * len(comment))
        self.assertEqual(self.exif_entry(cleaned, 0x0131)["value"], b"\x00" * 7)
        self.assertEqual(self.exif_entry(cleaned, 0x0112)["value"], b"\x00\x06")
        joined = "\n".join(notes)
        self.assertIn("UserComment", joined)
        self.assertIn("Software", joined)

    def test_xmp_element_and_attribute_removal(self):
        xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            'xmlns:Iptc4xmpExt="http://cv.iptc.org/newscodes/digitalsourcetype/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about="" dc:format="image/jpeg"\n'
            '    Iptc4xmpExt:DigitalSourceType='
            '"http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia">\n'
            '   <dc:format>image/jpeg</dc:format>\n'
            '   <xmp:CreatorTool>ComfyUI</xmp:CreatorTool>\n'
            '   <dc:title>A real sunset</dc:title>\n'
            '  </rdf:Description>\n'
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
        )
        jpeg = jpeg_with_extra(mp.wrap_xmp_app1(xmp.encode()))
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        joined = "\n".join(notes)
        self.assertIn("xmp", joined)
        self.assertIn("<xmp:CreatorTool>", joined)
        self.assertIn("DigitalSourceType", joined)
        text = mp.extract_xmp(cleaned)
        self.assertIsNotNone(text)
        self.assertIn("A real sunset", text)      # unrelated properties survive
        self.assertIn("image/jpeg", text)
        self.assertNotIn("AlgorithmicMedia", text)
        self.assertNotIn("ComfyUI", text)
        self.assertEqual(mp.find_ai_markers(cleaned), [])

    def test_xmp_real_camera_source_type_kept(self):
        xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" '
            'xmlns:Iptc4xmpExt="http://cv.iptc.org/newscodes/digitalsourcetype/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""\n'
            '    Iptc4xmpExt:DigitalSourceType='
            '"http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture">\n'
            '   <dc:title>Beach</dc:title>\n'
            '  </rdf:Description>\n'
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
        )
        jpeg = jpeg_with_extra(mp.wrap_xmp_app1(xmp.encode()))
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        self.assertEqual(notes, [])
        self.assertIn("digitalCapture", mp.extract_xmp(cleaned))

    def test_xmp_fallback_whole_packet_dropped(self):
        xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about="">\n'
            '   <dc:description><![CDATA[made with Stable Diffusion]]></dc:description>\n'
            '  </rdf:Description>\n'
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
        )
        jpeg = jpeg_with_extra(mp.wrap_xmp_app1(xmp.encode()))
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        self.assertTrue(any("whole packet" in n for n in notes))
        self.assertIsNone(mp.extract_xmp(cleaned))
        self.assertEqual(mp.find_ai_markers(cleaned), [])

    def test_c2pa_comment_and_app13_dropped(self):
        c2pa = app11_segment((28).to_bytes(4, "big") + b"JUMBF" + b"c2pa\x00manifest-bytes")
        benign_app11 = app11_segment(b"jpegxt\x00data")
        comment = com_segment(b"masterpiece, best quality, Stable Diffusion")
        benign_comment = com_segment(b"holiday 2026")
        app13_payload = b"Photoshop 3.0\x00byline: Firefly"
        app13 = b"\xff\xed" + (len(app13_payload) + 2).to_bytes(2, "big") + app13_payload
        jpeg = jpeg_with_extra(c2pa, comment, benign_comment, benign_app11, app13)
        cleaned, notes = mp.strip_ai_metadata(jpeg)
        self.assertEqual({n.split(":")[0] for n in notes}, {"C2PA", "comment", "photoshop"})
        self.assertEqual(marker_payloads(cleaned, 0xEB), [b"jpegxt\x00data"])
        self.assertEqual(marker_payloads(cleaned, 0xFE), [b"holiday 2026"])
        self.assertEqual(marker_payloads(cleaned, 0xED), [])
        self.assertEqual(mp.find_ai_markers(cleaned), [])

    def test_strip_is_idempotent(self):
        jpeg = jpeg_with_extra(
            build_exif_app1([(0x0131, 2, b"ComfyUI\x00")]),
            com_segment("prompt:即梦 AI".encode("utf-8")),
            app11_segment((24).to_bytes(4, "big") + b"JUMBF" + b"claim"),
        )
        once, notes = mp.strip_ai_metadata(jpeg)
        self.assertEqual(len(notes), 3)
        twice, notes2 = mp.strip_ai_metadata(once)
        self.assertEqual(notes2, [])
        self.assertEqual(once, twice)

    def test_find_ai_markers_reports_sources(self):
        xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/" '
               'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
               '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               '<rdf:Description rdf:about="">'
               '<xmp:CreatorTool>MIDJOURNEY</xmp:CreatorTool>'
               '</rdf:Description></rdf:RDF></x:xmpmeta>')
        jpeg = jpeg_with_extra(
            app11_segment((16).to_bytes(4, "big") + b"JUMBF" + b"c2pa"),
            mp.wrap_xmp_app1(xmp.encode()),
            build_exif_app1([(0x0131, 2, b"Midjourney\x00")]),
        )
        findings = mp.find_ai_markers(jpeg)
        self.assertEqual({f["source"] for f in findings}, {"C2PA", "xmp", "exif"})
        for finding in findings:
            self.assertTrue(finding["detail"])

    def test_standalone_clean_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            out = Path(tmp) / "clean.jpg"
            image.write_bytes(jpeg_with_extra(
                build_exif_app1([(0x0131, 2, b"Doubao AI\x00")])))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual(mp.find_ai_markers(out.read_bytes()), [])
            self.assertIn("removed", stdout.getvalue())

    def test_standalone_clean_cli_without_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image)])
            self.assertEqual(code, 0)
            self.assertIn("no AI-generated metadata markers found", stdout.getvalue())
            self.assertFalse(list(Path(tmp).glob("*_clean*")))

    def test_motion_photo_creation_strips_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            video = Path(tmp) / "clip.mp4"
            out = Path(tmp) / "out.jpg"
            image.write_bytes(jpeg_with_extra(
                com_segment(b"Stable Diffusion prompt"),
                build_exif_app1([(0x0131, 2, b"ComfyUI\x00")])))
            video.write_bytes(fake_mp4(300))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(video), "-o", str(out)])
            self.assertEqual(code, 0)
            result = out.read_bytes()
            self.assertEqual(mp.find_ai_markers(result), [])
            self.assertEqual(result[mp.find_jpeg_eoi(result):], fake_mp4(300))
            report = mp.inspect_motion_photo(result)
            self.assertTrue(all(ok for _label, ok in report["checks"]))
            self.assertIn("removed AI metadata", stdout.getvalue())

    def test_info_reports_ai_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "ai.jpg"
            plain.write_bytes(jpeg_with_extra(
                build_exif_app1([(0x0131, 2, b"Kling AI\x00")])))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main(["--info", str(plain)])
            self.assertEqual(code, 1)  # not a motion photo, but the report prints
            self.assertIn("AI metadata", stdout.getvalue())
            self.assertIn("[exif]", stdout.getvalue())
            self.assertIn("Software", stdout.getvalue())


@unittest.skipUnless(REAL_FFMPEG, "no real ffmpeg available")
class RealFfmpegTests(unittest.TestCase):
    """End-to-end run against a real ffmpeg binary (found via PATH/imageio_ffmpeg)."""

    def test_compress_and_speed(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            generate = [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                # square source matches the 1x1 still, so aspect matching stays
                # out of the way and this test exercises compress+speed only
                "-f", "lavfi", "-i", "testsrc2=duration=8:size=1280x1280:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                "-shortest", str(src),
            ]
            made = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertTrue(mp.looks_like_mp4(src.read_bytes()))
            self.assertAlmostEqual(mp.probe_mp4(src.read_bytes())["duration"], 8.0,
                                   delta=0.3)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(src), "--compress", "--duration", "3",
                                "--volume", "1", "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0, stdout.getvalue())
            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            trailer = output[mp.find_jpeg_eoi(output):]
            self.assertLess(len(trailer), src.stat().st_size)  # compression worked

            probe = mp.probe_mp4(trailer)
            self.assertAlmostEqual(probe["duration"], 3.0, delta=0.5)
            self.assertTrue(probe["has_audio"])

            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(trailer)
            inspected = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(extracted),
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertIn("h264", inspected.stderr)
            self.assertIn("aac", inspected.stderr)
            self.assertIn("1280x1280", inspected.stderr)  # below the 1440px cap

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mp.main(["--info", str(Path(tmp) / "photo_motion.jpg")]), 0)

    @staticmethod
    def _generate_av_clip(ffmpeg, path, duration=4.0):
        made = subprocess.run(
            [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"testsrc2=duration={duration}:size=640x640:rate=24",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
             "-shortest", str(path)],
            capture_output=True, text=True,
        )
        assert made.returncode == 0, made.stderr

    def test_volume_default_mutes(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            self._generate_av_clip(ffmpeg, src)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())  # square still matches 640x640
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(src), "--compress", "--duration", "3",
                                "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertIn("audio muted", stdout.getvalue())

            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            trailer = output[mp.find_jpeg_eoi(output):]
            self.assertFalse(mp.probe_mp4(trailer)["has_audio"])
            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(trailer)
            inspected = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(extracted),
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertNotIn("Audio:", inspected.stderr)  # no audio stream at all

    def test_volume_alone_scales_audio_without_reencoding(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            self._generate_av_clip(ffmpeg, src)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(src), "--volume", "0.5",
                                "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0)
            self.assertIn("audio volume set to 0.5", stdout.getvalue())

            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            trailer = output[mp.find_jpeg_eoi(output):]
            probe = mp.probe_mp4(trailer)
            self.assertTrue(probe["has_audio"])
            self.assertAlmostEqual(probe["duration"], 4.0, delta=0.3)
            self.assertEqual((probe["width"], probe["height"]), (640, 640))

            def mean_volume(path):
                checked = subprocess.run(
                    [str(ffmpeg), "-hide_banner", "-i", str(path),
                     "-af", "volumedetect", "-f", "null", "-"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", checked.stderr)
                self.assertIsNotNone(match, checked.stderr)
                return float(match.group(1))

            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(trailer)
            # 0.5 is -6 dB; allow encoder slack around the expected drop
            self.assertLess(mean_volume(extracted), mean_volume(src) - 4.0)

    def test_volume_one_alone_embeds_source_unchanged(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            self._generate_av_clip(ffmpeg, src)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            with contextlib.redirect_stdout(io.StringIO()):
                code = mp.main([str(image), str(src), "--volume", "1"])
            self.assertEqual(code, 0)
            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            self.assertEqual(output[mp.find_jpeg_eoi(output):], src.read_bytes())

    def test_png_input_converted_by_real_ffmpeg(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "photo.png"
            made = subprocess.run(
                [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=duration=1:size=640x640:rate=24",
                 "-frames:v", "1", str(png)],
                capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            src = Path(tmp) / "src.mp4"  # square like the converted still
            made = subprocess.run(
                [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=duration=1:size=640x640:rate=24",
                 "-c:v", "libx264", "-preset", "veryfast", str(src)],
                capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(png), str(src), "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertIn("converted PNG -> JPEG (640x640", stdout.getvalue())

            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            eoi = mp.find_jpeg_eoi(output)
            self.assertEqual(mp.image_dimensions(output[:eoi]), (640, 640))
            self.assertEqual(output[eoi:], src.read_bytes())  # square: untouched
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mp.main(["--info", str(Path(tmp) / "photo_motion.jpg")]), 0)

    def test_transparent_png_flattened_onto_white(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "clear.png"  # a fully transparent frame
            made = subprocess.run(
                [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black@0.0:size=32x32,format=rgba",
                 "-frames:v", "1", str(png)],
                capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertTrue(mp.png_has_alpha(png.read_bytes()))

            src = Path(tmp) / "src.mp4"  # square like the converted still
            made = subprocess.run(
                [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=24",
                 "-c:v", "libx264", "-preset", "veryfast", str(src)],
                capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            with contextlib.redirect_stdout(io.StringIO()):
                code = mp.main([str(png), str(src), "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0)

            output = (Path(tmp) / "clear_motion.jpg").read_bytes()
            still = Path(tmp) / "still.jpg"
            still.write_bytes(output[:mp.find_jpeg_eoi(output)])
            raw = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-loglevel", "error",
                 "-i", str(still), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True)
            self.assertEqual(raw.returncode, 0)
            r, g, b = raw.stdout[:3]
            self.assertGreater(min(r, g, b), 200)  # transparent -> white, not black

    def test_match_aspect_crops_to_photo(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"  # 0.64 aspect vs the 1x1 (square) still
            generate = [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=2:size=800x1250:rate=24",
                "-c:v", "libx264", "-preset", "veryfast", str(src),
            ]
            made = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())  # 1x1 still -> crop to square
            # aspect matching is on by default now: plain run, no extra flags
            with contextlib.redirect_stdout(io.StringIO()):
                code = mp.main([str(image), str(src), "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0)

            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(output[mp.find_jpeg_eoi(output):])
            inspected = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(extracted),
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertIn("800x800", inspected.stderr)  # centre-cropped, no bars

    def test_stretch_matches_photo(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"  # 0.64 aspect vs the 1x1 (square) still
            generate = [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=2:size=800x1250:rate=24",
                "-c:v", "libx264", "-preset", "veryfast", str(src),
            ]
            made = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())  # 1x1 still -> stretch to square
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(src), "--stretch",
                                "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0)
            self.assertIn("stretched to the photo's aspect ratio", stdout.getvalue())

            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(output[mp.find_jpeg_eoi(output):])
            inspected = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(extracted),
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            # same target window as cropping: the full frame squeezed to 800x800
            self.assertIn("800x800", inspected.stderr)

    def test_crop_bars_removes_padding(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "padded.mp4"  # 640x640 content in a 672x672 frame
            generate = [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=1:size=640x640:rate=24",
                "-vf", "pad=672:672:0:0:black",
                "-c:v", "libx264", "-preset", "veryfast", str(src),
            ]
            made = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())  # square still: no aspect adjustment
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(src), "--crop-bars",
                                "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0)
            self.assertIn("trimmed black bars (32 px bottom, 32 px right)",
                          stdout.getvalue())

            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(output[mp.find_jpeg_eoi(output):])
            inspected = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(extracted),
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertIn("640x640", inspected.stderr)
            self.assertNotIn("672x672", inspected.stderr)

    def test_manual_crop_pixels(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "square.mp4"
            generate = [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=1:size=640x640:rate=24",
                "-c:v", "libx264", "-preset", "veryfast", str(src),
            ]
            made = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            with contextlib.redirect_stdout(io.StringIO()):
                code = mp.main([str(image), str(src), "--crop", "10:0:0:0",
                                "--ffmpeg", str(ffmpeg)])
            self.assertEqual(code, 0)
            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            extracted = Path(tmp) / "extracted.mp4"
            extracted.write_bytes(output[mp.find_jpeg_eoi(output):])
            inspected = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(extracted),
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertIn("640x630", inspected.stderr)

    def test_apple_pair_plays_in_real_ffmpeg(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            # one faststart clip (moov first: exercises the stco fixup) and one
            # regular clip (moov last) must both survive the atom surgery
            for index, flags in enumerate((["-movflags", "+faststart"], [])):
                src = Path(tmp) / f"src{index}.mp4"
                generate = [
                    str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=duration=1:size=640x640:rate=24",
                    "-c:v", "libx264", "-preset", "veryfast",
                    *flags, str(src),
                ]
                made = subprocess.run(generate, capture_output=True, text=True)
                self.assertEqual(made.returncode, 0, made.stderr)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = mp.main([str(image), str(src), "--format", "apple",
                                    "-o", str(Path(tmp) / f"pair{index}.jpg")])
                self.assertEqual(code, 0)
                movie = (Path(tmp) / f"pair{index}.mov").read_bytes()
                # the box surgery must not corrupt the stream
                played = subprocess.run(
                    [str(ffmpeg), "-v", "error", "-i", str(Path(tmp) / f"pair{index}.mov"),
                     "-f", "null", "-"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                self.assertEqual(played.returncode, 0, played.stderr)
                probe = mp.probe_mp4(movie)
                self.assertAlmostEqual(probe["duration"], 1.0, delta=0.3)
                self.assertEqual(movie[8:12], b"qt  ")

    def test_matching_aspect_embeds_without_transcode(self):
        ffmpeg = Path(REAL_FFMPEG)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "square.mp4"  # 1:1, same as the 1x1 still
            generate = [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=1:size=640x640:rate=24",
                "-c:v", "libx264", "-preset", "veryfast", str(src),
            ]
            made = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)

            image = Path(tmp) / "photo.jpg"
            image.write_bytes(jpeg_with_exif())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = mp.main([str(image), str(src)])
            self.assertEqual(code, 0)
            self.assertIn("aspect already matches", stdout.getvalue())
            output = (Path(tmp) / "photo_motion.jpg").read_bytes()
            self.assertEqual(output[mp.find_jpeg_eoi(output):], src.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
