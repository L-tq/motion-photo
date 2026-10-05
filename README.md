# motion-photo

Turn a JPEG still image plus an MP4 clip into an **Android Motion Photo**.
The default output is the **Honor 动态照片** flavour (Android Motion Photo
v1 — **verified playing on a real Honor phone**); `--format google` writes
the Google `MVIMG` / MicroVideo flavour that Google Photos on Android
recognises and plays.

Pure Python, standard library only (Python 3.8+). Optional video compression
and fast-forwarding use [ffmpeg](https://ffmpeg.org) when requested.

## Usage

```
python motion_photo.py photo.jpg clip.mp4                        # Honor 动态照片 (default; auto aspect-crop on mismatch)
python motion_photo.py photo.jpg clip.mp4 --compress             # shrink the video
python motion_photo.py photo.jpg clip.mp4 --duration             # fast-forward to 3 s
python motion_photo.py photo.jpg clip.mp4 --compress --duration 4
python motion_photo.py photo.jpg clip.mp4 --compress --volume 1  # keep the original sound
python motion_photo.py photo.jpg clip.mp4 --volume 0.5           # half loudness, no re-encode
python motion_photo.py photo.jpg clip.mp4 --stretch              # match aspect by stretching
python motion_photo.py photo.jpg clip.mp4 --crop-bars            # trim baked-in black bars
python motion_photo.py photo.jpg clip.mp4 --crop 24:8:0:0        # trim exact pixel edges
python motion_photo.py photo.jpg clip.mp4 --format google        # Google Photos MVIMG
python motion_photo.py photo.jpg clip.mp4 --format apple         # iPhone Live Photo pair
python motion_photo.py photo.jpg clip.mp4 --no-match-aspect      # never touch aspect
python motion_photo.py photo.jpg clip.mp4 -o MVIMG_20261004_120000.jpg
python motion_photo.py photo.jpg                                 # check & strip AI tags only
python motion_photo.py --info MVIMG_20261004_120000.jpg          # inspect a motion photo
```

- Output defaults to `<image>_motion.jpg` next to the input (`-o` overrides).
- `--info FILE` prints the XMP metadata and verifies that the declared lengths
  actually line up with the file bytes, exiting non-zero on any mismatch. It
  also reports any AI-generation markers it finds.

## AI-generation tag removal

Phone galleries and Chinese platforms label AI images using a handful of
well-known metadata markers. The tool detects and **removes them
automatically** whenever a motion photo is built (all formats), and a run with
an image but **no video** checks and cleans a single photo (`<image>_clean.jpg`
by default, `-o` overrides):

| Marker | Where | Action |
| --- | --- | --- |
| C2PA / Content Credentials manifest | APP11 JUMBF (Adobe Firefly, OpenAI, Leica, Sony, Samsung, …) | segment dropped |
| IPTC digital source type = *algorithmic media* | XMP `Iptc4xmpExt:DigitalSourceType` (Microsoft/Bing, Meta, …) | property removed |
| Generator names (`ComfyUI`, `Midjourney`, `即梦`, `豆包`, `Stable Diffusion`, `nano banana`, …) | EXIF `Software`/`UserComment`/`MakerNote`/XP strings, XMP `CreatorTool`/`dc:*` text | EXIF value zeroed in place, XMP element/attribute removed |
| Generator names in Photoshop blocks or comments | APP13 IPTC, JPEG COM | segment dropped |

Details:

- Detection is pattern-based (a built-in list of generator/platform names and
  AI statements, matched case-insensitively under ASCII, UTF-8 and UTF-16
  codings). `digitalCapture` (a real camera) is never touched.
- EXIF values are **zeroed in place** rather than deleted, so every TIFF
  offset — orientation included — stays valid and the JPEG layout is unchanged.
- XMP properties are removed element-by-element; if a pattern survives in
  markup the passes cannot edit (e.g. CDATA), the whole XMP packet is dropped.
- If anything is removed the run prints one line per marker, e.g.
  `photo : removed AI metadata - exif: Software = 'Stable Diffusion XL' (stable diffusion)`.
- Only metadata is handled. Invisible pixel watermarks such as Google
  SynthID cannot be removed (they are not metadata).
- A real-camera photo (Sony) and a real Honor capture were scanned as
  false-positive checks: both come back clean.

## Formats (`--format`)

| Flavour | Output | Mechanism |
| --- | --- | --- |
| `honor` (default) | single `<out>.jpg` | Honor/Huawei 动态照片, reverse-engineered from a real 2026 Honor capture: **Android Motion Photo v1** XMP (`GCamera:MotionPhoto="1"` + PresentationTimestamp/VideoTrim fields, Primary `Item:Length="0"`, exact MotionPhoto length) with the MP4 as the exact file tail and **no** vendor trailer — the `LIVE_` marker seen by third-party parsers belongs to older Huawei files and is absent here. **Verified playing in the stock Honor gallery** (file transferred via Google Drive; the round trip was byte-identical, so the format itself is what the phone accepts). |
| `google` | single `<out>.jpg` | Google Motion Photo: XMP MicroVideo + Container offsets, MP4 appended after the JPEG EOI. Plays in Google Photos (Android). |
| `apple` | `<out>.jpg` + `<out>.mov` | iPhone Live Photo: the pair is linked by a matching ContentIdentifier UUID stored in the JPG's Apple maker note (MakerApple tag 17) and in the MOV's QuickTime metadata (`com.apple.quicktime.content.identifier`). The MOV gets its major brand switched to `qt  ` and the metadata atoms are inserted in place (chunk offsets patched — no remux, no re-encode). Import **both files together** (AirDrop / drag into Mac Photos / iCloud upload) so they merge into one Live Photo. |

All video-processing flags (`--compress`, `--duration`, `--volume`, aspect
matching, `--crop`, `--crop-bars`) work with every format; the processed clip
is what gets embedded or paired. Apple pairing replaces a pre-existing EXIF
segment on the still (only Orientation is carried over).

## Optional video processing

Large or long source clips are handled by these flags (all require ffmpeg;
everything else stays dependency-free):

| Flag | Effect |
| --- | --- |
| `--compress` | Re-encodes with H.264 CRF 28 / `veryfast`, `yuv420p`, and scales down to at most **1440 px on the long edge** (aspect preserved, never upscaled). Kept audio is encoded as AAC 96 k. |
| `--volume LEVEL` | Audio volume of the embedded clip: **0 (the default) mutes** it by removing the audio track outright — smaller files, and galleries play motion photos silently anyway; `1` keeps the original sound; values in between scale it (e.g. `0.5` = half loudness), above 1 amplify. It applies whenever ffmpeg processes the video (`--compress`/`--duration`/cropping), and passed alone — with any level but `1` — it runs a fast **audio-only pass** that copies the video stream untouched (`-c:v copy`, no quality loss). A plain embed without processing flags keeps the video byte-identical, audio included. |
| `--duration [SEC]` | **Fast-forwards** the clip (all content kept, just sped up) so it lasts SEC seconds — default 3 s when the flag is given alone. Video timestamps are divided by the speed factor (`setpts`), the frame rate is resampled to 30 fps, and kept audio is retimed with a chained `atempo` (each stage ≤ 2.0). Clips already at/below the target are embedded unchanged; the tool never slows a video down. |
| `--no-match-aspect` | Disables the **default** aspect matching (see below). |
| `--stretch` | Matches the aspect ratio by **stretching** (non-uniform scale) instead of centre-cropping: no picture content is lost, at the cost of slight distortion (at most the AR mismatch percentage). Only used when the aspects actually differ. |
| `--crop-bars` | Detects and trims **baked-in black bars** at the video edges. A mid-clip frame is decoded and analysed; a line only counts as a bar when it is near-black *and* nearly flat, so dark-but-textured scene content is never touched. Detection is capped at 20 % per side, strips under 4 px are ignored, and amounts are rounded down to even pixels. |
| `--crop T:B:L:R` | Trims exact pixel counts from the video edges (`TOP:BOTTOM:LEFT:RIGHT`, e.g. `--crop 24:8:0:0`) before aspect matching. Amounts are rounded up to even. Combine with `--crop-bars` — the trims add up. |

All trims run **before** aspect matching, so the aspect window operates on the
cleaned frame, and every processing flag combines into a single ffmpeg pass.
When `--crop-bars` finds no bars (or cannot decode a frame to analyse), the
video is embedded unchanged.

**Aspect matching is on by default.** Players letterbox the clip inside the
still's frame, so a clip whose aspect ratio differs from the photo's shows thin
black side bars (a ~2% mismatch is already visible). By default the tool reads
both aspect ratios (photo from the JPEG SOF, clip from the MP4 `tkhd`,
rotation-aware) and conforms the clip to the photo's exact aspect ratio
whenever they differ by more than 0.2%. Both methods target the same window —
the largest photo-AR rectangle that fits inside the frame, so nothing is ever
padded or upscaled: the default **crop** trims the overflowing edges
(centred), while `--stretch` squeezes the full frame into the window.
Clips that already match are embedded without any re-encoding — no ffmpeg
involved — and if the video's dimensions can't be read, it is embedded as-is.

- Processing flags combine into a **single** ffmpeg pass. Speed-only and
  aspect-only re-encodes use high quality (CRF 18, no scaling) since their only
  job is retiming/cropping; only `--compress` shrinks.
- Without ffmpeg installed, a detected aspect mismatch fails with a clear
  message (remedies: install ffmpeg, or `--no-match-aspect`).
- The video duration is read with a built-in pure-Python MP4 box parser
  (`moov`/`mvhd` + audio track detection) — **no ffprobe needed**.
- ffmpeg is found via `--ffmpeg PATH`, then `PATH`, then the
[`imageio_ffmpeg`](https://pypi.org/project/imageio-ffmpeg/) package if it is
installed. Install hints are printed when it is missing (e.g.
`winget install Gyan.FFmpeg`).

Example: a 10 s 1920×1080 clip (7.2 MB) became a 3 s 1440×810 trailer
(0.57 MB, H.264 30 fps, sound kept via `--volume 1`) with
`--compress --duration`.

## How it works

A motion photo is an ordinary JPEG file with two additions:

```
+-----------------------------------+--------------------------+
|  JPEG (still + XMP APP1 segment)  |  MP4 video               |
+-----------------------------------+--------------------------+
0                                   Item:Length("MotionPhoto")
```

1. **XMP APP1 metadata.** The default (`honor`) flavour writes the official
   Android Motion Photo v1 scheme: `GCamera:MotionPhoto="1"`,
   `GCamera:MotionPhotoVersion="1"`, presentation-timestamp and video-trim
   fields, plus a `Container:Directory` whose `Primary` item has
   `Item:Length="0"` (it extends to the next item) and whose `MotionPhoto`
   item carries the exact MP4 byte length — the MP4 is the exact file tail.
   The `google` flavour writes both Google schemes instead, for maximum
   compatibility with older readers:
   - Legacy *MicroVideo*: `GCamera:MicroVideoVersion=1` and
     `GCamera:MicroVideoOffset`, the offset in bytes counted backwards from the
     end of the file to the start of the video.
   - *Container directory*: `Container:Directory` lists the `Primary` JPEG
     (`Item:Length` = size of the JPEG including the XMP segment itself) and
     the `MotionPhoto` MP4 (`Item:Length` = video size).
2. **The MP4 bytes** appended directly after the JPEG EOI (`FF D9`) marker.

The script inserts the XMP segment after any leading JFIF/EXIF segments (so
existing EXIF survives ahead of it), and if the input JPEG already contains a
motion-photo XMP segment or an old embedded video, they are replaced — you can
safely re-run the tool on its own output.

## Compatibility notes

- The default honor flavour plays in the **stock Honor gallery**: transfer the
  file as a file (USB / Drive / adb — apps that re-compress images will strip
  the trailer), let it appear in the gallery, and it animates. Verified on a
  real 2026 Honor phone; a Google-Drive round trip came back byte-identical.
- The google flavour is what Google Photos / the Android photo stack expect;
  playback works in Google Photos on Android.
- Best results when the video is H.264/AAC MP4 and matches the still's
  orientation; native motion clips are ~2–3 s (`--duration 2.5` mimics the
  Honor camera, whose clips measure ~2.4 s).
- **Samsung Gallery** uses its own additional `SEFT` trailer format, which this
  tool does not write; Samsung-targeted files may not animate there.
- JPEGs with an existing XMP packet of another kind get their packet replaced
  (only the motion-photo metadata is written back).

## Testing

```
python test_motion_photo.py
```

111 tests covering JPEG parsing (JFIF/EXIF ordering, progressive-safe marker
walking), the self-referential primary-length calculation across digit
boundaries, trailer replacement, error handling, the CLI, the MP4 box parser
(duration, audio, rotation-aware display size), the black-bar analyser,
ffmpeg command construction, AI-tag detection/removal (EXIF value blanking
with offset integrity, XMP element and attribute surgery, C2PA/COM/APP13
segment drops, big-endian and UTF-16 EXIF, idempotence, standalone clean and
motion-photo-integrated CLI runs),
and CLI behaviour via a fake-ffmpeg shim. End-to-end tests run against a
**real** ffmpeg (found via PATH or `imageio_ffmpeg`) when one is available and
auto-skip otherwise: they generate clips, process them with
`--compress --duration 3`, and verify the embedded trailers' duration, codecs,
size reduction, aspect-ratio cropping and stretching (default-on), automatic
black-bar removal on a padded clip, exact-pixel `--crop` trims, and that
matching clips are embedded byte-identical without transcoding.  The Apple
Live Photo pair is covered by structure tests (MakerApple EXIF, QuickTime
metadata atoms, stco chunk-offset patching for faststart and non-faststart
layouts) plus a real-ffmpeg test proving the surgically modified MOV still
decodes; the honor flavour is layout-checked and additionally validated against
a real Honor capture kept at C:/Users/wcbos/Downloads/IMG_20261004_144401.jpg
(auto-skipped when the sample is absent).
Fixtures are synthesised in-memory, so no media files are needed.
