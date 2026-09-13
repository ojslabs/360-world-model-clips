"""Real short audio extraction, lowered pitch, smooth repeats and immutable cache."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from scipy.io import wavfile

from app import source_atmosphere as atmosphere


class SourceAtmosphereTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.wav"
        clock = np.arange(8 * 48000) / 48000
        # Only the requested 3.0–4.75s window contains the 887Hz signal.
        frequency = np.where((clock >= 3) & (clock < 4.75), 887, 220)
        values = .5 * np.sin(2 * np.pi * frequency * clock)
        wavfile.write(self.source, 48000, np.rint(np.column_stack((values, values)) * 32767).astype(np.int16))

    def test_real_audio_window_slows_pitch_and_loops_to_exact_length_without_boost(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        rate, raw = wavfile.read(result["path"])
        values = raw.astype(np.float64) / 32768
        provenance = result["provenance"]
        self.assertEqual((rate, raw.shape), (48000, (312000, 2)))
        self.assertEqual((provenance["source_start"], provenance["source_end"]), (3, 4.75))
        self.assertEqual(provenance["speed_envelope"], {
            "curve": "raised_cosine", "start": 1, "minimum": .75, "end": 1,
            "ramp_seconds": .75, "hold_start": .75, "hold_end": 5.75})
        self.assertEqual(provenance["gain"], 1)
        self.assertFalse(provenance["reused_stadium_ambience"] or provenance["speech_separated"])
        self.assertEqual(provenance["method"], "source_slow_motion_loop_v3")
        def frequency(start, duration=.1):
            segment = values[round(start * rate):round((start + duration) * rate), 0]
            crossings = np.flatnonzero((segment[:-1] <= 0) & (segment[1:] > 0))
            interpolated = crossings - segment[crossings] / (segment[crossings + 1] - segment[crossings])
            return (len(interpolated) - 1) * rate / (interpolated[-1] - interpolated[0])
        entry, middle, ending = frequency(.025), frequency(3.2), frequency(6.375)
        self.assertAlmostEqual(middle, 887 * .75, delta=1)
        self.assertAlmostEqual(entry, 887, delta=12)
        self.assertAlmostEqual(ending, 887, delta=12)
        self.assertGreater(entry, middle * 1.3)
        self.assertGreater(ending, middle * 1.3)
        for join in provenance["loop_joins"]:
            left = round((join - .01) * rate)
            right = round((join + provenance["loop_crossfade_source_seconds"] / .75 + .01) * rate)
            self.assertLess(float(np.max(np.abs(np.diff(values[left:right, 0])))), .12)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual(hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest(), provenance["bed_sha256"])

    def test_cache_reuses_verified_audio_but_freeze_duration_and_source_changes_get_new_files(self):
        first = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        saved = Path(first["path"]).read_bytes()
        with patch.object(atmosphere.media, "run", side_effect=AssertionError("Cached loop must not extract again")):
            self.assertEqual(atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds"), first)
        other_frame = atmosphere.prepare_slow_source_bed(self.source, 5, self.root / "beds")
        other_duration = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds", seconds=7)
        stamp = self.source.stat()
        os.utime(self.source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
        changed_source = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        self.assertEqual(len({r["path"] for r in (first, other_frame, other_duration, changed_source)}), 4)
        self.assertEqual(Path(first["path"]).read_bytes(), saved)

    def test_loud_audio_is_attenuated_and_invalid_source_windows_fail(self):
        rate, raw = wavfile.read(self.source)
        wavfile.write(self.source, rate, (raw.astype(np.int32) * 1.99).astype(np.int16))
        loud = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "loud")
        self.assertLess(loud["provenance"]["gain"], 1)
        self.assertLessEqual(loud["provenance"]["bed_peak"], .9001)
        for freeze in (1, 8.1, float("nan")):
            with self.subTest(freeze=freeze), self.assertRaises(ValueError):
                atmosphere.prepare_slow_source_bed(self.source, freeze, self.root / "invalid")

    def test_changed_cache_is_not_overwritten_or_silently_reused(self):
        result = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        target = Path(result["path"])
        target.write_bytes(b"changed saved audio")
        with self.assertRaisesRegex(ValueError, "loop changed"):
            atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        self.assertEqual(target.read_bytes(), b"changed saved audio")

    def video(self, name, audio_seconds=None, audio_codec="pcm_s16le", audio_offset=0):
        path = self.root / name
        command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=64x36:r=2:d=8"]
        if audio_seconds is not None:
            command += ["-itsoffset", str(audio_offset), "-f", "lavfi", "-i", f"sine=frequency=887:sample_rate=48000:duration={audio_seconds}"]
        command += ["-map", "0:v:0"]
        if audio_seconds is not None:
            command += ["-map", "1:a:0", "-c:a", audio_codec]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path]
        atmosphere.media.run(command)
        return path

    def test_audible_mono_is_kept_as_a_whole_mix_in_both_output_channels(self):
        rate, stereo = wavfile.read(self.source)
        mono = self.root / "mono.wav"
        wavfile.write(mono, rate, stereo[:, 0])
        result = atmosphere.prepare_slow_source_bed(mono, 4.75, self.root / "mono-beds")
        _, output = wavfile.read(result["path"])
        self.assertTrue(np.array_equal(output[:, 0], output[:, 1]))
        self.assertGreater(result["provenance"]["bed_rms"], .1)
        self.assertEqual(result["provenance"]["source_audio_channels"], 1)
        self.assertEqual(result["provenance"]["source_window_state"], "audible")
        self.assertFalse(result["provenance"]["speech_separated"])

    def test_silent_stereo_and_mono_remain_silent_with_truthful_provenance(self):
        for channels in (1, 2):
            source = self.root / f"silent-{channels}.wav"
            shape = (8 * 48000,) if channels == 1 else (8 * 48000, channels)
            wavfile.write(source, 48000, np.zeros(shape, dtype=np.int16))
            result = atmosphere.prepare_slow_source_bed(source, 4.75, self.root / "silence")
            rate, values = wavfile.read(result["path"])
            self.assertEqual(values.shape, (312000, 2))
            self.assertFalse(np.any(values))
            provenance = result["provenance"]
            self.assertTrue(provenance["source_audio_present"])
            self.assertEqual(provenance["source_audio_channels"], channels)
            self.assertEqual(provenance["source_window_state"], "silent")
            self.assertEqual(provenance["silence_reason"], "silent_source_window")
            self.assertEqual(provenance["padded_source_seconds"], 0)
            self.assertEqual(provenance["bed_rms"], 0)
            self.assertEqual(provenance["gain"], 1)

    def test_known_missing_audio_track_produces_silence_without_attempting_audio_decode(self):
        source = self.video("no-audio.mp4")
        original = source.read_bytes()
        with patch.object(atmosphere, "_strict_media", wraps=atmosphere._strict_media) as command:
            result = atmosphere.prepare_slow_source_bed(source, 4.75, self.root / "no-audio")
        self.assertEqual(len(command.call_args_list), 1)
        self.assertEqual(command.call_args.args[0][0], "ffprobe")
        rate, values = wavfile.read(result["path"])
        self.assertEqual((rate, values.shape), (48000, (312000, 2)))
        self.assertFalse(np.any(values))
        self.assertFalse(result["provenance"]["source_audio_present"])
        self.assertEqual(result["provenance"]["source_window_state"], "no_audio_stream")
        self.assertEqual(result["provenance"]["silence_reason"], "no_audio_stream")
        self.assertEqual(result["provenance"]["padded_source_seconds"], 1.75)
        self.assertEqual(source.read_bytes(), original)

    def test_audio_shorter_than_video_pads_clean_eof_and_distinguishes_ended_audio(self):
        source = self.video("short-audio.mkv", audio_seconds=3.5)
        partial = atmosphere.prepare_slow_source_bed(source, 4, self.root / "short-audio")
        provenance = partial["provenance"]
        self.assertEqual(provenance["source_window_state"], "partial_audio_eof")
        self.assertAlmostEqual(provenance["extracted_source_seconds"], 1.25, delta=.001)
        self.assertAlmostEqual(provenance["padded_source_seconds"], .5, delta=.001)
        self.assertGreater(provenance["bed_rms"], .01)
        self.assertTrue(provenance["source_audio_present"])
        ended = atmosphere.prepare_slow_source_bed(source, 7, self.root / "short-audio")
        self.assertEqual(ended["provenance"]["source_window_state"], "audio_ended")
        self.assertEqual(ended["provenance"]["silence_reason"], "audio_ended")
        self.assertEqual(ended["provenance"]["padded_source_seconds"], 1.75)
        self.assertFalse(np.any(wavfile.read(ended["path"])[1]))

    def test_future_audio_start_preserves_silence_before_the_track_begins(self):
        source = self.video("delayed-audio.mkv", audio_seconds=2, audio_offset=5)
        result = atmosphere.prepare_slow_source_bed(source, 4.75, self.root / "delayed-beds")
        self.assertTrue(result["provenance"]["source_audio_present"])
        self.assertEqual(result["provenance"]["source_window_state"], "silent")
        self.assertFalse(np.any(wavfile.read(result["path"])[1]))

    def test_offset_audio_uses_its_true_ending_timestamp_without_shifting_the_window(self):
        source = self.video("offset-audio.mkv", audio_seconds=3, audio_offset=2)
        result = atmosphere.prepare_slow_source_bed(source, 6, self.root / "offset-beds")
        provenance = result["provenance"]
        self.assertEqual(provenance["source_window_state"], "partial_audio_eof")
        self.assertAlmostEqual(provenance["extracted_source_seconds"], .75, delta=.001)
        self.assertAlmostEqual(provenance["padded_source_seconds"], 1, delta=.001)

    def test_stereo_whole_mix_preserves_channels_harmonics_and_noise_without_separation(self):
        rate = 48000
        clock = np.arange(8 * rate) / rate
        rng = np.random.default_rng(42)
        # Independent musical tones, a voiced harmonic envelope and broadband noise.
        voice = (.05 + .03 * np.sin(2 * np.pi * 3 * clock)) * np.sin(2 * np.pi * 200 * clock)
        left = .2 * np.sin(2 * np.pi * 440 * clock) + voice + .003 * rng.normal(size=len(clock))
        right = .2 * np.sin(2 * np.pi * 680 * clock) + voice + .003 * rng.normal(size=len(clock))
        source = self.root / "mixed-audio.wav"
        wavfile.write(source, rate, np.rint(np.column_stack((left, right)) * 32767).astype(np.int16))
        result = atmosphere.prepare_slow_source_bed(source, 4.75, self.root / "mixed")
        _, raw = wavfile.read(result["path"])
        values = raw.astype(np.float64) / 32768
        segment = values[2 * rate:4 * rate]
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))[:, None], axis=0))
        frequencies = np.fft.rfftfreq(len(segment), 1 / rate)
        self.assertAlmostEqual(frequencies[np.argmax(spectrum[:, 0])], 440 * .75, delta=1)
        self.assertAlmostEqual(frequencies[np.argmax(spectrum[:, 1])], 680 * .75, delta=1)
        self.assertGreater(spectrum[round(200 * .75 * 2)].min(), 100)
        self.assertFalse(np.array_equal(raw[:, 0], raw[:, 1]))
        self.assertFalse(result["provenance"]["speech_separated"])
        self.assertFalse(result["provenance"]["reused_stadium_ambience"])
        self.assertEqual(result["provenance"]["classification"], "whole_source_mix")
        self.assertEqual(result["provenance"]["gain"], 1)

    def test_invalid_input_and_decoder_errors_do_not_become_silent_success(self):
        corrupted = self.root / "corrupt.mp4"
        corrupted.write_bytes(b"not a media recording")
        with self.assertRaisesRegex(ValueError, "decode"):
            atmosphere.prepare_slow_source_bed(corrupted, 4.75, self.root / "corrupt")
        for result in (Mock(returncode=1, stdout=b"", stderr=b"decode failed"),
                       Mock(returncode=0, stdout=b"", stderr=b"logged decoder error")):
            with patch.object(atmosphere.subprocess, "run", return_value=result), self.assertRaisesRegex(ValueError, "decode"):
                atmosphere._strict_media(["ffmpeg", "-v", "error", "-i", self.source])
        self.assertFalse(list((self.root / "corrupt").glob("*.wav")))

    def test_unexpected_short_or_invalid_pcm_is_rejected_instead_of_padded(self):
        metadata = json.dumps({"streams": [{"codec_type": "audio", "duration": "8", "channels": 2}],
                               "format": {"duration": "8"}}).encode()
        for raw in (np.zeros((100, 2), dtype="<f4").tobytes(), b"bad",
                    np.full((84000, 2), np.nan, dtype="<f4").tobytes()):
            with patch.object(atmosphere, "_strict_media", side_effect=[metadata, raw]), self.assertRaises(ValueError):
                atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "bad-pcm")

    def test_v3_policy_keeps_legacy_cached_files_untouched(self):
        legacy = self.root / "cache/source-audio-v2.wav"
        legacy.parent.mkdir()
        legacy.write_bytes(b"old saved audio is preserved")
        atmosphere.prepare_slow_source_bed(self.source, 4.75, legacy.parent)
        self.assertEqual(legacy.read_bytes(), b"old saved audio is preserved")


if __name__ == "__main__":
    unittest.main()
