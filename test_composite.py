"""Real FFmpeg timing, picture and audio checks for finished local highlights."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import composite
import crowd_audio
import media


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
        self.assertAlmostEqual(result["source_window"]["start"], self.freeze - 8)
        self.assertAlmostEqual(result["source_window"]["end"], self.freeze + 4)
        self.assertEqual([(s["output_start"], s["output_end"]) for s in result["segments"]],
                         [(0, 8), (8, 14), (14, 18)])
        self.assertAlmostEqual(result["media"]["duration"], 18, places=2)
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1920, 1080))
        self.assertEqual(result["audio_provenance"]["orbit"], "silence_no_crowd_recording")
        self.assertFalse(result["audio_provenance"]["provider_audio_used"])
        self.assertIsNone(result["reference_closure"])
        self.assertTrue(result["media"]["decoded_audio"])
        for at, channel in ((7.9, 0), (8.1, 1), (13.9, 1), (14.1, 2), (17.8, 2)):
            pixel = self._pixel(output, at)
            self.assertEqual(pixel.index(max(pixel)), channel, (at, pixel))
        self.assertAlmostEqual(self._rms(output, 1), self._rms(self.source, self.freeze - 7), delta=.003)
        self.assertLess(self._rms(output, 9), .0001)
        self.assertAlmostEqual(self._rms(output, 17), self._rms(self.source, self.freeze + 3), delta=.003)

    def test_supplied_crowd_loops_during_orbit_and_fades_into_four_second_tail(self):
        output = self.root / "with-crowd.mp4"
        with patch.object(composite, "OUTPUT_SIZE", (320, 180)):
            result = composite.assemble(self.source, self.orbit, self.freeze, output,
                                        tail_seconds=4, crowd_path=self.crowd)
            combined = composite.combine([output, output], self.root / "combined.mp4")
        self.assertAlmostEqual(result["media"]["duration"], 18, places=2)
        self.assertEqual(result["audio_provenance"]["orbit"], "supplied_crowd_recording")
        self.assertEqual(result["audio_provenance"]["join_fade_seconds"], .25)
        self.assertAlmostEqual(self._rms(output, 10), self._rms(self.crowd, .25), delta=.003)
        self.assertAlmostEqual(self._rms(output, 9), self._rms(self.crowd, .25), delta=.003)
        self.assertAlmostEqual(self._rms(output, 17), self._rms(self.source, self.freeze + 3), delta=.003)
        self.assertAlmostEqual(combined["media"]["duration"], 36, delta=.04)
        self.assertEqual(combined["clips"][1]["output_start"], 18)
        self.assertEqual(self._pixel(combined["path"], 18.2).index(max(self._pixel(combined["path"], 18.2))), 0)
        self.assertAlmostEqual(self._rms(combined["path"], 17), self._rms(output, 17), delta=.003)
        self.assertAlmostEqual(self._rms(combined["path"], 19), self._rms(output, 1), delta=.003)

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
        self.assertEqual((closure["first_frame"], closure["last_frame"]), (240, 419))
        self.assertEqual((closure["first_seconds"], closure["last_seconds"]), (8, 419 / 30))
        hashes = media.run(["ffmpeg", "-v", "error", "-i", output, "-an", "-vf",
                            "select=eq(n\\,240)+eq(n\\,330)+eq(n\\,419)", "-vsync", "0",
                            "-pix_fmt", "rgb24", "-f", "framemd5", "-"]).decode()
        frames = [line.rsplit(",", 1)[1].strip() for line in hashes.splitlines() if not line.startswith("#")]
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0], frames[2])
        self.assertNotEqual(frames[0], frames[1])
        self.assertAlmostEqual(result["media"]["duration"], 18, places=2)
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1920, 1080))
        self.assertEqual(self._pixel(output, 14.1).index(max(self._pixel(output, 14.1))), 2)
        self.assertAlmostEqual(self._rms(output, 17), self._rms(self.source, self.freeze + 3), delta=.003)

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
                    execute = media.subprocess.run
                    calls = []
                    def capture(*args, **kwargs):
                        result = execute(*args, **kwargs)
                        calls.append((args[0], result))
                        return result
                    with patch.object(media.subprocess, "run", side_effect=capture):
                        try:
                            composite._verify(broken, 19, (240, 419))
                        except RuntimeError:
                            continue
                    # A failing platform reports the exact multi-output command's
                    # final frame/time and decoder log, distinguishing early EOF
                    # from a decoder that silently accepts the damaged packet.
                    command, accepted = calls[-1]
                    diagnostic = list(command)
                    diagnostic[diagnostic.index("-v") + 1] = "info"
                    diagnostic[1:1] = ["-progress", "pipe:2"]
                    report = execute(diagnostic, capture_output=True, timeout=60)
                    self.fail(f"Accepted corrupt {kind} packet at {packet['pts_time']}: "
                              f"exit={accepted.returncode}, stderr={accepted.stderr!r}. "
                              f"Diagnostic exit={report.returncode}:\n"
                              + report.stderr.decode(errors="replace")[-6000:])

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
        self.assertAlmostEqual(result["media"]["duration"], 18, places=2)
        for start in (7.75, 8, 10, 13.75, 14):
            self.assertGreater(self._rms(output, start), .01)
        self.assertAlmostEqual(self._rms(output, 1), self._rms(source, 11), delta=.003)
        self.assertAlmostEqual(self._rms(output, 17), self._rms(source, 21), delta=.003)
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
        self.assertGreater(self._rms(output, 10), .01)

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
        self.assertEqual(self._pixel(legacy, 14).index(max(self._pixel(legacy, 14))), 0)
        self.assertEqual(self._pixel(next_frame, 14).index(max(self._pixel(next_frame, 14))), 2)
        self.assertEqual(old["source_window"]["resume_frame"], 540)
        self.assertEqual(advanced["source_window"]["resume_frame"], 541)
        self.assertAlmostEqual(advanced["source_window"]["resume_time"], 541 / 30)
        self.assertEqual(advanced["source_window"]["resume_audio_sample"], round(541 / 30 * 48000))
        self.assertEqual(advanced["segments"][-1]["source_start"], 541 / 30)
        self.assertEqual(advanced["segments"][-1]["source_end"], 541 / 30 + 4)
        self.assertEqual(advanced["segments"][0]["source_start"], old["segments"][0]["source_start"])
        self.assertEqual(advanced["media"]["duration"], 18)
        def start_rms(path, start):
            raw = media.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", path,
                             "-t", "0.015", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
            values = np.frombuffer(raw, "<f4")
            return float(np.sqrt(np.mean(values ** 2)))
        self.assertGreater(start_rms(next_frame, 14.01), 2 * start_rms(legacy, 14.01))
        self.assertAlmostEqual(start_rms(next_frame, 14.01), start_rms(source, 541 / 30 + .01), delta=.025)

    def test_next_frame_handoff_requires_the_full_four_second_tail(self):
        source = self.root / "frame-handoff.mkv"
        if not source.exists():
            self._fixture(["-f", "lavfi", "-i", "color=red:size=320x180:rate=30", "-f", "lavfi", "-i",
                           "anullsrc=r=48000:cl=stereo", "-t", "32", "-c:v", "ffv1", "-c:a", "pcm_s16le", source])
        legacy = composite._source_window(source, 28, 4)
        self.assertEqual(legacy["end"], 32)
        with self.assertRaisesRegex(ValueError, "4 seconds after"):
            composite._source_window(source, 28, 4, resume_next_frame=True)

    def test_missing_source_context_rejected_before_creating_output(self):
        for time, tail in ((4, 4), (29, 4), (31, 4)):
            output = self.root / f"invalid-{time}.mp4"
            with self.assertRaisesRegex(ValueError, "full 8 seconds"):
                composite.assemble(self.source, self.orbit, time, output, tail_seconds=tail)
            self.assertFalse(output.exists())

    def test_invalid_duration_or_freeze_does_not_render(self):
        for freeze, tail in ((float("nan"), 4), (float("inf"), 4), (self.freeze, 2), (self.freeze, True)):
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
