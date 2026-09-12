"""Offline endpoint equality, retained interior and input preservation checks."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import media
import orbit_loop
import composite


class OrbitLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.orbit = cls.root / "generated.mp4"
        cls.reference = cls.root / "reference.png"
        Image.new("RGB", (1920, 1080), (240, 30, 20)).save(cls.reference)
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=blue:size=1280x720:rate=30,drawbox=x=500:y=300:w=200:h=100:color=lime:t=fill",
                   "-t", "6", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                   "-pix_fmt", "yuv420p", cls.orbit], timeout=60)

    def test_same_reference_endpoints_and_generated_interior_remain(self):
        output = self.root / "anchored.mp4"
        before_orbit = hashlib.sha256(self.orbit.read_bytes()).hexdigest()
        before_image = hashlib.sha256(self.reference.read_bytes()).hexdigest()
        result = orbit_loop.anchor_reference(self.orbit, self.reference, output)
        first = orbit_loop._pixels(output, 0)
        last = orbit_loop._pixels(output, 179)
        interior = orbit_loop._pixels(output, 90)
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(last).hexdigest())
        self.assertNotEqual(first, interior)
        self.assertGreater(first[0], 200)
        self.assertGreater(interior[2], 200)
        self.assertEqual(result["media"]["duration"], 6)
        self.assertEqual(result["media"]["frames"], 180)
        self.assertTrue(result["reference_anchored"])
        self.assertFalse(result["generated_motion_verified"])
        self.assertEqual(result["audio"], "none")
        self.assertAlmostEqual(result["blends"]["start"]["end"], .15)
        self.assertAlmostEqual(result["blends"]["end"]["start"], 5.65)
        self.assertAlmostEqual(result["blends"]["reference_hold"]["start"], 5.85)
        self.assertEqual(result["source_orbit_sha256"], before_orbit)
        self.assertEqual(hashlib.sha256(self.orbit.read_bytes()).hexdigest(), before_orbit)
        self.assertEqual(hashlib.sha256(self.reference.read_bytes()).hexdigest(), before_image)

    def test_existing_output_and_input_paths_cannot_be_overwritten(self):
        output = self.root / "existing.mp4"
        output.write_bytes(b"existing export")
        for target in (output, self.orbit, self.reference):
            with self.subTest(target=target.name), self.assertRaisesRegex(ValueError, "new MP4"):
                orbit_loop.anchor_reference(self.orbit, self.reference, target)
        self.assertEqual(output.read_bytes(), b"existing export")

    def test_cut_keeps_generated_pixels_up_to_one_reference_endpoint_frame(self):
        output = self.root / "cut.mp4"
        result = orbit_loop.anchor_reference(self.orbit, self.reference, output, transition="cut")
        first = orbit_loop._pixels(output, 0)
        last = orbit_loop._pixels(output, 179)
        self.assertEqual(first, last)
        for frame in (1, 174, 178):
            pixels = orbit_loop._pixels(output, frame)
            self.assertGreater(pixels[2], 200, frame)
            self.assertLess(pixels[0], 25, frame)
        self.assertIsNone(result["blends"])
        self.assertEqual(result["added_hold_seconds"], 0)
        self.assertEqual(result["transition"], "cut")
        self.assertEqual(result["media"]["frames"], 180)

    def test_wrong_reference_aspect_is_rejected_without_output(self):
        square = self.root / "square.png"
        Image.new("RGB", (100, 100)).save(square)
        output = self.root / "bad-reference.mp4"
        with self.assertRaisesRegex(ValueError, "16:9 PNG"):
            orbit_loop.anchor_reference(self.orbit, square, output)
        self.assertFalse(output.exists())

    def test_1080p_cut_keeps_full_resolution_with_identical_endpoints(self):
        orbit = self.root / "generated-1080p.mp4"
        output = self.root / "cut-1080p.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=blue:size=1920x1080:rate=30,drawbox=x=751:y=451:w=3:h=200:color=lime:t=fill",
                   "-t", "6", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                   "-pix_fmt", "yuv420p", orbit], timeout=60)
        original = hashlib.sha256(orbit.read_bytes()).hexdigest()
        result = orbit_loop.anchor_reference(orbit, self.reference, output, transition="cut")
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1920, 1080))
        self.assertEqual(result["reference_render"]["height"], 1080)
        first, middle, last = orbit_loop._verified_samples(output, 180)
        self.assertEqual(len(first), 1920 * 1080 * 3)
        self.assertEqual(first, last)
        self.assertNotEqual(first, middle)
        self.assertEqual(result["media"]["frames"], 180)
        self.assertEqual(result["added_hold_seconds"], 0)
        self.assertEqual(hashlib.sha256(orbit.read_bytes()).hexdigest(), original)

    def test_combined_decode_requires_the_actual_final_frame(self):
        truncated = self.root / "missing-final-frame.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-i", self.orbit, "-map", "0:v:0",
                   "-frames:v", "179", "-c:v", "copy", truncated], timeout=30)
        with self.assertRaisesRegex(RuntimeError, "all required reference frames"):
            orbit_loop._verified_samples(truncated, 180)

    def test_4k_reference_and_highlight_keep_native_detail_and_matching_endpoints(self):
        reference = self.root / "reference-4k.png"
        # Narrow high-contrast lines expose an accidental intermediate 1080p image.
        image = Image.new("RGB", (3840, 2160), "white")
        for x in (1200, 1204, 1208):
            for y in range(700, 1400):
                image.putpixel((x, y), (0, 0, 0))
        image.save(reference)
        orbit = self.root / "native-1080p.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=blue:size=1920x1080:rate=30", "-t", "6", "-an",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", orbit])
        anchored = self.root / "anchor-4k.mp4"
        result = orbit_loop.anchor_reference(orbit, reference, anchored, transition="cut", output_size=(3840, 2160))
        first, middle, last = orbit_loop._verified_samples(anchored, 180)
        self.assertEqual(first, last)
        self.assertNotEqual(first, middle)
        self.assertEqual(len(first), 3840 * 2160 * 3)
        sample = lambda x: first[(900 * 3840 + x) * 3]
        self.assertLess(sample(1200), 35)
        self.assertGreater(sample(1201), 210)
        self.assertEqual(result["resolution_provenance"], {
            "output_size": [3840, 2160], "generated_size": [1920, 1080],
            "reference_size": [3840, 2160], "generated_upscaled": True, "reference_upscaled": False})
        source = self.root / "source-4k.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=red:size=3840x2160:rate=25", "-f", "lavfi", "-i",
                   "sine=frequency=440:sample_rate=48000", "-t", "18", "-c:v", "libx264",
                   "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", source], timeout=90)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        finished = composite.assemble(source, anchored, 10, self.root / "finished-4k.mp4",
                                      reference_anchored=True, auto_crowd=False,
                                      resume_next_frame=True, output_size=(3840, 2160))
        self.assertEqual((finished["media"]["width"], finished["media"]["height"]), (3840, 2160))
        self.assertEqual(finished["media"]["duration"], 18)
        self.assertEqual((finished["reference_closure"]["first_frame"],
                          finished["reference_closure"]["last_frame"]), (240, 419))
        self.assertTrue(finished["reference_closure"]["first_last_pixel_hash_equal"])
        self.assertTrue(finished["media"]["decoded_video"] and finished["media"]["decoded_audio"])
        self.assertEqual(finished["source_window"]["resume_time"], 10.04)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
        reel = composite.combine([finished["path"], finished["path"]], self.root / "reel-4k.mp4")
        self.assertEqual(reel["media"]["duration"], 36)
        self.assertEqual((reel["media"]["width"], reel["media"]["height"]), (3840, 2160))
        # Same codecs and clocks cannot make unlike resolutions safe to stream-copy.
        small = self.root / "finished-1080p.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-i", source, "-vf", "scale=1920:1080,fps=30",
                   "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", small])
        with self.assertRaisesRegex(ValueError, "same resolution"):
            composite.combine([small, finished["path"]], self.root / "mixed-reel.mp4")


if __name__ == "__main__":
    unittest.main()
