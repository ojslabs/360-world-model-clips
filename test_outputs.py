"""Output lifecycle checks: local assembly never resubmits a saved Fal orbit."""
import tempfile
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import composite
import crowd_audio
import fal_camera
import media
import orbit_loop
import server
import source_atmosphere


class OutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = patch.object(server, "DATA", self.root)
        state.start()
        self.addCleanup(state.stop)
        self.video_id = server.SEED
        self.location = server.folder(self.video_id)
        self.location.mkdir(parents=True)
        (self.location / "source.mp4").write_bytes(b"source fixture")
        frames = self.location / "frames"
        frames.mkdir()
        (frames / "cut1.png").write_bytes(b"frozen frame fixture")
        self.project = {
            "id": self.video_id, "media": {"duration": 900, "fps": 30000 / 1001},
            "candidates": [{"id": "cut1", "selected": True, "freeze_time": 120.12,
                            "tail_seconds": 4, "frame_url": "/frame.png"}],
            "generations": [self._run("first", 120.12), self._run("second", 240.24)],
        }
        media.write_json(server.manifest(self.video_id), self.project)
        self.provider = patch.object(fal_camera, "generate", side_effect=AssertionError("Unexpected model request"))
        self.generate = self.provider.start()
        self.addCleanup(self.provider.stop)

    def _run(self, identifier, time):
        target = self.location / "exports" / identifier
        target.mkdir(parents=True)
        (target / "fal-camera.mp4").write_bytes(b"saved Fal fixture")
        return {"id": identifier, "provider": "Fal", "status": "complete", "freeze_time": time,
                "tail_seconds": 4, "request_id": "acknowledged-" + identifier,
                "url": f"/media/{self.video_id}/exports/{identifier}/fal-camera.mp4"}

    def _assemble(self, source, orbit, freeze, output, tail_seconds):
        Path(output).write_bytes(f"completed at {freeze} with {tail_seconds}".encode())
        return {"path": str(output), "media": {"duration": media.DEFAULTS["lead_seconds"] + media.DEFAULTS["orbit_seconds"] + tail_seconds},
                "audio_provenance": {"orbit": "source_crowd_centre_suppressed"},
                "source_window": {"freeze_time": freeze, "start": freeze - media.DEFAULTS["lead_seconds"], "end": freeze + tail_seconds}}

    def _combine(self, paths, output):
        Path(output).write_bytes(b"combined local fixture")
        return {"path": str(output), "media": {"duration": len(paths) * sum(media.DEFAULTS[key] for key in ("lead_seconds", "orbit_seconds", "tail_seconds"))},
                "clips": [{"path": str(path)} for path in paths]}

    def test_completed_orbit_assembles_locally_and_repeat_reuses_output(self):
        with patch.object(composite, "assemble", side_effect=self._assemble) as assemble:
            first = server.assemble_highlight(self.video_id, "first", 4)
            repeated = server.assemble_highlight(self.video_id, "first", 4)
        self.assertEqual(first, repeated)
        self.assertEqual(assemble.call_count, 1)
        self.generate.assert_not_called()
        self.assertNotIn("path", first)
        self.assertEqual(first["source_window"]["freeze_time"], 120.12)
        saved = server.load_project(self.video_id)["generations"][0]
        self.assertEqual(saved["request_id"], "acknowledged-first")
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(len(saved["composites"]), 1)

    def test_new_timing_recut_preserves_historical_thirteen_and_nineteen_second_outputs(self):
        target = self.location / "exports/first"
        old_path = target / "highlight-8-6-5.mp4"
        old_path.write_bytes(b"historical nineteen-second render")
        old = {"id": "historical-19", "tail_seconds": 5, "lead_seconds": 8,
               "url": f"/media/{self.video_id}/exports/first/{old_path.name}",
               "media": {"duration": 19}, "audio_version": crowd_audio.AUDIO_VERSION}
        thirteen_path = target / "highlight-3-6-4.mp4"
        thirteen_path.write_bytes(b"historical thirteen-second render")
        thirteen = {**old, "id": "historical-13", "lead_seconds": 3, "tail_seconds": 4,
                    "url": f"/media/{self.video_id}/exports/first/{thirteen_path.name}",
                    "media": {"duration": 13}}
        previous = self.project["generations"][0]
        previous.update(tail_seconds=5, lead_seconds=8, composites=[old, thirteen])
        media.write_json(server.manifest(self.video_id), self.project)
        native_before = (target / "fal-camera.mp4").read_bytes()
        with patch.object(composite, "assemble", side_effect=self._assemble):
            recut = server.assemble_highlight(self.video_id, "first", 4)
        self.assertEqual(recut["media"]["duration"], 18)
        self.assertEqual((recut["lead_seconds"], recut["tail_seconds"]), (8, 4))
        self.assertEqual(old_path.read_bytes(), b"historical nineteen-second render")
        self.assertEqual(thirteen_path.read_bytes(), b"historical thirteen-second render")
        self.assertEqual((target / "fal-camera.mp4").read_bytes(), native_before)
        saved = server.read_run(self.video_id, "first")
        self.assertIn(old, saved["composites"])
        self.assertIn(thirteen, saved["composites"])
        self.assertEqual((saved["lead_seconds"], saved["tail_seconds"]), (8, 5))
        self.generate.assert_not_called()

    def test_reference_anchoring_reaches_the_compositor_without_another_provider_call(self):
        self.project["generations"][0]["delivery"] = {"anchor_reference": True}
        media.write_json(server.manifest(self.video_id), self.project)
        def anchor(orbit, frame, output):
            self.assertEqual(orbit.name, "fal-camera.mp4")
            self.assertEqual(frame.name, "frame.png")
            output.write_bytes(b"reference anchored orbit")
            return {"path": str(output), "first_last_pixel_hash_equal": True,
                    "generated_motion_verified": False, "media": {"duration": 6}}
        def assemble(source, orbit, freeze, output, tail_seconds, reference_anchored):
            self.assertTrue(reference_anchored)
            self.assertTrue(orbit.name.startswith("fal-camera-loop-"))
            return self._assemble(source, orbit, freeze, output, tail_seconds)
        with patch.object(orbit_loop, "anchor_reference", side_effect=anchor) as closure, \
             patch.object(composite, "assemble", side_effect=assemble):
            result = server.assemble_highlight(self.video_id, "first", 4)
            self.assertEqual(server.assemble_highlight(self.video_id, "first", 4), result)
        closure.assert_called_once()
        self.generate.assert_not_called()
        self.assertTrue(result["reference_anchored"])
        self.assertIn("-matched-source_crowd_v1.mp4", result["url"])
        saved = server.load_project(self.video_id)["generations"][0]
        self.assertTrue(saved["raw_url"].endswith("/fal-camera.mp4"))

    def test_audio_version_is_separate_and_missing_file_can_be_reassembled(self):
        with patch.object(composite, "assemble", side_effect=self._assemble) as assemble:
            original = server.assemble_highlight(self.video_id, "first", 4)
            with patch.object(crowd_audio, "AUDIO_VERSION", "next_audio"):
                revised = server.assemble_highlight(self.video_id, "first", 4)
                (self.location / "exports/first" / Path(revised["url"]).name).unlink()
                recovered = server.assemble_highlight(self.video_id, "first", 4)
        self.assertEqual(assemble.call_count, 3)
        self.assertEqual(revised["id"], recovered["id"])
        self.assertNotEqual(original["id"], revised["id"])
        self.assertEqual(revised["media"]["duration"], 18)
        self.assertTrue((self.location / "exports/first" / Path(original["url"]).name).is_file())

    def test_source_audio_override_uses_own_slow_bed_and_preserves_prior_crowd_edit(self):
        with patch.object(composite, "assemble", side_effect=self._assemble):
            previous = server.assemble_highlight(self.video_id, "first", 4)
        previous_path = self.location / "exports/first" / Path(previous["url"]).name
        previous_bytes = previous_path.read_bytes()
        media.write_json(self.root / "audio-overrides.json", {self.video_id: "source_slow_motion"})
        target = self.location / "exports/first"
        bed = {"path": str(target / "source-audio-test.wav"),
               "provenance": {"method": source_atmosphere.METHOD, "source_video_id": self.video_id,
                              "playback_speed": .75}}

        def assemble(source, orbit, freeze, output, tail_seconds, **options):
            self.assertEqual(source, self.location / "source.mp4")
            self.assertEqual(freeze, 120.12)
            self.assertEqual(options["crowd_path"], bed["path"])
            self.assertEqual(options["crowd_provenance"], bed["provenance"])
            self.assertIs(options["auto_crowd"], False)
            return self._assemble(source, orbit, freeze, output, tail_seconds)

        with patch.object(source_atmosphere, "prepare_slow_source_bed", return_value=bed) as prepare, \
                patch.object(composite, "assemble", side_effect=assemble) as assembled:
            revised = server.assemble_highlight(self.video_id, "first", 4)
            self.assertEqual(server.assemble_highlight(self.video_id, "first", 4), revised)
        prepare.assert_called_once_with(self.location / "source.mp4", 120.12, target,
                                       seconds=media.DEFAULTS["orbit_seconds"] + 2 * composite.JOIN_FADE_SECONDS)
        assembled.assert_called_once()
        self.assertEqual(revised["audio_version"], source_atmosphere.METHOD)
        self.assertEqual(revised["audio_provenance"]["orbit"], "source_slow_motion_loop")
        self.assertEqual(revised["audio_provenance"]["audio_version"], revised["audio_version"])
        self.assertNotEqual(previous["id"], revised["id"])
        self.assertNotEqual(previous["url"], revised["url"])
        self.assertEqual(previous_path.read_bytes(), previous_bytes)
        saved = server.read_run(self.video_id, "first")
        self.assertIn(previous, saved["composites"])
        self.assertIn(revised, saved["composites"])
        self.assertEqual(saved["request_id"], "acknowledged-first")
        self.generate.assert_not_called()

    def test_audio_override_for_another_source_leaves_default_composition_unchanged(self):
        media.write_json(self.root / "audio-overrides.json", {"8o40mSS05iE": "source_slow_motion"})
        with patch.object(source_atmosphere, "prepare_slow_source_bed") as prepare, \
                patch.object(composite, "assemble", side_effect=self._assemble) as assembled:
            result = server.assemble_highlight(self.video_id, "first", 4)
        prepare.assert_not_called()
        self.assertNotIn("auto_crowd", assembled.call_args.kwargs)
        self.assertNotIn("crowd_path", assembled.call_args.kwargs)
        self.assertEqual(result["audio_version"], crowd_audio.AUDIO_VERSION)
        self.assertEqual(server.audio_mode(self.video_id), "crowd_atmosphere")
        self.generate.assert_not_called()

    def test_failed_source_audio_preparation_never_substitutes_borrowed_crowd(self):
        media.write_json(self.root / "audio-overrides.json", {self.video_id: "source_slow_motion"})
        before = server.load_project(self.video_id)
        with patch.object(source_atmosphere, "prepare_slow_source_bed", side_effect=ValueError("Silent source")), \
                patch.object(composite, "assemble") as assembled:
            with self.assertRaisesRegex(ValueError, "Silent source"):
                server.assemble_highlight(self.video_id, "first", 4)
        assembled.assert_not_called()
        self.assertEqual(server.load_project(self.video_id), before)
        self.generate.assert_not_called()

    def test_saved_run_reports_real_local_phases_before_each_operation(self):
        self.project["generations"][0].update(prompt="saved prompt", delivery={"anchor_reference": True})
        media.write_json(server.manifest(self.video_id), self.project)
        observed = []

        def assert_phase(expected):
            run = server.public_project(self.video_id)["generations"][0]
            observed.append(run["stage"])
            self.assertEqual(run["stage"], expected)
            self.assertIsNotNone(run["stage_started_at"])

        def anchor(orbit, frame, output):
            assert_phase("anchoring")
            output.write_bytes(b"reference anchored orbit")
            return {"path": str(output), "first_last_pixel_hash_equal": True,
                    "generated_motion_verified": False, "media": {"duration": 6}}

        def assemble(source, orbit, freeze, output, tail_seconds, reference_anchored):
            assert_phase("assembling")
            return self._assemble(source, orbit, freeze, output, tail_seconds)

        with patch.object(orbit_loop, "anchor_reference", side_effect=anchor), \
             patch.object(composite, "assemble", side_effect=assemble), \
             patch.object(server.time, "time", side_effect=[100, 104, 110]):
            result = server.execute_generation(self.video_id, "first", resume_only=True)
        self.assertEqual(observed, ["anchoring", "assembling"])
        self.assertEqual(result["local_stage_timings"], {"anchoring": 4, "assembling": 6})
        self.assertEqual(result["local_stage"], "complete")
        self.assertEqual(server.public_project(self.video_id)["generations"][0]["stage"], "complete")
        self.generate.assert_not_called()
        self.assertEqual(len(server.load_project(self.video_id)["generations"][0]["composites"]), 1)
        self.generate.assert_not_called()

    def test_direct_cut_revision_bypasses_legacy_cache_and_preserves_previous_edit(self):
        with patch.object(composite, "assemble", side_effect=self._assemble):
            old = server.assemble_highlight(self.video_id, "first", 4)
        project = server.load_project(self.video_id)
        project["generations"][0]["delivery"] = {
            "anchor_reference": True, "transition": "cut", "resume_next_frame": True}
        media.write_json(server.manifest(self.video_id), project)
        def anchor(orbit, frame, output, transition):
            self.assertEqual(transition, "cut")
            output.write_bytes(b"direct cut orbit")
            return {"first_last_pixel_hash_equal": True, "transition": "cut", "media": {"duration": 6}}
        def assemble(source, orbit, freeze, output, tail_seconds, reference_anchored, resume_next_frame):
            self.assertTrue(reference_anchored)
            self.assertTrue(resume_next_frame)
            return self._assemble(source, orbit, freeze, output, tail_seconds)
        with patch.object(orbit_loop, "anchor_reference", side_effect=anchor) as anchored, \
             patch.object(composite, "assemble", side_effect=assemble) as assembled:
            revised = server.assemble_highlight(self.video_id, "first", 4)
            self.assertEqual(server.assemble_highlight(self.video_id, "first", 4), revised)
        anchored.assert_called_once()
        assembled.assert_called_once()
        self.assertEqual(revised["previous_edit_url"], old["url"])
        self.assertEqual(revised["join_version"], "cut_next_frame_v1")
        self.assertNotEqual(revised["url"], old["url"])
        self.assertTrue((self.location / "exports/first" / Path(old["url"]).name).is_file())
        self.generate.assert_not_called()

    def test_local_assembly_failure_preserves_saved_orbit_and_request(self):
        before = server.load_project(self.video_id)
        orbit = self.location / "exports/first/fal-camera.mp4"
        with patch.object(composite, "assemble", side_effect=RuntimeError("local encoder failed")):
            with self.assertRaisesRegex(RuntimeError, "local encoder"):
                server.assemble_highlight(self.video_id, "first", 4)
        self.assertEqual(server.load_project(self.video_id), before)
        self.assertEqual(orbit.read_bytes(), b"saved Fal fixture")
        self.generate.assert_not_called()

    def test_generation_remains_complete_when_automatic_local_assembly_fails(self):
        def generated(frame, target, prompt, seconds):
            output = target / "fal-camera.mp4"
            output.write_bytes(b"new provider video")
            return {"path": str(output), "media": {"duration": 6}, "request_id": "new-request"}

        self.generate.side_effect = generated
        def immediate_task(kind, operation, **_binding):
            operation()
            return {"job_id": "unit-test-job"}

        with patch.object(server, "task", side_effect=immediate_task), \
             patch.object(server, "generation_status", return_value={"ready": True, "model": fal_camera.MODEL}), \
             patch.object(media, "run", return_value=b""), \
             patch.object(server, "assemble_highlight", side_effect=RuntimeError("local encoder failed")):
            response = server.start_generation(self.video_id, "fal-h3-max")
        saved = next(r for r in server.load_project(self.video_id)["generations"] if r["id"] == response["run_id"])
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["request_id"], "new-request")
        self.assertIn("Retry local assembly", saved["composition_error"])
        self.assertTrue((self.location / "exports" / response["run_id"] / "fal-camera.mp4").is_file())

    def test_combined_outputs_preserve_selected_order_and_reuse_identical_reel(self):
        with patch.object(composite, "assemble", side_effect=self._assemble):
            first = server.assemble_highlight(self.video_id, "first", 4)
            second = server.assemble_highlight(self.video_id, "second", 4)
        with patch.object(composite, "combine", side_effect=self._combine) as combine:
            reverse = server.combine_highlights(self.video_id, [second["id"], first["id"]])
            repeated = server.combine_highlights(self.video_id, [second["id"], first["id"]])
            forwards = server.combine_highlights(self.video_id, [first["id"], second["id"]])
        self.assertEqual(reverse, repeated)
        self.assertNotEqual(reverse["id"], forwards["id"])
        self.assertEqual(combine.call_count, 2)
        self.assertEqual([p.parent.name for p in combine.call_args_list[0].args[0]], ["second", "first"])
        self.assertNotIn("path", reverse)
        self.assertNotIn("clips", reverse)
        self.generate.assert_not_called()

    def test_combined_outputs_reject_invalid_unknown_duplicate_or_missing_selection(self):
        with patch.object(composite, "assemble", side_effect=self._assemble):
            output = server.assemble_highlight(self.video_id, "first", 4)
        for items in (None, [], "first-10", [None], ["unknown"], [output["id"], output["id"]]):
            with self.assertRaises(ValueError):
                server.combine_highlights(self.video_id, items)
        (self.location / "exports/first" / Path(output["url"]).name).unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            server.combine_highlights(self.video_id, [output["id"]])
        self.generate.assert_not_called()

    def test_only_completed_fal_runs_can_be_assembled_or_combined(self):
        with patch.object(composite, "assemble", side_effect=self._assemble):
            output = server.assemble_highlight(self.video_id, "first", 4)
        for provider, status in (("Reactor", "complete"), ("Fal", "failed"), ("Fal", "running")):
            project = server.load_project(self.video_id)
            project["generations"][0].update(provider=provider, status=status)
            media.write_json(server.manifest(self.video_id), project)
            with self.assertRaisesRegex(ValueError, "completed Fal"):
                server.assemble_highlight(self.video_id, "first", 4)
            with self.assertRaises(ValueError):
                server.combine_highlights(self.video_id, [output["id"]])
        self.generate.assert_not_called()

    def test_tail_and_source_context_rejected_before_any_generation(self):
        for tail in (2, 5, 10, 12, True, "4"):
            with self.assertRaisesRegex(ValueError, "4 seconds"):
                server.assemble_highlight(self.video_id, "first", tail)
        with patch.object(server, "generation_status", return_value={"ready": True, "model": fal_camera.MODEL}):
            for freeze, tail in ((2, 4), (898, 4), (899, 4)):
                project = server.load_project(self.video_id)
                project["candidates"][0].update(freeze_time=freeze, tail_seconds=tail)
                media.write_json(server.manifest(self.video_id), project)
                with self.assertRaises(ValueError):
                    server.start_generation(self.video_id, "fal-h3-max")
        self.generate.assert_not_called()

    def test_rerun_copies_original_verified_frame_without_changing_current_selection(self):
        previous = self.project["generations"][0]
        previous.update(item_id="original-cut", frame_sha256=hashlib.sha256(b"original frame").hexdigest())
        (self.location / "exports/first/frame.png").write_bytes(b"original frame")
        media.write_json(server.manifest(self.video_id), self.project)
        before = server.load_project(self.video_id)["candidates"]
        with patch.object(server, "generation_status", return_value={"ready": True, "model": fal_camera.MODEL}), \
             patch.object(server, "task", return_value={"job_id": "bound-job"}):
            started = server.start_generation(self.video_id, "fal-h3-max", reference_run_id="first",
                                              item_id="a-different-current-candidate")
        saved = server.read_run(self.video_id, started["run_id"])
        self.assertEqual(saved["freeze_time"], previous["freeze_time"])
        self.assertEqual(saved["frame_sha256"], previous["frame_sha256"])
        self.assertEqual(saved["reference_run_id"], "first")
        self.assertEqual(saved["tail_seconds"], 4)
        self.assertEqual(saved["lead_seconds"], 8)
        self.assertEqual(saved["prompt"], media.ORBIT_PRESET["input"]["prompt"])
        self.assertEqual(server.load_project(self.video_id)["candidates"], before)
        self.assertEqual((self.location / "exports" / started["run_id"] / "frame.png").read_bytes(), b"original frame")
        self.generate.assert_not_called()

    def test_rerun_refuses_an_altered_original_frame_before_creating_a_run(self):
        previous = self.project["generations"][0]
        previous.update(item_id="original-cut", frame_sha256=hashlib.sha256(b"original frame").hexdigest())
        (self.location / "exports/first/frame.png").write_bytes(b"different frame")
        media.write_json(server.manifest(self.video_id), self.project)
        with patch.object(server, "generation_status", return_value={"ready": True, "model": fal_camera.MODEL}), \
             patch.object(server, "task") as task:
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                server.start_generation(self.video_id, "fal-h3-max", reference_run_id="first")
        task.assert_not_called()
        self.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
