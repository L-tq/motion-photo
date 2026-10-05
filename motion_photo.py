#!/usr/bin/env python3
"""Combine a JPEG still image and an MP4 clip into an Android Motion Photo.

By default the tool writes the Honor 动态照片 flavour (Android Motion Photo
v1, verified playing on a real Honor phone); --format google writes the
Google MVIMG flavour that Google Photos recognises.

A Motion Photo is an ordinary JPEG file with two additions:

1. An XMP APP1 segment describing the embedded media.  The default honor
   flavour writes the Android Motion Photo v1 scheme (GCamera:MotionPhoto="1"
   plus a Container directory whose Primary item has Length 0 and whose
   MotionPhoto item carries the exact MP4 size).  The google flavour writes
   both Google schemes for maximum compatibility with older readers:
   - legacy "MicroVideo":  GCamera:MicroVideoVersion / GCamera:MicroVideoOffset,
     where the offset counts bytes backwards from the end of the file to the
     start of the video;
   - "Container directory": Container:Directory lists the primary JPEG and the
     appended MP4 with their exact byte lengths.
2. The MP4 bytes appended directly after the JPEG EOI (FF D9) marker, as the
   exact tail of the file.

Usage:
    python motion_photo.py photo.jpg clip.mp4
    python motion_photo.py photo.jpg clip.mp4 --compress --duration
    python motion_photo.py photo.jpg clip.mp4 --compress --volume 1
    python motion_photo.py photo.jpg clip.mp4 --crop-bars --stretch
    python motion_photo.py photo.jpg clip.mp4 --crop 24:8:0:0
    python motion_photo.py photo.jpg clip.mp4 --format google
    python motion_photo.py photo.jpg clip.mp4 --format apple
    python motion_photo.py photo.jpg                          # strip AI tags only
    python motion_photo.py --info MVIMG_20261004_120000.jpg

Formats (--format): honor (default) writes Honor's Android Motion Photo v1
layout (reverse-engineered from a real 2026 Honor capture: MotionPhoto="1"
XMP, Primary Item:Length=0, MP4 as the exact file tail); google writes the
single-JPG Android motion photo for Google Photos; apple writes an iPhone
Live Photo pair (photo.jpg + photo.mov linked by a matching
ContentIdentifier UUID, imported as a pair via AirDrop/Mac Photos/iCloud).

Optional video processing (needs ffmpeg): --compress re-encodes the clip to
shrink it, and --duration fast-forwards it to a target length (default 3 s).
The clip's audio follows --volume: the default 0 mutes it (the audio track is
removed outright -- smaller files, and galleries play motion photos silently
anyway), 1 keeps the original sound and other values scale it.  By default
the clip is also conformed to the photo's aspect ratio when the
two differ (players letterbox such clips with black side bars): centre-cropped
by default, or stretched with --stretch.  Matching clips are embedded without
any re-encoding.  --volume only touches the audio when ffmpeg runs: with any
re-encoding, or on its own (any level but 1) as a fast audio-only pass that
copies the video stream; a plain embed keeps the video byte-identical.
Disable matching with --no-match-aspect.  Baked-in black
bars can be trimmed automatically (--crop-bars) or by explicit pixel counts
(--crop TOP:BOTTOM:LEFT:RIGHT).

AI-generated metadata is checked and removed automatically: EXIF string
values naming a generator (Software, UserComment, MakerNote, ...), XMP
properties such as Iptc4xmpExt:DigitalSourceType=trainedAlgorithmicMedia or
xmp:CreatorTool, C2PA/JUMBF content-credential manifests (APP11), Photoshop
IPTC blocks (APP13) and JPEG comments.  EXIF values are zeroed in place so
every TIFF offset (orientation included) stays valid.  Calling the script
with an image and no video checks and cleans that single photo instead of
building a motion photo.

Only the Python standard library is used; ffmpeg is required solely when
video processing actually happens.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"
EXIF_NS = b"Exif\x00\x00"

FTYP = b"ftyp"
MAX_APP1_SIZE = 0xFFFF


class MotionPhotoError(Exception):
    """Raised for invalid input or structural problems."""


# --------------------------------------------------------------------------
# JPEG parsing
# --------------------------------------------------------------------------

def walk_jpeg(data: bytes):
    """Walk the top-level JPEG structure of ``data``.

    Yields 4-tuples ``(kind, marker, marker_pos, end_pos)`` where ``kind`` is
    "segment" for length-bearing markers and "eoi" for the final FFD9.  For a
    segment, ``end_pos`` is just past its payload (marker byte pair + 2-byte
    length + payload).  Entropy-coded scans after SOS are consumed internally
    (honouring byte stuffing and restart markers), so extra scans and segments
    of progressive JPEGs are walked correctly as well.

    Raises MotionPhotoError on truncation or desynchronisation.
    """
    if not data.startswith(SOI):
        raise MotionPhotoError("not a JPEG: missing SOI (FF D8) marker")
    n = len(data)
    pos = 2
    while True:
        if pos >= n:
            raise MotionPhotoError("truncated JPEG: reached end of file before EOI")
        if data[pos] != 0xFF:
            raise MotionPhotoError(f"corrupt JPEG: expected a marker at byte {pos}")
        marker_pos = pos
        while pos < n and data[pos] == 0xFF:  # skip fill bytes before the marker
            pos += 1
        if pos >= n:
            raise MotionPhotoError("truncated JPEG: file ends inside a marker")
        marker = data[pos]
        pos += 1
        if marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD8:
            raise MotionPhotoError(
                f"corrupt JPEG: unexpected marker FF{marker:02X} at byte {marker_pos}"
            )
        if marker == 0xD9:
            yield ("eoi", marker, marker_pos, pos)
            return
        if pos + 2 > n:
            raise MotionPhotoError("truncated JPEG: segment length runs past end of file")
        seg_len = int.from_bytes(data[pos:pos + 2], "big")
        seg_end = pos + seg_len
        if seg_len < 2 or seg_end > n:
            raise MotionPhotoError(
                f"corrupt JPEG: bad length {seg_len} for marker FF{marker:02X} "
                f"at byte {marker_pos}"
            )
        yield ("segment", marker, marker_pos, seg_end)
        pos = seg_end
        if marker == 0xDA:  # SOS: entropy-coded data follows the segment header
            while pos < n:
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                if pos + 1 >= n:
                    raise MotionPhotoError("truncated JPEG: file ends inside entropy-coded data")
                nxt = data[pos + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:  # stuffed byte / restart marker
                    pos += 2
                    continue
                break  # a real marker: the outer loop takes over from here
            else:
                raise MotionPhotoError("truncated JPEG: no EOI after entropy-coded data")


def find_jpeg_eoi(data: bytes) -> int:
    """Return the offset just past the top-level EOI marker of ``data``."""
    for kind, _marker, _mpos, end in walk_jpeg(data):
        if kind == "eoi":
            return end
    raise MotionPhotoError("no EOI marker found")  # defensive; walk_jpeg raises first


def scan_pre_sos_segments(data: bytes):
    """List the top-level (kind, marker, marker_pos, end_pos) events up to SOS/EOI."""
    events = []
    for event in walk_jpeg(data):
        events.append(event)
        if event[0] == "eoi" or event[1] == 0xDA:
            break
    return events


SOF_MARKERS = frozenset(
    m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC)  # DHT, JPG, DAC
)


def image_dimensions(data: bytes):
    """(width, height) read from the first SOF marker of a JPEG."""
    for kind, marker, marker_pos, _end in walk_jpeg(data):
        if kind == "segment" and marker in SOF_MARKERS:
            height = int.from_bytes(data[marker_pos + 5:marker_pos + 7], "big")
            width = int.from_bytes(data[marker_pos + 7:marker_pos + 9], "big")
            if width and height:
                return width, height
    raise MotionPhotoError("could not read the JPEG dimensions (no SOF marker found)")


# --------------------------------------------------------------------------
# XMP
# --------------------------------------------------------------------------

_XMP_TEMPLATE = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""\n'
    '    xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"\n'
    '    xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
    '    xmlns:Item="http://ns.google.com/photos/1.0/container/item/">\n'
    '   <GCamera:MicroVideoVersion>1</GCamera:MicroVideoVersion>\n'
    '   <GCamera:MicroVideoOffset>{video_len}</GCamera:MicroVideoOffset>\n'
    '   <Container:Directory>\n'
    '    <rdf:Seq>\n'
    '     <rdf:li rdf:parseType="Resource">\n'
    '      <Container:Item Item:Length="{image_len}" '
    'Item:Mime="image/jpeg" Item:Semantic="Primary"/>\n'
    '     </rdf:li>\n'
    '     <rdf:li rdf:parseType="Resource">\n'
    '      <Container:Item Item:Length="{video_len}" '
    'Item:Mime="video/mp4" Item:Semantic="MotionPhoto"/>\n'
    '     </rdf:li>\n'
    '    </rdf:Seq>\n'
    '   </Container:Directory>\n'
    '  </rdf:Description>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>\n'
)


def render_xmp(image_len: int, video_len: int) -> bytes:
    return _XMP_TEMPLATE.format(image_len=image_len, video_len=video_len).encode("utf-8")


def wrap_xmp_app1(xmp: bytes) -> bytes:
    """Wrap an XMP packet into a JPEG APP1 segment."""
    payload = XMP_NS + xmp
    if 2 + len(payload) > MAX_APP1_SIZE:
        raise MotionPhotoError("XMP packet too large for a single APP1 segment (65535 bytes)")
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def build_xmp_app1(base_len: int, video_len: int) -> bytes:
    """Build the motion-photo XMP APP1 segment.

    ``base_len`` is the length of the JPEG the segment will be spliced into
    (excluding the segment itself).  The Primary item length must equal the
    final JPEG size, which includes this segment, so the packet is re-rendered
    until the value is self-consistent.
    """
    image_len = 0
    for _ in range(8):
        segment = wrap_xmp_app1(render_xmp(image_len, video_len))
        new_len = base_len + len(segment)
        if new_len == image_len:
            return segment
        image_len = new_len
    raise MotionPhotoError("failed to stabilise the XMP primary image length")


# --------------------------------------------------------------------------
# Motion photo assembly
# --------------------------------------------------------------------------

def _assemble_motion_photo(jpeg: bytes, media: bytes, build_segment) -> bytes:
    """Splice a motion-photo XMP APP1 into a clean JPEG, then append media.

    ``build_segment(base_len)`` renders the XMP APP1 segment, where ``base_len``
    is the length of the JPEG it will be spliced into (excluding the segment).
    Any existing motion-photo XMP segment is replaced; the XMP is kept behind
    leading JFIF/EXIF segments.
    """
    events = scan_pre_sos_segments(jpeg)
    insert_at = 2  # default: directly after SOI
    replace = None  # span of an existing XMP APP1 segment, if any
    for kind, marker, marker_pos, seg_end in events:
        payload = jpeg[marker_pos + 4:seg_end]
        if kind == "segment" and marker == 0xE1 and payload.startswith(XMP_NS):
            replace = (marker_pos, seg_end)
            break
        if kind == "segment" and marker == 0xE0 and (
            payload.startswith(b"JFIF\x00") or payload.startswith(b"JFXX\x00")
        ):
            insert_at = seg_end  # keep JFIF ahead of the XMP
            continue
        if kind == "segment" and marker == 0xE1 and payload.startswith(EXIF_NS):
            insert_at = seg_end  # keep EXIF ahead of the XMP
            continue
        break

    if replace:
        head, tail = jpeg[:replace[0]], jpeg[replace[1]:]
        segment = build_segment(len(head) + len(tail))
        new_jpeg = head + segment + tail
    else:
        segment = build_segment(len(jpeg))
        new_jpeg = jpeg[:insert_at] + segment + jpeg[insert_at:]
    return new_jpeg + media


def create_motion_photo(image_bytes: bytes, video_bytes: bytes) -> bytes:
    """Merge a JPEG and an MP4 into a motion photo and return the file bytes.

    Any trailer already present after the JPEG EOI (e.g. an older embedded
    video) is discarded, and an existing motion-photo XMP APP1 segment is
    replaced, so the function is idempotent on its own output.
    """
    if not video_bytes:
        raise MotionPhotoError("video file is empty")
    eoi_end = find_jpeg_eoi(image_bytes)
    jpeg = image_bytes[:eoi_end]  # drop any existing trailer
    return _assemble_motion_photo(
        jpeg, video_bytes, lambda base: build_xmp_app1(base, len(video_bytes)))


def looks_like_mp4(video: bytes) -> bool:
    """True if ``video`` starts with an ISO base media file 'ftyp' box."""
    return len(video) >= 12 and video[4:8] == FTYP


# --------------------------------------------------------------------------
# Apple Live Photo format (a paired JPG + MOV)
# --------------------------------------------------------------------------
# A Live Photo is two files sharing one ContentIdentifier UUID: the JPG carries
# it in the Apple maker note (MakerApple tag 17) inside EXIF, the MOV carries
# it as QuickTime metadata (com.apple.quicktime.content.identifier).  This is
# the same pairing makelive/Synology Photos produce.

APPLE_MAKER_HEADER = b"Apple iOS\x00\x01"
APPLE_CONTENT_KEY = b"com.apple.quicktime.content.identifier"


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def build_apple_exif_app1(asset_id: str, orientation: int = None) -> bytes:
    """EXIF APP1 with Make=Apple and a MakerNote holding the ContentIdentifier."""
    identifier = asset_id.encode() + b"\0"
    maker_ifd = ((1).to_bytes(2, "big")
                 + (0x0011).to_bytes(2, "big") + (2).to_bytes(2, "big")
                 + len(identifier).to_bytes(4, "big") + (26).to_bytes(4, "big")
                 + (0).to_bytes(4, "big"))
    makernote = (APPLE_MAKER_HEADER + b"MM\x00\x2a" + (8).to_bytes(4, "big")
                 + maker_ifd + identifier)

    entries = [(0x010F, 2, b"Apple\x00"), (0x927C, 7, makernote)]
    if orientation:
        entries.append((0x0112, 3, orientation.to_bytes(2, "big")))
    entries.sort()
    data_start = 8 + 2 + 12 * len(entries) + 4
    ifd = len(entries).to_bytes(2, "big")
    data = b""
    for tag, exif_type, value in entries:
        if len(value) <= 4:
            field = value.ljust(4, b"\0")
        else:
            field = (data_start + len(data)).to_bytes(4, "big")
            data += value + (b"\0" if len(value) % 2 else b"")
        ifd += tag.to_bytes(2, "big") + exif_type.to_bytes(2, "big")
        ifd += len(value).to_bytes(4, "big") + field
    ifd += (0).to_bytes(4, "big")
    payload = b"Exif\x00\x00" + b"MM\x00\x2a" + (8).to_bytes(4, "big") + ifd + data
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def _read_exif_orientation(jpeg: bytes):
    """Orientation (tag 0x0112) from an existing EXIF APP1, or None."""
    for kind, marker, mpos, seg_end in scan_pre_sos_segments(jpeg):
        if kind == "segment" and marker == 0xE1 and jpeg[mpos + 4:seg_end].startswith(EXIF_NS):
            tiff = jpeg[mpos + 10:seg_end]
            if len(tiff) < 14:
                return None
            endian = ">" if tiff[:2] == b"MM" else "<"
            ifd_at = int.from_bytes(tiff[4:8], "big" if endian == ">" else "little")
            count = int.from_bytes(tiff[ifd_at:ifd_at + 2],
                                   "big" if endian == ">" else "little")
            for i in range(count):
                entry = tiff[ifd_at + 2 + 12 * i:ifd_at + 14 + 12 * i]
                if len(entry) < 12:
                    break
                tag = int.from_bytes(entry[0:2], "big" if endian == ">" else "little")
                if tag == 0x0112:
                    return int.from_bytes(entry[8:10], "big" if endian == ">" else "little")
            return None
    return None


def create_apple_photo(image_bytes: bytes, asset_id: str) -> bytes:
    """Clean JPEG (no trailer, no Google XMP) carrying the Apple ContentIdentifier."""
    eoi_end = find_jpeg_eoi(image_bytes)
    jpeg = image_bytes[:eoi_end]
    new_exif = build_apple_exif_app1(asset_id, _read_exif_orientation(jpeg))

    spans = []  # (start, end, replacement)
    exif_replaced = False
    insert_at = 2
    for kind, marker, mpos, seg_end in scan_pre_sos_segments(jpeg):
        if kind != "segment":
            break
        payload = jpeg[mpos + 4:seg_end]
        if marker == 0xE0 and payload.startswith(b"JFIF\x00"):
            insert_at = seg_end
        elif marker == 0xE1 and payload.startswith(EXIF_NS) and not exif_replaced:
            spans.append((mpos, seg_end, new_exif))
            exif_replaced = True
        elif marker == 0xE1 and payload.startswith(XMP_NS):
            spans.append((mpos, seg_end, b""))  # stale motion-photo XMP is dropped
    if not exif_replaced:
        spans.append((insert_at, insert_at, new_exif))

    out = jpeg
    for start, end, replacement in sorted(spans, reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def _shift_chunk_offsets(buf: bytearray, start: int, end: int,
                         delta: int, threshold: int):
    """Add ``delta`` to stco/co64 entries at or after ``threshold`` (box insert).

    ``start``/``end`` delimit a region of child boxes (a container's payload).
    """
    for box_type, s, e in _iter_boxes(bytes(buf[start:end]), 0, end - start):
        abs_start, abs_end = start + s, start + e
        if box_type in (b"stco", b"co64"):
            count_at = abs_start + 4  # skip version/flags, read entry_count
            count = int.from_bytes(buf[count_at:count_at + 4], "big")
            size = 4 if box_type == b"stco" else 8
            for i in range(count):
                at = count_at + 4 + i * size
                value = int.from_bytes(buf[at:at + size], "big")
                if value >= threshold:
                    buf[at:at + size] = (value + delta).to_bytes(size, "big")
        elif box_type in (b"trak", b"mdia", b"minf", b"stbl"):
            _shift_chunk_offsets(buf, abs_start, abs_end, delta, threshold)


def build_apple_mov(video_bytes: bytes, asset_id: str) -> bytes:
    """Turn MP4 bytes into a QuickTime .mov linked to the photo by the UUID.

    The major brand is switched to 'qt  ' and a moov/udta/meta subtree with the
    mdta-space content identifier is appended as the last moov child.  Chunk
    offsets pointing past the insertion point are shifted so the file stays
    playable without a remux.
    """
    data = bytearray(video_bytes)
    boxes = list(_iter_boxes(bytes(data), 0, len(data)))

    for box_type, start, _end in boxes:
        if box_type == b"ftyp":
            data[start:start + 4] = b"qt  "  # QuickTime major brand
            break

    moov = next(((s, e) for t, s, e in boxes if t == b"moov"), None)
    if moov is None:
        raise MotionPhotoError("video has no moov box; cannot write Apple metadata")

    hdlr = _box(b"hdlr", b"\x00" * 8 + b"mdta" + b"\x00" * 12 + b"\x00")
    keys = _box(b"keys",
                (0).to_bytes(4, "big") + (1).to_bytes(4, "big")
                + (8 + len(APPLE_CONTENT_KEY)).to_bytes(4, "big")
                + b"mdta" + APPLE_CONTENT_KEY)
    value_data = _box(b"data", (1).to_bytes(4, "big") + (0).to_bytes(4, "big")
                      + asset_id.encode())  # data type 1 = UTF-8, locale 0
    ilst = _box(b"ilst", (1).to_bytes(4, "big") + value_data)
    udta = _box(b"udta", _box(b"meta", (0).to_bytes(4, "big") + hdlr + keys + ilst))

    moov_start, moov_end = moov  # payload start / box end of the moov box
    data[moov_end:moov_end] = udta  # udta becomes the last moov child
    size_at = moov_start - 8  # the moov box's own size field
    new_size = int.from_bytes(data[size_at:size_at + 4], "big") + len(udta)
    data[size_at:size_at + 4] = new_size.to_bytes(4, "big")
    _shift_chunk_offsets(data, moov_start, moov_end, len(udta), moov_end)
    return bytes(data)


# --------------------------------------------------------------------------
# Honor/Huawei dynamic photo format
# --------------------------------------------------------------------------
# Reverse-engineered from a real 2026 Honor capture (IMG_20261004_144401.jpg):
# a plain JPEG whose XMP follows the official Android Motion Photo v1 scheme,
# with the MP4 as the exact tail of the file.  No vendor trailer at all:
#
#   GCamera:MotionPhoto="1", GCamera:MotionPhotoVersion="1",
#   GCamera:MotionPhotoPresentationTimestampUs / VideoTrimStartUs / EndUs
#   Container:Directory -> Primary image/jpeg Item:Length="0" (extends to the
#   next item) + MotionPhoto video/mp4 Item:Length=<exact MP4 size>
#
# The video starts at file_size - Item:Length, and there is no LIVE_ marker
# anywhere (that marker belongs to older Huawei/Honor captures).

_HONOR_XMP_TEMPLATE = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1.0-jc003">\n'
    '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '    <rdf:Description rdf:about=""\n'
    '        xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"\n'
    '        xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
    '        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"\n'
    '      GCamera:MotionPhoto="1"\n'
    '      GCamera:MotionPhotoVersion="1"\n'
    '      GCamera:MotionPhotoPresentationTimestampUs="{presentation_us}"\n'
    '      GCamera:MotionPhotoVideoTrimStartUs="0"\n'
    '      GCamera:MotionPhotoVideoTrimEndUs="{trim_end_us}">\n'
    '      <Container:Directory>\n'
    '        <rdf:Seq>\n'
    '          <rdf:li rdf:parseType="Resource">\n'
    '            <Container:Item\n'
    '              Item:Mime="image/jpeg"\n'
    '              Item:Semantic="Primary"\n'
    '              Item:Length="0"\n'
    '              Item:Padding="0"/>\n'
    '          </rdf:li>\n'
    '          <rdf:li rdf:parseType="Resource">\n'
    '            <Container:Item\n'
    '              Item:Mime="video/mp4"\n'
    '              Item:Semantic="MotionPhoto"\n'
    '              Item:Length="{video_len}"\n'
    '              Item:Padding="0"/>\n'
    '          </rdf:li>\n'
    '        </rdf:Seq>\n'
    '      </Container:Directory>\n'
    '    </rdf:Description>\n'
    '  </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>\n'
)


def build_honor_xmp_app1(video_len: int, duration: float = None) -> bytes:
    """Honor's Android-Motion-Photo-v1 XMP segment (no self-referential sizes)."""
    if duration:
        presentation_us = int(duration * 1_000_000) // 2  # still taken mid-clip
        trim_end_us = int(duration * 1_000_000)
    else:
        presentation_us, trim_end_us = 0, 0
    xmp = _HONOR_XMP_TEMPLATE.format(
        presentation_us=presentation_us, trim_end_us=trim_end_us, video_len=video_len
    )
    return wrap_xmp_app1(xmp.encode("utf-8"))


def create_honor_photo(image_bytes: bytes, video_bytes: bytes,
                       duration: float = None) -> bytes:
    """Honor dynamic photo: JPEG + Android v1 XMP + MP4 as the exact file tail."""
    if not video_bytes:
        raise MotionPhotoError("video file is empty")
    eoi_end = find_jpeg_eoi(image_bytes)
    jpeg = image_bytes[:eoi_end]  # drop any existing trailer
    return _assemble_motion_photo(
        jpeg, video_bytes,
        lambda _base: build_honor_xmp_app1(len(video_bytes), duration))


# --------------------------------------------------------------------------
# AI-generated metadata detection & removal
# --------------------------------------------------------------------------
# Phone galleries label AI images via a handful of well-known markers:
# C2PA/JUMBF content-credential manifests (APP11; written by Adobe Firefly,
# OpenAI, Leica, Sony, Samsung, ...), the IPTC "algorithmic media" digital
# source type and generator names inside XMP, generator names in EXIF string
# values (Software, UserComment, MakerNote, ...), Photoshop/IPTC blocks
# (APP13) and JPEG comments.  strip_ai_metadata() finds and removes them all;
# EXIF values are zero-filled in place so every TIFF offset -- orientation
# included -- stays valid.

AI_METADATA_PATTERNS = (
    # Western generators and platforms
    "stable diffusion", "stable-diffusion", "stablediffusion",
    "automatic1111", "comfyui", "invokeai", "fooocus", "diffusers",
    "midjourney", "niji journey", "dall-e", "dall·e", "dalle", "openai",
    "sora", "firefly", "black forest labs", "flux.1", "google imagen",
    "nano banana", "gemini", "grok-imagine", "runwayml", "pika labs",
    "luma dream", "ideogram", "leonardo.ai", "leonardo ai", "recraft",
    "novelai", "copilot",
    # Chinese generators and platforms
    "jimeng", "即梦", "dreamina", "doubao", "豆包", "seedream", "seedance",
    "kling", "可灵", "wenxin", "文心", "tongyi", "通义万相", "通义",
    "wanxiang", "万相", "hunyuan", "混元", "vidu", "pixverse", "ailab",
    "aigc",
    # generic AI statements and the IPTC algorithmic-media source type
    "ai generated", "ai-generated", "generated by ai", "generative ai",
    "artificial intelligence", "synthetic media", "ai生成", "ai合成",
    "ai创作", "由ai", "生成式", "algorithmicmedia",
)

EXIF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
                   10: 8, 11: 4, 12: 8}

_SUB_IFD_NAMES = {0x8769: "Exif", 0x8825: "GPS", 0xA005: "Interop"}

_AI_EXIF_TAG_NAMES = {
    0x000B: "ProcessingSoftware", 0x010E: "ImageDescription",
    0x0116: "HostComputer", 0x0131: "Software", 0x013B: "Artist",
    0x8298: "Copyright", 0x927C: "MakerNote", 0x9286: "UserComment",
    0x9C9B: "XPTitle", 0x9C9C: "XPComment", 0x9C9D: "XPAuthor",
    0x9C9E: "XPKeywords", 0x9C9F: "XPSubject",
}


def iter_exif_entries(data: bytes):
    """Yield the EXIF entries of the first Exif APP1 segment as dicts.

    Each dict has ``ifd`` (IFD0/IFD1/Exif/GPS/Interop), ``tag``, ``type``,
    ``count``, ``size``, ``value`` (raw value bytes) and ``value_at`` (the
    value's absolute offset inside ``data``, inline or out-of-line).  Sub-IFD
    pointer entries are descended into, not yielded.
    """
    tiff_at = tiff_end = None
    for kind, marker, mpos, seg_end in scan_pre_sos_segments(data):
        if kind == "segment" and marker == 0xE1 and data[mpos + 4:seg_end].startswith(EXIF_NS):
            tiff_at, tiff_end = mpos + 10, seg_end  # skip FF E1 <len2> "Exif\0\0"
            break
    if tiff_at is None:
        return
    if data[tiff_at:tiff_at + 2] == b"II":
        order = "little"
    elif data[tiff_at:tiff_at + 2] == b"MM":
        order = "big"
    else:
        return

    def u16(at):
        return int.from_bytes(data[at:at + 2], order)

    def u32(at):
        return int.from_bytes(data[at:at + 4], order)

    def read_ifd(offset):
        """(entries, next-IFD offset); empty when the offset is unusable."""
        start = tiff_at + offset
        if offset < 8 or start + 2 > tiff_end:
            return [], 0
        count = u16(start)
        if count > 512 or start + 2 + 12 * count + 4 > tiff_end:
            return [], 0
        entries = []
        for i in range(count):
            entry_at = start + 2 + 12 * i
            tag, exif_type, value_count = u16(entry_at), u16(entry_at + 2), u32(entry_at + 4)
            size = EXIF_TYPE_SIZES.get(exif_type, 1) * value_count
            value_at = entry_at + 8 if size <= 4 else tiff_at + u32(entry_at + 8)
            value = (data[value_at:value_at + size]
                     if 0 <= value_at and value_at + size <= len(data) else b"")
            entries.append({"entry_at": entry_at, "tag": tag, "type": exif_type,
                            "count": value_count, "size": size,
                            "value_at": value_at, "value": value})
        return entries, u32(start + 2 + 12 * count)

    queue = [(u32(tiff_at + 4), "IFD0")]
    seen = set()
    while queue:
        offset, name = queue.pop(0)
        if not offset or offset in seen:
            continue
        seen.add(offset)
        entries, next_offset = read_ifd(offset)
        for entry in entries:
            if entry["type"] == 4 and entry["count"] == 1 and entry["tag"] in _SUB_IFD_NAMES:
                queue.append((u32(entry["entry_at"] + 8), _SUB_IFD_NAMES[entry["tag"]]))
                continue
            entry["ifd"] = name
            yield entry
        if name == "IFD0" and next_offset:
            queue.append((next_offset, "IFD1"))


def _ai_pattern_hit(text: str):
    """The first AI metadata pattern contained in ``text``, or None."""
    low = text.lower()
    return next((p for p in AI_METADATA_PATTERNS if p in low), None)


def _value_ai_pattern(raw: bytes):
    """The first AI pattern found in ``raw`` under any plausible text coding."""
    candidates = []
    if len(raw) > 8:  # EXIF UserComment: 8-byte charset prefix + payload
        prefix, body = raw[:8], raw[8:]
        if prefix.rstrip(b"\x00") in (b"ASCII", b"UNICODE"):
            encoding = "utf-16-le" if prefix.startswith(b"UNICODE") else "utf-8"
            candidates.append(body.decode(encoding, "ignore"))
    candidates.append(raw.decode("utf-8", "ignore"))
    if len(raw) % 2 == 0:
        candidates.append(raw.decode("utf-16-le", "ignore"))
        candidates.append(raw.decode("utf-16-be", "ignore"))
    for text in candidates:
        hit = _ai_pattern_hit(text)
        if hit:
            return hit
    return None


def _value_snippet(raw: bytes, limit: int = 48) -> str:
    """Short printable rendering of a metadata value, for reports."""
    best = ""
    for encoding in ("utf-8", "utf-16-le"):
        text = "".join(ch for ch in raw.decode(encoding, "ignore") if ch.isprintable())
        if len(text) > len(best):
            best = text
    return best[:limit] + ("\u2026" if len(best) > limit else "")


def _is_c2pa_container(payload: bytes) -> bool:
    """True when an APP11 payload carries JUMBF/C2PA content-credential data."""
    return b"JUMBF" in payload or b"c2pa" in payload


_XML_TEXT_ELEMENT_RE = re.compile(
    r"<([A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*)([^>]*)>([^<]+)"
    r"</([A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*)\s*>"
)
_XML_ATTRIBUTE_RE = re.compile(r'\s([A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)="([^"]*)"')


def _clean_xmp_text(xmp: str):
    """Strip AI-labelled properties from XMP text.

    Returns ``(cleaned_text, notes)`` where ``cleaned_text`` is None when the
    whole packet must be dropped (an AI pattern sits in markup the element/
    attribute passes cannot edit, e.g. CDATA or nested structures).
    """
    notes = []

    def is_algorithmic_source(name: str, value: str) -> bool:
        return (name.lower().endswith("digitalsourcetype")
                and "algorithmicmedia" in value.lower())

    def drop_element(match):
        name, text = match.group(1), match.group(3)
        if match.group(4) != name:
            return match.group(0)
        hit = _ai_pattern_hit(text)
        if hit is None and not is_algorithmic_source(name, text):
            return match.group(0)
        notes.append(f"XMP <{name}> ({hit or 'IPTC algorithmic-media source type'})")
        return ""

    def drop_attribute(match):
        name, value = match.group(1), match.group(2)
        hit = _ai_pattern_hit(value)
        if hit is None and not is_algorithmic_source(name, value):
            return match.group(0)
        notes.append(f'XMP {name}="..." ({hit or "IPTC algorithmic-media source type"})')
        return ""

    cleaned = _XML_TEXT_ELEMENT_RE.sub(drop_element, xmp)
    cleaned = _XML_ATTRIBUTE_RE.sub(drop_attribute, cleaned)
    if _ai_pattern_hit(cleaned) is not None:
        notes.append("XMP packet (whole packet removed: pattern in uneditable markup)")
        return None, notes
    return cleaned, notes


def _plan_ai_removals(data: bytes):
    """Scan ``data`` for AI-generation metadata.

    Returns ``(findings, plan)``: findings are ``{"source", "detail"}`` dicts
    for reporting; plan actions are ``("drop", start, end)`` to delete a JPEG
    segment, ``("blank", start, length)`` to zero out an EXIF value in place,
    and ``("xmp", start, end, payload)`` to rebuild the XMP APP1 segment.
    """
    findings, plan = [], []
    exif_done = xmp_done = False
    for kind, marker, mpos, seg_end in scan_pre_sos_segments(data):
        if kind != "segment":
            continue
        payload = data[mpos + 4:seg_end]
        if marker == 0xEB and _is_c2pa_container(payload):
            findings.append({"source": "C2PA",
                             "detail": "content-credentials/JUMBF manifest in APP11"})
            plan.append(("drop", mpos, seg_end))
        elif marker == 0xFE and _value_ai_pattern(payload):
            findings.append({"source": "comment",
                             "detail": f"JPEG comment: {_value_snippet(payload)!r}"})
            plan.append(("drop", mpos, seg_end))
        elif marker == 0xED and _value_ai_pattern(payload):
            findings.append({"source": "photoshop",
                             "detail": "AI-labelled IPTC/Photoshop data in APP13"})
            plan.append(("drop", mpos, seg_end))
        elif marker == 0xE1 and payload.startswith(XMP_NS) and not xmp_done:
            xmp_done = True
            cleaned, notes = _clean_xmp_text(
                payload[len(XMP_NS):].decode("utf-8", "replace"))
            if notes:
                findings.append({"source": "xmp", "detail": "; ".join(notes)})
                if cleaned is None:
                    plan.append(("drop", mpos, seg_end))
                else:
                    plan.append(("xmp", mpos, seg_end, XMP_NS + cleaned.encode("utf-8")))
        elif marker == 0xE1 and payload.startswith(EXIF_NS) and not exif_done:
            exif_done = True
            for entry in iter_exif_entries(data):
                if entry["type"] not in (1, 2, 7) or entry["size"] <= 0:
                    continue
                hit = _value_ai_pattern(entry["value"])
                if hit:
                    label = _AI_EXIF_TAG_NAMES.get(entry["tag"],
                                                   f"tag 0x{entry['tag']:04X}")
                    findings.append({
                        "source": "exif",
                        "detail": (f"{label} = "
                                   f"{_value_snippet(entry['value'])!r} ({hit})"),
                    })
                    plan.append(("blank", entry["value_at"], entry["size"]))
    return findings, plan


def find_ai_markers(data: bytes) -> list:
    """List AI-generation metadata markers in a JPEG as {'source', 'detail'} dicts."""
    return _plan_ai_removals(data)[0]


def strip_ai_metadata(data: bytes):
    """Remove AI-generation metadata from JPEG bytes; return (clean, notes).

    Segments naming a generator are dropped (C2PA manifests, comments,
    Photoshop blocks), XMP properties are removed from the packet, and EXIF
    values are zero-filled in place so all TIFF offsets stay valid.  Clean
    input is returned byte-identical with an empty note list.
    """
    findings, plan = _plan_ai_removals(data)
    if not plan:
        return data, []
    buf = bytearray(data)
    for action in plan:  # in-place blanks first: every remaining offset stays valid
        if action[0] == "blank":
            _, start, length = action
            buf[start:start + length] = b"\x00" * length
    for action in sorted((a for a in plan if a[0] != "blank"),
                         key=lambda a: a[1], reverse=True):
        if action[0] == "drop":
            _, start, end = action
            del buf[start:end]
        else:
            _, start, end, payload = action
            segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
            buf[start:end] = segment
    return bytes(buf), [f"{f['source']}: {f['detail']}" for f in findings]


# --------------------------------------------------------------------------
# MP4 inspection (no ffprobe needed)
# --------------------------------------------------------------------------

def _iter_boxes(data: bytes, start: int, end: int):
    """Yield (box_type, payload_start, payload_end) for ISO-BMFF boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        header = 8
        if size == 1:  # 64-bit largesize follows the type
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:  # box extends to the end of its enclosing container
            size = end - pos
        if size < header or pos + size > end:
            return
        yield data[pos + 4:pos + 8], pos + header, pos + size
        pos += size


def _parse_mvhd(data: bytes, start: int):
    """Duration in seconds from an mvhd box payload, or None."""
    if data[start:start + 1] == b"\x01":  # version 1
        if start + 32 > len(data):
            return None
        timescale = int.from_bytes(data[start + 20:start + 24], "big")
        duration = int.from_bytes(data[start + 24:start + 32], "big")
    else:
        if start + 20 > len(data):
            return None
        timescale = int.from_bytes(data[start + 12:start + 16], "big")
        duration = int.from_bytes(data[start + 16:start + 20], "big")
    if not timescale or not duration:
        return None
    return duration / timescale


def _parse_tkhd(data: bytes, start: int, end: int):
    """(width, height) display size from a tkhd box payload, or None."""
    version1 = data[start:start + 1] == b"\x01"
    off = 52 if version1 else 40  # offset of the transformation matrix
    if start + off + 44 > end:
        return None
    matrix = [
        int.from_bytes(data[start + off + i * 4:start + off + i * 4 + 4],
                       "big", signed=True)
        for i in range(9)
    ]
    width = int.from_bytes(data[start + off + 36:start + off + 40], "big") >> 16
    height = int.from_bytes(data[start + off + 40:start + off + 44], "big") >> 16
    if matrix[0] == 0 and matrix[4] == 0:  # 90/270-degree rotation display matrix
        width, height = height, width
    if width <= 0 or height <= 0:
        return None
    return width, height


def _parse_trak(data: bytes, start: int, end: int):
    """(handler_type, display_size_or_None) for one trak box."""
    handler = None
    dims = None
    for box_type, s, e in _iter_boxes(data, start, end):
        if box_type == b"tkhd":
            dims = _parse_tkhd(data, s, e)
        elif box_type == b"mdia":
            for sub_type, s2, _e2 in _iter_boxes(data, s, e):
                if sub_type == b"hdlr":
                    handler = data[s2 + 8:s2 + 12]
    return handler, dims


def probe_mp4(data: bytes) -> dict:
    """Read duration, audio presence and video display size from a standard MP4.

    Returns {"duration": float|None, "has_audio": bool, "width"/"height": int|None}
    where the size accounts for 90/270-degree rotation metadata.
    """
    duration = None
    has_audio = False
    width = height = None
    for box_type, start, end in _iter_boxes(data, 0, len(data)):
        if box_type != b"moov":
            continue
        for sub_type, s, e in _iter_boxes(data, start, end):
            if sub_type == b"mvhd":
                duration = _parse_mvhd(data, s)
            elif sub_type == b"trak":
                handler, dims = _parse_trak(data, s, e)
                if handler == b"soun":
                    has_audio = True
                elif handler == b"vide" and dims:
                    width, height = dims
    return {"duration": duration, "has_audio": has_audio,
            "width": width, "height": height}


# --------------------------------------------------------------------------
# Optional video processing (requires ffmpeg)
# --------------------------------------------------------------------------

MAX_DIMENSION = 1440  # cap for the long edge when compressing (Pixel-class)
COMPRESS_CRF = "28"
SPEED_ONLY_CRF = "18"  # retime/crop only, so keep quality high
DURATION_EPSILON = 0.05  # seconds; videos within this of the target are left alone
ASPECT_TOLERANCE = 0.002  # relative; below this, aspect differences are sub-pixel
BAR_MAX_MEAN = 20.0     # a bar line's mean brightness must not exceed this
BAR_MAX_STDEV = 12.0    # ...and its spread must stay low (real content has texture)
BAR_MIN_PIXELS = 4      # narrower strips are left alone (invisible after rounding)
BAR_MAX_FRACTION = 0.2  # never trim more than this fraction per side (false-positive guard)

FFMPEG_INSTALL_HINT = (
    "video processing (--compress/--duration/--volume/aspect cropping) requires "
    "ffmpeg; install it (e.g. 'winget install Gyan.FFmpeg' or from "
    "https://ffmpeg.org/download.html) or pass --ffmpeg PATH"
)


def resolve_ffmpeg(explicit: str = None) -> Path:
    """Locate the ffmpeg binary: explicit path, then PATH, then imageio_ffmpeg."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise MotionPhotoError(f"ffmpeg not found at {path}")
        return path
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    try:
        import imageio_ffmpeg
        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        raise MotionPhotoError(FFMPEG_INSTALL_HINT) from None


def build_atempo_chain(speed: float) -> str:
    """An atempo filter chain with every stage <= 2.0 (the classic safe limit)."""
    parts = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.000000")
        remaining /= 2.0
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


def _aspect_window_expressions(aspect: float):
    """(w, h) ffmpeg expressions for the largest aspect-ratio rectangle that
    fits inside the frame, rounded to the nearest even dimension."""
    return (
        f"2*trunc(min(iw,ih*{aspect:.6f})/2+0.5)",
        f"2*trunc(min(ih,iw/{aspect:.6f})/2+0.5)",
    )


def build_transcode_cmd(ffmpeg: Path, src: Path, dst: Path, *, speed=None,
                        compress=False, has_audio=False, match_aspect=None,
                        match_mode="crop", trim=(0, 0, 0, 0), volume=1.0) -> list:
    keep_audio = volume > 0
    cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-map", "0:v:0"]
    if keep_audio:
        cmd += ["-map", "0:a:0?"]  # optional: no audio track is not an error
    else:
        cmd += ["-an"]  # --volume 0: the output carries no audio stream at all
    video_filters = []
    if speed:
        video_filters.append(f"setpts=PTS/{speed:.6f}")
    top, bottom, left, right = trim
    if top or bottom or left or right:
        # explicit edge trim (manual --crop and/or auto-detected bars); runs
        # before the aspect window so that operates on the cleaned frame
        video_filters.append(
            f"crop=iw-{left + right}:ih-{top + bottom}:{left}:{top}"
        )
    if match_aspect:
        # both methods target the same window (the largest photo-AR rectangle
        # that fits inside the frame, so nothing is ever padded or upscaled):
        # crop trims the overflowing edges, stretch squeezes the full frame in.
        # Quotes protect the commas inside the expressions from ffmpeg's
        # filter-chain parser.
        w_expr, h_expr = _aspect_window_expressions(match_aspect)
        if match_mode == "stretch":
            video_filters.append(f"scale='{w_expr}':'{h_expr}'")
        else:
            video_filters.append(f"crop='{w_expr}':'{h_expr}'")
    if compress:
        # min() keeps the cap from ever upscaling.
        video_filters.append(
            f"scale='min({MAX_DIMENSION},iw)':'min({MAX_DIMENSION},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    if video_filters:
        cmd += ["-vf", ",".join(video_filters)]
    if has_audio and keep_audio:
        audio_filters = []
        if speed:
            audio_filters.append(build_atempo_chain(speed))
        if volume != 1:
            audio_filters.append(f"volume={volume:g}")
        if audio_filters:
            cmd += ["-af", ",".join(audio_filters)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-crf", COMPRESS_CRF if compress else SPEED_ONLY_CRF]
    if speed:
        cmd += ["-r", "30"]  # resample so sped-up clips keep a normal frame rate
    if has_audio and keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k" if compress else "128k"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(dst)]
    return cmd


def build_audio_remux_cmd(ffmpeg: Path, src: Path, dst: Path, *, volume: float) -> list:
    """Audio-only pass: the video stream is copied untouched, the audio track
    is dropped (``volume`` 0) or re-encoded at the requested level."""
    cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-map", "0:v:0", "-c:v", "copy"]
    if volume > 0:
        cmd += ["-map", "0:a:0?", "-af", f"volume={volume:g}",
                "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    return cmd + ["-movflags", "+faststart", "-f", "mp4", str(dst)]


def _decode_gray_frame(ffmpeg: Path, video_path: Path, timestamp: float,
                       width: int, height: int):
    """Decode one frame as 8-bit gray (display-oriented) or None."""
    cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{timestamp:.3f}", "-i", str(video_path),
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or len(result.stdout) < width * height:
        return None
    return result.stdout[:width * height]


def analyze_black_bars(frame: bytes, width: int, height: int):
    """(top, bottom, left, right) black-bar pixel counts of a gray frame.

    A line counts as bar when it is near-black AND nearly flat (low spread) —
    dark-but-textured scene content is left alone. Amounts are rounded down to
    even pixels and capped at BAR_MAX_FRACTION per side.
    """
    row_stats = []
    for y in range(height):
        row = frame[y * width:(y + 1) * width]
        mean = sum(row) / width
        var = sum(v * v for v in row) / width - mean * mean
        row_stats.append((mean, max(var, 0.0) ** 0.5))
    col_sums = [0] * width
    col_squares = [0] * width
    for y in range(height):
        row = frame[y * width:(y + 1) * width]
        for x, v in enumerate(row):
            col_sums[x] += v
            col_squares[x] += v * v
    col_stats = [(col_sums[x] / height,
                  max(col_squares[x] / height - (col_sums[x] / height) ** 2, 0.0) ** 0.5)
                 for x in range(width)]

    def is_bar(stats):
        return stats[0] <= BAR_MAX_MEAN and stats[1] <= BAR_MAX_STDEV

    def bounds(stats, total):
        cap = int(total * BAR_MAX_FRACTION)
        head = 0
        while head < cap and is_bar(stats[head]):
            head += 1
        tail = 0
        while tail < cap and is_bar(stats[total - 1 - tail]):
            tail += 1
        return head, tail

    def even_or_zero(count):
        return count // 2 * 2 if count >= BAR_MIN_PIXELS else 0

    top, bottom = bounds(row_stats, height)
    left, right = bounds(col_stats, width)
    return tuple(even_or_zero(v) for v in (top, bottom, left, right))


def detect_black_bars(video_path: Path, ffmpeg: Path, *, duration=None,
                      width=None, height=None):
    """Analyse a mid-clip frame; returns the bar tuple, or None if impossible."""
    if not width or not height:
        return None
    timestamp = duration / 2 if duration else 0.0
    frame = _decode_gray_frame(ffmpeg, video_path, timestamp, width, height)
    if frame is None:
        return None
    return analyze_black_bars(frame, width, height)


def _stderr_tail(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text[-limit:] if text else "(no stderr output)"


def process_video(video_path: Path, ffmpeg: Path, *, target_duration=None,
                  compress=False, match_aspect=None, match_mode="crop",
                  trim=(0, 0, 0, 0), volume=1.0, audio_pass=False):
    """Optionally re-encode the video; returns (video_bytes, report).

    ``match_aspect`` is a width/height ratio the video is conformed to using
    ``match_mode`` ("crop" trims the overflowing edges, "stretch" squeezes the
    full frame in).  ``trim`` is a (top, bottom, left, right) pixel edge trim
    applied first.  ``volume`` is the output audio level (0 removes the audio
    track, 1 keeps the original sound, other values scale it); ``audio_pass``
    runs a fast audio-only remux when nothing else needs a re-encode.  The
    report dict has "changed" (bool), "speed" (factor or None), "duration"
    (original duration in seconds or None) and "audio" ("muted", the volume
    float, or None when the audio was left alone).
    """
    raw = video_path.read_bytes()
    probe = probe_mp4(raw)
    duration, has_audio = probe["duration"], probe["has_audio"]

    speed = None
    if target_duration is not None:
        if duration is None:
            raise MotionPhotoError(
                f"could not read the MP4 duration of {video_path}; only standard "
                "non-fragmented MP4 files are supported for --duration"
            )
        if duration > target_duration + DURATION_EPSILON:
            speed = duration / target_duration
    reencode = speed is not None or compress or match_aspect or any(trim)
    if audio_pass and volume == 1:
        audio_pass = False  # original volume: an audio pass would change nothing
    if not reencode and not audio_pass:
        return raw, {"changed": False, "speed": None, "duration": duration,
                     "audio": None}

    fd, tmp_name = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if reencode:
            cmd = build_transcode_cmd(ffmpeg, video_path, tmp, speed=speed,
                                      compress=compress, has_audio=has_audio,
                                      match_aspect=match_aspect,
                                      match_mode=match_mode, trim=trim,
                                      volume=volume)
        else:
            cmd = build_audio_remux_cmd(ffmpeg, video_path, tmp, volume=volume)
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise MotionPhotoError(
                "ffmpeg failed while processing the video:\n"
                f"  command: {' '.join(cmd)}\n"
                f"  {_stderr_tail(result.stderr)}"
            )
        if not has_audio:
            audio = None
        elif volume <= 0:
            audio = "muted"
        elif volume != 1:
            audio = volume
        else:
            audio = None
        return tmp.read_bytes(), {"changed": True, "speed": speed,
                                  "duration": duration, "audio": audio}
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------

def extract_xmp(data: bytes):
    """Return the text of the first Adobe-namespace XMP APP1 packet, or None."""
    for kind, marker, marker_pos, seg_end in scan_pre_sos_segments(data):
        if kind == "segment" and marker == 0xE1 and data[marker_pos + 4:seg_end].startswith(XMP_NS):
            return data[marker_pos + 4 + len(XMP_NS):seg_end].decode("utf-8", "replace")
    return None


def parse_container_items(xmp: str):
    """Parse Container:Item entries from XMP text into a list of attr dicts."""
    items = []
    for match in re.finditer(r"<Container:Item\b([^>]*?)/?>", xmp):
        items.append(dict(re.findall(r'Item:(\w+)\s*=\s*"([^"]*)"', match.group(1))))
    return items or None


def parse_micro_video(xmp: str):
    """Parse GCamera/Camera:MicroVideo* properties from XMP text, or None."""
    found = re.findall(r"<(?:[A-Za-z_]+:)?MicroVideo(Version|Offset)>\s*(\d+)\s*</", xmp)
    return {key: int(value) for key, value in found} or None


def inspect_motion_photo(data: bytes) -> dict:
    """Inspect a candidate motion photo and return findings plus consistency checks."""
    still_end = find_jpeg_eoi(data)
    xmp = extract_xmp(data)
    result = {
        "size": len(data),
        "still_end": still_end,
        "trailer_len": len(data) - still_end,
        "xmp": xmp,
        "micro_video": parse_micro_video(xmp) if xmp else None,
        "items": parse_container_items(xmp) if xmp else None,
        "ai_markers": find_ai_markers(data),
        "checks": [],
    }
    if xmp is None:
        result["checks"].append(("motion photo XMP metadata found", False))
        return result

    items = result["items"] or []
    primary = next(
        (int(i["Length"]) for i in items
         if i.get("Semantic") == "Primary" and i.get("Length")),
        None,
    )
    motion = next(
        (int(i["Length"]) for i in items
         if i.get("Semantic") == "MotionPhoto" and i.get("Length")),
        None,
    )
    if primary == 0 and motion is not None:
        # Android Motion Photo v1 (as written by Honor): Length 0 means the
        # primary item extends to the next item, so the MP4 is the file tail.
        video_start = len(data) - motion
        result["checks"].append(
            ("MotionPhoto length places the MP4 after the JPEG (Android v1)",
             still_end <= video_start <= len(data) - 8
             and data[video_start + 4:video_start + 8] == FTYP)
        )
    else:
        if primary is not None:
            result["checks"].append(
                ("Container Primary length matches JPEG end", primary == still_end)
            )
            result["checks"].append(
                ("MP4 'ftyp' signature at Primary offset",
                 data[primary + 4:primary + 8] == FTYP)
            )
        if primary is not None and motion is not None:
            result["checks"].append(
                ("file size equals Primary + MotionPhoto lengths",
                 len(data) == primary + motion)
            )
    micro = result["micro_video"]
    if micro and micro.get("Offset") is not None:
        start = len(data) - micro["Offset"]
        result["checks"].append(
            ("MicroVideoOffset points at an MP4 'ftyp' signature",
             0 <= start <= len(data) - 8 and data[start + 4:start + 8] == FTYP)
        )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MotionPhotoError(f"cannot read {path}: {exc}") from None


def _print_info(path: Path, data: bytes) -> int:
    report = inspect_motion_photo(data)
    print(f"File       : {path} ({report['size']:,} bytes)")
    print(f"JPEG still : {report['still_end']:,} bytes (EOI at offset {report['still_end']:,})")
    print(f"Trailer    : {report['trailer_len']:,} bytes")
    markers = report.get("ai_markers") or []
    if markers:
        print("AI metadata:")
        for marker in markers:
            print(f"  [{marker['source']}] {marker['detail']}")
    if report["xmp"] is None:
        print("XMP        : none found (not a motion photo)")
    else:
        print("XMP        : motion photo metadata found")
        micro = report["micro_video"]
        if micro:
            version = micro.get("Version", "?")
            offset = micro.get("Offset", "?")
            print(f"  MicroVideo: version={version} offset={offset}")
        items = report["items"]
        if items:
            print("  Container directory:")
            for index, item in enumerate(items, 1):
                print(
                    f"    [{index}] {item.get('Semantic', '?'):<12} "
                    f"{item.get('Mime', '?'):<10} length={item.get('Length', '?')}"
                )
    for label, ok in report["checks"]:
        print(f"  [{'ok' if ok else 'FAIL'}] {label}")
    return 0 if report["checks"] and all(ok for _label, ok in report["checks"]) else 1


def _clean_image(args, image_path: Path) -> int:
    """Standalone mode: check a JPEG for AI-generation tags and strip them."""
    data = _read(image_path)
    findings = find_ai_markers(data)
    if not findings and not args.output:
        print(f"{image_path}: no AI-generated metadata markers found")
        return 0
    cleaned, _notes = strip_ai_metadata(data)
    output = Path(args.output) if args.output else image_path.with_name(
        image_path.stem + "_clean" + image_path.suffix)
    try:
        output.write_bytes(cleaned)
    except OSError as exc:
        raise MotionPhotoError(f"cannot write {output}: {exc}") from None
    for marker in findings:
        print(f"  removed: [{marker['source']}] {marker['detail']}")
    if findings:
        print(f"AI metadata removed from {len(findings)} location(s): {output}")
    else:
        print(f"no AI-generated metadata markers found; clean copy written: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="motion_photo.py",
        description=(
            "Combine a JPEG still image and an MP4 video into an Android Motion "
            "Photo: the Honor 动态照片 flavour by default (verified on a real "
            "Honor phone), Google MVIMG via --format google."
        ),
        epilog=(
            "examples:\n"
            "  python motion_photo.py photo.jpg clip.mp4\n"
            "  python motion_photo.py photo.jpg clip.mp4 --compress --duration\n"
            "  python motion_photo.py photo.jpg clip.mp4 --compress --volume 1\n"
            "  python motion_photo.py photo.jpg clip.mp4 --crop-bars --stretch\n"
            "  python motion_photo.py photo.jpg clip.mp4 --crop 24:8:0:0\n"
            "  python motion_photo.py photo.jpg                        "
            "# check & strip AI-generation tags only\n"
            "  python motion_photo.py --info MVIMG_20261004_120000.jpg\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image", nargs="?", help="input JPEG file")
    parser.add_argument("video", nargs="?",
                        help="input MP4 video file (omit it to check and clean "
                             "the image's AI-generation tags only)")
    parser.add_argument("-o", "--output", metavar="PATH",
                        help="output file (default: <image>_motion.jpg)")
    parser.add_argument("--compress", action="store_true",
                        help="re-encode the video with ffmpeg to shrink it "
                             f"(H.264 CRF {COMPRESS_CRF}, max {MAX_DIMENSION}px; "
                             "audio muted unless --volume keeps it)")
    parser.add_argument("--duration", metavar="SEC", type=float, nargs="?",
                        const=3.0, default=None,
                        help="fast-forward the video to SEC seconds (default: 3 s when "
                             "the flag is given alone); videos already this short are "
                             "embedded unchanged")
    parser.add_argument("--volume", metavar="LEVEL", type=float, default=None,
                        help="audio volume of the embedded video: 0 mutes it by "
                             "removing the audio track (the default), 1 keeps the "
                             "original sound, values in between scale it and above 1 "
                             "amplify it. Applied whenever ffmpeg processes the video "
                             "(--compress/--duration/cropping); passed alone with any "
                             "level but 1 it runs a fast audio-only pass")
    parser.add_argument("--no-match-aspect", dest="match_aspect",
                        action="store_false", default=True,
                        help="disable the default centre-cropping of the video to the "
                             "still image's aspect ratio. Cropping happens only when the "
                             "aspects actually differ (players letterbox such clips with "
                             "black side bars) and requires ffmpeg; matching clips are "
                             "embedded without re-encoding")
    parser.add_argument("--stretch", action="store_true",
                        help="match the aspect ratio by stretching (non-uniform scale) "
                             "instead of centre-cropping: no picture content is lost, at "
                             "the cost of slight distortion. Only used when the aspects "
                             "actually differ")
    parser.add_argument("--crop", metavar="T:B:L:R",
                        help="trim pixels from the video edges before aspect matching: "
                             "TOP:BOTTOM:LEFT:RIGHT (e.g. --crop 24:8:0:0); amounts are "
                             "rounded up to even")
    parser.add_argument("--crop-bars", action="store_true",
                        help="detect and trim baked-in black bars at the video edges "
                             "(a mid-clip frame is analysed; dark-but-textured scene "
                             "content is not touched)")
    parser.add_argument("--ffmpeg", metavar="PATH",
                        help="ffmpeg binary to use (default: search PATH, then the "
                             "imageio_ffmpeg package)")
    parser.add_argument("--format", choices=("google", "honor", "apple"),
                        default="honor", metavar="FLAVOUR",
                        help="motion photo flavour: honor (default; Android "
                             "Motion Photo v1 XMP with the MP4 as the exact file "
                             "tail - verified playing on a real Honor phone), "
                             "google (Google Photos MVIMG: legacy MicroVideo + "
                             "Container XMP, MP4 appended after the JPEG EOI), "
                             "apple (iPhone Live Photo pair: <out>.jpg + <out>.mov "
                             "linked by a matching ContentIdentifier UUID)")
    parser.add_argument("--info", metavar="FILE",
                        help="inspect an existing motion photo and exit")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.info:
            if args.image or args.video:
                parser.error("--info cannot be combined with image/video arguments")
            return _print_info(Path(args.info), _read(Path(args.info)))

        if not args.image:
            parser.error("an image file is required (or use --info FILE)")
        if not args.video:
            return _clean_image(args, Path(args.image))

        if args.stretch and not args.match_aspect:
            parser.error("--stretch requires aspect matching; remove --no-match-aspect")
        if args.volume is not None and not args.volume >= 0:
            parser.error("--volume must be a number >= 0 (0 mutes, 1 = original)")

        manual_trim = None
        if args.crop is not None:
            match = re.fullmatch(r"(\d+):(\d+):(\d+):(\d+)", args.crop)
            if not match:
                raise MotionPhotoError(
                    "--crop expects TOP:BOTTOM:LEFT:RIGHT pixel counts, "
                    "e.g. --crop 24:8:0:0"
                )
            manual_trim = tuple((int(v) + 1) // 2 * 2 for v in match.groups())

        image_path = Path(args.image)
        video_path = Path(args.video)
        image_bytes = _read(image_path)
        video_bytes = _read(video_path)

        # AI-generation tags are stripped up front, whatever the output format.
        image_bytes, ai_removed = strip_ai_metadata(image_bytes)
        for note in ai_removed:
            print(f"  photo : removed AI metadata - {note}")

        # Default aspect matching: only needs ffmpeg when a real mismatch exists.
        video_probe = probe_mp4(video_bytes)
        photo_aspect = None
        photo_dims = None
        crop_needed = False
        aspect_checked = False
        if args.match_aspect:
            photo_dims = image_dimensions(image_bytes)
            photo_aspect = photo_dims[0] / photo_dims[1]
            if video_probe["width"] and video_probe["height"]:
                aspect_checked = True
                video_aspect = video_probe["width"] / video_probe["height"]
                crop_needed = abs(video_aspect - photo_aspect) > ASPECT_TOLERANCE * photo_aspect

        # --volume defaults to mute; a level of exactly 1 means "leave the
        # audio alone", so only other explicit levels force an ffmpeg pass.
        volume = 0.0 if args.volume is None else args.volume
        audio_pass = args.volume is not None and args.volume != 1.0
        wants_processing = (args.compress or args.duration is not None
                            or manual_trim is not None or args.crop_bars
                            or audio_pass)
        if wants_processing or crop_needed:
            if args.duration is not None and args.duration <= 0:
                raise MotionPhotoError("--duration must be a positive number of seconds")
            try:
                ffmpeg = resolve_ffmpeg(args.ffmpeg)
            except MotionPhotoError as exc:
                if crop_needed and not wants_processing:
                    raise MotionPhotoError(
                        f"{exc}\n  the video aspect ({video_probe['width']}:"
                        f"{video_probe['height']}) differs from the photo's "
                        f"({photo_dims[0]}:{photo_dims[1]}), and the default aspect "
                        "matching needs ffmpeg; pass --no-match-aspect to embed "
                        "the video unchanged"
                    ) from None
                raise

            trim = manual_trim or (0, 0, 0, 0)
            bars_detected = None
            if args.crop_bars:
                bars = detect_black_bars(video_path, ffmpeg,
                                         duration=video_probe["duration"],
                                         width=video_probe["width"],
                                         height=video_probe["height"])
                if bars is None:
                    print("warning: could not analyse a video frame; black-bar "
                          "detection skipped", file=sys.stderr)
                else:
                    bars_detected = bars
                    trim = tuple(a + b for a, b in zip(trim, bars))
            if video_probe["width"] and video_probe["height"]:
                top, bottom, left, right = trim
                if top + bottom >= video_probe["height"] or left + right >= video_probe["width"]:
                    raise MotionPhotoError(
                        "--crop/--crop-bars would remove the entire frame "
                        f"(trim {top}/{bottom}/{left}/{right} of a "
                        f"{video_probe['width']}x{video_probe['height']} video)"
                    )

            video_bytes, report = process_video(
                video_path, ffmpeg, target_duration=args.duration,
                compress=args.compress,
                match_aspect=photo_aspect if crop_needed else None,
                match_mode="stretch" if args.stretch else "crop",
                trim=trim,
                volume=volume,
                audio_pass=audio_pass,
            )
            if report["changed"]:
                details = []
                if report["speed"]:
                    details.append(f"sped up {report['speed']:.2f}x to {args.duration:g}s")
                if any(trim):
                    sides = [f"{n} px {name}" for n, name in
                             zip(trim, ("top", "bottom", "left", "right")) if n]
                    label = "black bars (" if bars_detected and any(bars_detected) else "edges ("
                    details.append("trimmed " + label + ", ".join(sides) + ")")
                if crop_needed:
                    verb = "stretched" if args.stretch else "cropped"
                    details.append(
                        f"{verb} to the photo's aspect ratio "
                        f"({photo_dims[0]}:{photo_dims[1]})"
                    )
                if args.compress:
                    details.append(
                        f"re-encoded (H.264 CRF {COMPRESS_CRF}, "
                        f"max {MAX_DIMENSION}px)"
                    )
                if report.get("audio") == "muted":
                    details.append("audio muted (track removed)")
                elif report.get("audio"):
                    details.append(f"audio volume set to {report['audio']:g}")
                print(f"  video : {'; '.join(details)} "
                      f"({video_path.stat().st_size:,} -> {len(video_bytes):,} bytes)")
            elif args.duration is not None:
                print(f"  video : already {report['duration']:.1f}s (within the "
                      f"{args.duration:g}s target); embedded unchanged")
            elif bars_detected is not None:
                print("  video : no significant black bars detected; embedded unchanged")
        elif aspect_checked:
            print("  video : aspect already matches the photo; embedded as-is")

        if not looks_like_mp4(video_bytes):
            print(
                "warning: video does not start with an MP4 'ftyp' box; "
                "Google Photos may not play it",
                file=sys.stderr,
            )

        if args.format == "apple":
            asset_id = str(uuid.uuid4()).upper()
            photo_bytes = create_apple_photo(image_bytes, asset_id)
            movie_bytes = build_apple_mov(video_bytes, asset_id)
            base = Path(args.output) if args.output else image_path.with_name(
                image_path.stem + "_live"
            )
            if base.suffix.lower() in (".jpg", ".jpeg"):
                base = base.with_suffix("")
            photo_path = base.with_suffix(".jpg")
            movie_path = base.with_suffix(".mov")
            for path, blob in ((photo_path, photo_bytes), (movie_path, movie_bytes)):
                try:
                    path.write_bytes(blob)
                except OSError as exc:
                    raise MotionPhotoError(f"cannot write {path}: {exc}") from None
            print(f"Live Photo pair written:")
            print(f"  photo : {photo_path} ({len(photo_bytes):,} bytes)")
            print(f"  video : {movie_path} ({len(movie_bytes):,} bytes)")
            print(f"  pair  : ContentIdentifier {asset_id}")
            print("  import: transfer BOTH files together (AirDrop / Mac Photos "
                  "drag-in / iCloud upload); import as a pair merges them")
            return 0

        if args.format == "honor":
            result = create_honor_photo(
                image_bytes, video_bytes, duration=probe_mp4(video_bytes)["duration"])
        else:
            result = create_motion_photo(image_bytes, video_bytes)

        output = Path(args.output) if args.output else image_path.with_name(
            image_path.stem + "_motion.jpg"
        )
        try:
            output.write_bytes(result)
        except OSError as exc:
            raise MotionPhotoError(f"cannot write {output}: {exc}") from None

        still_len = len(result) - len(video_bytes)
        print(f"Motion photo written: {output}")
        print(f"  still : {still_len:,} bytes (JPEG incl. XMP)")
        print(f"  video : {len(video_bytes):,} bytes")
        print(f"  total : {len(result):,} bytes")
        return 0
    except MotionPhotoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
