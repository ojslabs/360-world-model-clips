"""Centre cancellation and guarded local crowd-bed preparation, without downloads."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile

from app import crowd_audio


class CrowdAudioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fallback = patch.object(crowd_audio, "FALLBACK_PATH", self.root / "fallback-crowd.wav")
        fallback.start()
        self.addCleanup(fallback.stop)
        t = np.arange(12 * 48000) / 48000
        self.voice = .25 * np.sin(2 * np.pi * 1000 * t)
        self.samples = np.column_stack((self.voice + .04 * np.sin(2 * np.pi * 350 * t),
                                        self.voice + .04 * np.sin(2 * np.pi * 550 * t))).astype(np.float32)
        self.source = self.root / "source.wav"
        wavfile.write(self.source, 48000, np.round(self.samples * 32767).astype(np.int16))

    def test_common_voice_cancels_while_distinct_channel_atmosphere_remains(self):
        bed, info = crowd_audio.isolate_stereo(self.samples)
        correlation = abs(float(np.dot(bed, self.voice) / np.dot(self.voice, self.voice)))
        self.assertLess(correlation, .001)
        self.assertGreater(info["bed_rms"], .01)
        self.assertLessEqual(info["bed_peak"], .90)
        self.assertFalse(info["perfect_speech_separation"])

    def test_empty_or_mono_atmosphere_is_rejected(self):
        for samples in (self.samples[:, :1], np.column_stack((self.voice, self.voice)), np.zeros_like(self.samples)):
            with self.assertRaisesRegex(ValueError, "Supply a crowd recording"):
                crowd_audio.isolate_stereo(samples)

    def test_bed_is_local_checked_nonempty_and_source_is_unchanged(self):
        original = self.source.read_bytes()
        output = self.root / "crowd.wav"
        with patch.object(crowd_audio, "_speech_seconds", return_value=[4.2, 0]) as check:
            result = crowd_audio.prepare_crowd_bed(self.source, 6, output)
        rate, pcm = wavfile.read(output)
        self.assertEqual(rate, 48000)
        self.assertEqual(len(pcm), 312000)
        self.assertGreater(float(np.std(pcm)), 100)
        self.assertTrue(result["provenance"]["speech_check_passed"])
        self.assertEqual(result["provenance"]["source_start"], 2.75)
        self.assertEqual(result["provenance"]["source_end"], 9.25)
        self.assertEqual(result["provenance"]["audio_version"], crowd_audio.AUDIO_VERSION)
        self.assertEqual(self.source.read_bytes(), original)
        check.assert_called_once()
        self.assertFalse(list(self.root.glob(".crowd-check-*")))

    def test_remaining_speech_or_missing_detector_does_not_publish_a_bed(self):
        for response in ([4, 1], RuntimeError("detector unavailable")):
            output = self.root / "rejected.wav"
            options = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with patch.object(crowd_audio, "_speech_seconds", **options):
                with self.assertRaises((ValueError, RuntimeError)):
                    crowd_audio.prepare_crowd_bed(self.source, 6, output)
            self.assertFalse(output.exists())

    def test_missing_or_modified_model_cannot_bypass_the_speech_guard(self):
        wrong = self.root / "wrong.onnx"
        wrong.write_bytes(b"not the pinned model")
        for model in (self.root / "missing.onnx", wrong):
            with patch.object(crowd_audio, "MODEL_PATH", model):
                with self.assertRaisesRegex(RuntimeError, "speech check is unavailable"):
                    crowd_audio._speech_seconds([self.source])

    def test_existing_bed_and_insufficient_context_are_rejected(self):
        output = self.root / "existing.wav"
        output.write_bytes(b"existing crowd")
        with self.assertRaises(ValueError):
            crowd_audio.prepare_crowd_bed(self.source, 6, output)
        self.assertEqual(output.read_bytes(), b"existing crowd")
        with self.assertRaisesRegex(ValueError, "required crowd interval"):
            crowd_audio.prepare_crowd_bed(self.source, 1, self.root / "short.wav")

    def test_configured_fallback_retains_source_provenance_and_rejects_modified_audio(self):
        with patch.object(crowd_audio, "_speech_seconds", return_value=[4, 0]):
            created = crowd_audio.configure_fallback(self.source, 6, "C9sL5j_iUiE")
        loaded = crowd_audio.fallback_bed()
        self.assertEqual(loaded["provenance"], created["provenance"])
        self.assertEqual(loaded["provenance"]["source_video_id"], "C9sL5j_iUiE")
        self.assertTrue(loaded["provenance"]["reused_stadium_ambience"])
        crowd_audio.FALLBACK_PATH.write_bytes(b"changed audio")
        with self.assertRaisesRegex(crowd_audio.CrowdUnavailable, "no verified fallback"):
            crowd_audio.fallback_bed()


if __name__ == "__main__":
    unittest.main()
