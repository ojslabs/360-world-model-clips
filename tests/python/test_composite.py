"""Real FFmpeg timing, picture and audio checks for finished local highlights."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import composite
from app import crowd_audio
from app import media


class CompositeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.source = cls.root / "source.mp4"
        cls.orbit = cls.root / "orbit.mp4"
        cls.crowd = cls.root / "crowd.wav"
        cls.freeze = 540 / (30000 / 1001)
        cls._fixture([
            "-f", "lavfi", "-i", "color=red:size=320x180:rate=30000/1001,"
            "drawbox=color=blue:t=fill:enable='gte(t,18.018)'",
            "-f", "lavfi", "-i", "aevalsrc='if(lt(t,18.018),0.1,0.3)*sin(2*PI*440*t)':s=48000",
            "-t", "32", "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", cls.source,
        ])
        cls._fixture([
            "-f", "lavfi", "-i", "color=green:size=320x180:rate=30",
            "-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*1000*t)':s=48000",
            "-t", "6", "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", cls.orbit,
        ])
        cls._fixture([
            "-f", "lavfi", "-i", "aevalsrc='0.2*sin(2*PI*220*t)':s=48000",
            "-t", "1.5", "-ac", "2", "-c:a", "pcm_s16le", cls.crowd,
        ])

    @staticmethod
    def _fixture(args):
        media.run(["ffmpeg", "-v", "error", "-y", *args], timeout=60)

    @staticmethod
    def _rms(path, start):
        pcm = media.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", path,
                         "-t", "0.5", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
        samples = np.frombuffer(pcm, dtype="<f4")
        return float(np.sqrt(np.mean(samples ** 2)))

    @staticmethod
    def _pixel(path, start):
        raw = media.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", path,
                         "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24",
                         "-f", "rawvideo", "-"])
        return tuple(raw[:3])

    def test_full_highlight_has_exact_context_picture_order_and_source_audio(self):
        output = self.root / "finished1080.mp4"
        result = composite.assemble(self.source, self.orbit, self.freeze, output, auto_crowd=False)
        self.assertEqual(result["source_window"]["freeze_frame"], 540)
        self.assertEqual(result["source_window"]["source_fps"], "30000/1001")
        self.assertEqual(result["source_window"]["freeze_audio_sample"], round(self.freeze * 48000))
        self.assertAlmostEqual(result["source_window"]["start"], self.freeze - 4)
        self.assertAlmostEqual(result["source_window"]["end"], self.freeze + 10)
        self.assertEqual([(s["output_start"], s["output_end"]) for s in result["segments"]],
                         [(0, 4), (4, 10), (10, 20)])
        self.assertAlmostEqual(result["media"]["duration"], 20, places=2)
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1920, 1080))
        self.assertEqual(result["audio_provenance"]["orbit"], "silence_no_crowd_recording")
        self.assertFalse(result["audio_provenance"]["provider_audio_used"])
        self.assertIsNone(result["reference_closure"])
        self.assertTrue(result["media"]["decoded_audio"])
        for at, channel in ((3.9, 0), (4.1, 1), (9.9, 1), (10.1, 2), (19.8, 2)):
            pixel = self._pixel(output, at)
            self.assertEqual(pixel.index(max(pixel)), channel, (at, pixel))
        self.assertAlmostEqual(self._rms(output, 1), self._rms(self.source, self.freeze - 3), delta=.003)
        self.assertLess(self._rms(output, 9), .0001)
        self.assertAlmostEqual(self._rms(output, 19), self._rms(self.source, self.freeze + 9), delta=.003)

    def test_supplied_crowd_loops_during_orbit_and_fades_into_ten_second_tail(self):
        output = self.root / "with-crowd.mp4"
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            result = composite.assemble(self.source, self.orbit, self.freeze, output,
                                        tail_seconds=10, crowd_path=self.crowd)
            combined = composite.combine([output, output], self.root / "combined.mp4")
        self.assertAlmostEqual(result["media"]["duration"], 20, places=2)
        self.assertEqual(result["audio_provenance"]["orbit"], "supplied_crowd_recording")
        self.assertEqual(result["audio_provenance"]["join_fade_seconds"], .25)
        self.assertAlmostEqual(self._rms(output, 6), self._rms(self.crowd, .25), delta=.003)
        self.assertAlmostEqual(self._rms(output, 9), self._rms(self.crowd, .25), delta=.003)
        self.assertAlmostEqual(self._rms(output, 19), self._rms(self.source, self.freeze + 9), delta=.003)
        self.assertAlmostEqual(combined["media"]["duration"], 40, delta=.04)
        self.assertEqual(combined["clips"][1]["output_start"], 20)
        self.assertEqual(self._pixel(combined["path"], 20.2).index(max(self._pixel(combined["path"], 20.2))), 0)
        self.assertAlmostEqual(self._rms(combined["path"], 19), self._rms(output, 19), delta=.003)
        self.assertAlmostEqual(self._rms(combined["path"], 21), self._rms(output, 1), delta=.003)

    def test_anchored_orbit_keeps_identical_reference_pixels_in_the_finished_highlight(self):
        anchored = self.root / "anchored.mp4"
        self._fixture([
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30,trim=end_frame=1,"
            "loop=loop=179:size=1:start=0,setpts=N/30/TB,"
            "drawbox=color=green:t=fill:enable='between(n,1,178)'",
            "-frames:v", "180", "-an", "-c:v", "libx264", "-preset", "fast", "-qp", "18",
            "-g", "1", "-x264-params", "aq-mode=0:psy=0:mbtree=0:ipratio=1",
            "-pix_fmt", "yuv420p", anchored,
        ])
        output = self.root / "anchored-finished.mp4"
        result = composite.assemble(self.source, anchored, self.freeze, output, reference_anchored=True, auto_crowd=False)
        closure = result["reference_closure"]
        self.assertTrue(closure["first_last_pixel_hash_equal"])
        self.assertEqual((closure["first_frame"], closure["last_frame"]), (120, 299))
        self.assertEqual((closure["first_seconds"], closure["last_seconds"]), (4, 299 / 30))
        hashes = media.run(["ffmpeg", "-v", "error", "-i", output, "-an", "-vf",
                            "select=eq(n\\,120)+eq(n\\,210)+eq(n\\,299)", "-vsync", "0",
                            "-pix_fmt", "rgb24", "-f", "framemd5", "-"]).decode()
        frames = [line.rsplit(",", 1)[1].strip() for line in hashes.splitlines() if not line.startswith("#")]
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0], frames[2])
        self.assertNotEqual(frames[0], frames[1])
        self.assertAlmostEqual(result["media"]["duration"], 20, places=2)
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1920, 1080))
        self.assertEqual(self._pixel(output, 10.1).index(max(self._pixel(output, 10.1))), 2)
        self.assertAlmostEqual(self._rms(output, 19), self._rms(self.source, self.freeze + 9), delta=.003)

    def test_fractional_reel_preserves_every_picture_and_continuous_frame_clock(self):
        source = self.root / "fractional.mp4"
        # A source-tail cut can end between 30-fps output frames. Video has 539
        # frames while AAC/container durations carry their own rounding.
        self._fixture(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                       "-t", "17.96", "-c:v", "libx264", "-preset", "veryfast",
                       "-c:a", "aac", source])
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            result = composite.combine([source, source], self.root / "fractional-reel.mp4")
        _, video, _ = composite._probe(result["path"])
        self.assertEqual(video["avg_frame_rate"], "30/1")
        self.assertEqual(int(video["nb_frames"]), 1078)
        self.assertAlmostEqual(result["media"]["duration"], 1078 / 30, places=5)
        self.assertEqual(result["clips"][1]["output_start"], 539 / 30)

        def picture_rows(path):
            text = media.run(["ffmpeg", "-v", "error", "-i", path, "-an",
                              "-vsync", "0", "-f", "framemd5", "-"]).decode()
            return [line.split(",") for line in text.splitlines() if not line.startswith("#")]

        original = picture_rows(source)
        reel = picture_rows(result["path"])
        self.assertEqual(len(original), 539)
        self.assertEqual([row[-1].strip() for row in reel],
                         [row[-1].strip() for row in original] * 2)
        points = [int(row[2]) for row in reel]
        self.assertEqual([b - a for a, b in zip(points, points[1:])], [1] * 1077)
        for start in (539 / 30 - .25, 539 / 30 + .25):
            self.assertAlmostEqual(self._rms(result["path"], start), self._rms(source, 1), delta=.003)

    def test_anchor_flag_rejects_unmatched_reference_endpoints_without_replacing_them(self):
        unanchored = self.root / "not-anchored.mp4"
        self._fixture([
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "6", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", unanchored,
        ])
        original = unanchored.read_bytes()
        output = self.root / "unmatched-finished.mp4"
        with self.assertRaisesRegex(ValueError, "matching decoded reference endpoints"):
            composite.assemble(self.source, unanchored, self.freeze, output, reference_anchored=True)
        self.assertEqual(unanchored.read_bytes(), original)
        self.assertFalse(output.exists())

    def test_endpoint_verification_still_decodes_video_and_audio_after_the_orbit(self):
        valid = self.root / "verification-tail.mp4"
        self._fixture([
            "-f", "lavfi", "-i", "color=red:size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "19", "-c:v", "libx264", "-preset", "ultrafast", "-g", "1",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", valid,
        ])
        packets = json.loads(media.run(["ffprobe", "-v", "error", "-show_packets",
                                         "-of", "json", valid]))["packets"]
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            checked = composite._verify(valid, 19, (240, 419))
            self.assertEqual(checked["endpoint_rgb_hashes"][0], checked["endpoint_rgb_hashes"][1])
            self.assertTrue(checked["decoded_video"] and checked["decoded_audio"])
            for kind in ("video", "audio"):
                with self.subTest(track=kind):
                    packet = next(p for p in packets if p["codec_type"] == kind and float(p["pts_time"]) >= 18)
                    broken = self.root / f"broken-tail-{kind}.mp4"
                    body = bytearray(valid.read_bytes())
                    position, size = int(packet["pos"]), int(packet["size"])
                    body[position:position + size] = b"\xff" * size
                    broken.write_bytes(body)
                    with self.assertRaises(RuntimeError):
                        composite._verify(broken, 19, (240, 419))

    def test_default_crowd_processing_is_audible_and_keeps_source_audio_timing(self):
        source = self.root / "stereo-atmosphere.mp4"
        self._fixture([
            "-f", "lavfi", "-i", "color=red:size=320x180:rate=30", "-f", "lavfi", "-i",
            "aevalsrc='0.15*sin(2*PI*1000*t)+0.04*sin(2*PI*350*t)|"
            "0.15*sin(2*PI*1000*t)+0.04*sin(2*PI*550*t)':s=48000:c=stereo",
            "-t", "32", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", source,
        ])
        output = self.root / "automatic-crowd.mp4"
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)), \
             patch.object(crowd_audio, "_speech_seconds", return_value=[4, 0]):
            result = composite.assemble(source, self.orbit, 18, output)
        self.assertEqual(result["audio_provenance"]["orbit"], "source_crowd_centre_suppressed")
        self.assertEqual(result["audio_provenance"]["crowd_bed"]["bed_speech_seconds"], 0)
        self.assertEqual(result["audio_provenance"]["join_fade_seconds"], .25)
        self.assertAlmostEqual(result["media"]["duration"], 20, places=2)
        for start in (3.75, 4, 6, 9.75, 10):
            self.assertGreater(self._rms(output, start), .01)
        self.assertAlmostEqual(self._rms(output, 1), self._rms(source, 15), delta=.003)
        self.assertAlmostEqual(self._rms(output, 19), self._rms(source, 27), delta=.003)
        self.assertFalse(list(self.root.glob(".crowd-bed-*")))

    def test_default_does_not_replace_missing_stereo_atmosphere_with_silence(self):
        output = self.root / "no-atmosphere.mp4"
        with patch.object(crowd_audio, "FALLBACK_PATH", self.root / "missing-fallback.wav"), \
             self.assertRaisesRegex(ValueError, "no verified fallback"):
            composite.assemble(self.source, self.orbit, self.freeze, output)
        self.assertFalse(output.exists())

    def test_verified_fallback_is_labelled_with_its_actual_source(self):
        output = self.root / "fallback-atmosphere.mp4"
        provenance = {"reused_stadium_ambience": True, "source_video_id": "C9sL5j_iUiE",
                      "source_start": 519.405, "source_end": 525.905, "speech_check_passed": True}
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)), \
             patch.object(crowd_audio, "fallback_bed", return_value={"path": str(self.crowd), "provenance": provenance}):
            result = composite.assemble(self.source, self.orbit, self.freeze, output)
        self.assertEqual(result["audio_provenance"]["orbit"], "source_crowd_fallback")
        self.assertEqual(result["audio_provenance"]["crowd_bed"]["source_video_id"], "C9sL5j_iUiE")
        self.assertIn("too little", result["audio_provenance"]["crowd_bed"]["fallback_reason"])
        self.assertGreater(self._rms(output, 6), .01)

    def test_decoder_or_detector_failures_cannot_trigger_fallback(self):
        with patch.object(crowd_audio, "prepare_crowd_bed", side_effect=RuntimeError("decoder failed")), \
             patch.object(crowd_audio, "fallback_bed") as fallback:
            with self.assertRaisesRegex(RuntimeError, "decoder failed"):
                composite.assemble(self.source, self.orbit, self.freeze, self.root / "failed-source.mp4")
        fallback.assert_not_called()

    def test_next_frame_handoff_advances_video_and_audio_together(self):
        source = self.root / "frame-handoff.mkv"
        self._fixture([
            "-f", "lavfi", "-i", "color=red:size=320x180:rate=30,"
            "drawbox=color=blue:t=fill:enable='gte(n,541)'", "-f", "lavfi", "-i",
            "aevalsrc='if(lt(t,541/30),0.1,0.3)*sin(2*PI*440*t)':s=48000",
            "-t", "32", "-c:v", "ffv1", "-c:a", "pcm_s16le", source,
        ])
        legacy, next_frame = self.root / "repeat-frame.mp4", self.root / "next-frame.mp4"
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            old = composite.assemble(source, self.orbit, 18, legacy, auto_crowd=False)
            advanced = composite.assemble(source, self.orbit, 18, next_frame,
                                           auto_crowd=False, resume_next_frame=True)
        self.assertEqual(self._pixel(legacy, 10).index(max(self._pixel(legacy, 10))), 0)
        self.assertEqual(self._pixel(next_frame, 10).index(max(self._pixel(next_frame, 10))), 2)
        self.assertEqual(old["source_window"]["resume_frame"], 540)
        self.assertEqual(advanced["source_window"]["resume_frame"], 541)
        self.assertAlmostEqual(advanced["source_window"]["resume_time"], 541 / 30)
        self.assertEqual(advanced["source_window"]["resume_audio_sample"], round(541 / 30 * 48000))
        self.assertEqual(advanced["segments"][-1]["source_start"], 541 / 30)
        self.assertEqual(advanced["segments"][-1]["source_end"], 541 / 30 + 10)
        self.assertEqual(advanced["segments"][0]["source_start"], old["segments"][0]["source_start"])
        self.assertEqual(advanced["media"]["duration"], 20)
        def start_rms(path, start):
            raw = media.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", path,
                             "-t", "0.015", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
            values = np.frombuffer(raw, "<f4")
            return float(np.sqrt(np.mean(values ** 2)))
        self.assertGreater(start_rms(next_frame, 10.01), 2 * start_rms(legacy, 10.01))
        self.assertAlmostEqual(start_rms(next_frame, 10.01), start_rms(source, 541 / 30 + .01), delta=.025)

    def test_next_frame_handoff_clamps_the_tail_at_video_eof(self):
        source = self.root / "frame-handoff.mkv"
        if not source.exists():
            self._fixture(["-f", "lavfi", "-i", "color=red:size=320x180:rate=30", "-f", "lavfi", "-i",
                           "anullsrc=r=48000:cl=stereo", "-t", "32", "-c:v", "ffv1", "-c:a", "pcm_s16le", source])
        legacy = composite._source_window(source, 28, 10)
        self.assertEqual(legacy["end"], 32)
        advanced = composite._source_window(source, 28, 10, resume_next_frame=True)
        self.assertEqual(advanced["end"], 32)
        self.assertAlmostEqual(advanced["actual_tail_seconds"], 4 - 1 / 30)
        self.assertEqual(advanced["requested_tail_seconds"], 10)
        last = composite._source_window(source, 959 / 30, 10, resume_next_frame=True)
        self.assertEqual(last["actual_tail_seconds"], 0)

    def test_missing_source_context_rejected_before_creating_output(self):
        for time, tail in ((3, 10), (33, 10)):
            output = self.root / f"invalid-{time}.mp4"
            with self.assertRaisesRegex(ValueError, "full 4 seconds"):
                composite.assemble(self.source, self.orbit, time, output, tail_seconds=tail)
            self.assertFalse(output.exists())

    def test_silent_source_last_frame_has_no_tail_and_penultimate_frame_resumes_once(self):
        source = self.root / "silent-end.mp4"
        self._fixture(["-f", "lavfi", "-i", "color=red:size=320x180:rate=30,"
                       "drawbox=color=blue:t=fill:enable='gte(n,239)'", "-t", "8", "-an",
                       "-c:v", "libx264", "-preset", "ultrafast", source])
        source_details = media.probe(source)
        self.assertIsNone(source_details["audio_codec"])
        preview = media.browser_preview(source, self.root, source_details)
        self.assertEqual(preview["kind"], "original")
        media.verify_source_decode(source, source_details)
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            last = composite.assemble(source, self.orbit, 239 / 30, self.root / "last-frame.mp4",
                                      crowd_path=self.crowd, resume_next_frame=True)
            previous = composite.assemble(source, self.orbit, 238 / 30, self.root / "one-tail-frame.mp4",
                                          crowd_path=self.crowd, resume_next_frame=True)
        self.assertEqual(last["tail_seconds"], 10)
        self.assertEqual(last["actual_tail_seconds"], 0)
        self.assertEqual(last["media"]["duration"], 10)
        self.assertEqual([part["kind"] for part in last["segments"]], ["source_lead", "camera_orbit"])
        self.assertEqual(last["audio_provenance"]["lead"], "silence_no_source_audio")
        self.assertEqual(last["audio_provenance"]["tail"], "no_source_tail")
        self.assertLess(self._rms(last["path"], 1), .0001)
        self.assertGreater(self._rms(last["path"], 6), .05)
        self.assertEqual(self._pixel(last["path"], 299 / 30).index(max(self._pixel(last["path"], 299 / 30))), 1)
        self.assertAlmostEqual(previous["actual_tail_seconds"], 1 / 30)
        self.assertAlmostEqual(previous["media"]["duration"], 301 / 30, places=5)
        self.assertEqual(previous["segments"][-1]["source_start"], 239 / 30)
        self.assertEqual(self._pixel(previous["path"], 10).index(max(self._pixel(previous["path"], 10))), 2)

    def test_short_audio_is_padded_without_shortening_source_video(self):
        source = self.root / "short-audio.mp4"
        self._fixture(["-f", "lavfi", "-i", "color=red:size=320x180:rate=30:duration=20",
                       "-f", "lavfi", "-i", "aevalsrc='0.1*sin(2*PI*440*t)':s=48000:d=9",
                       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", source])
        self.assertEqual(media.probe(source)["duration"], 20)
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            result = composite.assemble(source, self.orbit, 6, self.root / "padded-audio.mp4",
                                        crowd_path=self.crowd, resume_next_frame=True)
        self.assertEqual(result["actual_tail_seconds"], 10)
        self.assertEqual(result["media"]["duration"], 20)
        self.assertEqual(result["audio_provenance"]["tail"], "original_source_padded_at_audio_eof")
        self.assertAlmostEqual(self._rms(result["path"], 1), self._rms(source, 3), delta=.003)
        self.assertAlmostEqual(self._rms(result["path"], 11), self._rms(source, 7 + 1 / 30), delta=.003)
        self.assertLess(self._rms(result["path"], 15), .0001)

    def test_invalid_duration_or_freeze_does_not_render(self):
        for freeze, tail in ((float("nan"), 10), (float("inf"), 10), (self.freeze, 2), (self.freeze, True)):
            with self.assertRaises(ValueError):
                composite.assemble(self.source, self.orbit, freeze, self.root / "invalid.mp4", tail)

    def test_rejects_wrong_orbit_duration_and_missing_crowd_audio(self):
        with self.assertRaisesRegex(ValueError, "approximately six seconds"):
            composite.assemble(self.source, self.source, self.freeze, self.root / "long-orbit.mp4")
        with self.assertRaisesRegex(ValueError, "missing"):
            composite.assemble(self.source, self.orbit, self.freeze, self.root / "missing-crowd.mp4",
                               crowd_path=self.root / "missing.wav")

    def test_existing_output_is_never_overwritten(self):
        target = self.root / "immutable.mp4"
        target.write_bytes(b"Existing completed output")
        with self.assertRaises(FileExistsError):
            composite.assemble(self.source, self.orbit, self.freeze, target)
        self.assertEqual(target.read_bytes(), b"Existing completed output")


if __name__ == "__main__":
    unittest.main()
