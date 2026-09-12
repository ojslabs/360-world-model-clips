"""Real short audio extraction, lowered pitch, smooth repeats and immutable cache."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile

import source_atmosphere as atmosphere


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
        self.assertEqual(provenance["method"], "source_slow_motion_loop_v2")
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

    def test_loud_audio_is_attenuated_and_silent_or_incomplete_windows_fail(self):
        rate, raw = wavfile.read(self.source)
        wavfile.write(self.source, rate, (raw.astype(np.int32) * 1.99).astype(np.int16))
        loud = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "loud")
        self.assertLess(loud["provenance"]["gain"], 1)
        self.assertLessEqual(loud["provenance"]["bed_peak"], .9001)
        for freeze in (1, 8.1, float("nan")):
            with self.subTest(freeze=freeze), self.assertRaises(ValueError):
                atmosphere.prepare_slow_source_bed(self.source, freeze, self.root / "invalid")
        silent = self.root / "silent.wav"
        wavfile.write(silent, 48000, np.zeros((3 * 48000, 2), dtype=np.int16))
        with self.assertRaisesRegex(ValueError, "silent"):
            atmosphere.prepare_slow_source_bed(silent, 2, self.root / "silent-beds")

    def test_changed_cache_is_not_overwritten_or_silently_reused(self):
        result = atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        target = Path(result["path"])
        target.write_bytes(b"changed saved audio")
        with self.assertRaisesRegex(ValueError, "loop changed"):
            atmosphere.prepare_slow_source_bed(self.source, 4.75, self.root / "beds")
        self.assertEqual(target.read_bytes(), b"changed saved audio")


if __name__ == "__main__":
    unittest.main()
