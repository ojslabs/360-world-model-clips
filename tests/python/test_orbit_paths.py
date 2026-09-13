"""Requested camera geometry and immutable paid-request identity, offline only."""
import copy
from contextlib import nullcontext
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from app import fal_camera
from app import fal_credentials
from app import media
from app import server

KEY = "test-orbit-path-browser-key-never-valid"
VIDEO = "C9sL5j_iUiE"
PATHS = ("around", "over-under", "diagonal")


def position(point):
    azimuth = math.radians(point["azimuth"])
    elevation = math.radians(point["elevation"])
    radius = point["distance"]
    return (radius * math.cos(elevation) * math.cos(azimuth),
            radius * math.cos(elevation) * math.sin(azimuth), radius * math.sin(elevation))


class OrbitGeometryTests(unittest.TestCase):
    def request(self, path):
        return media.orbit_request("https://example.test/saved-reference.png", orbit_path=path)

    def test_default_horizontal_is_the_existing_exact_request(self):
        actual = self.request("around")
        self.assertEqual(actual, media.orbit_request("https://example.test/saved-reference.png"))
        expected = copy.deepcopy(media.ORBIT_PRESET["input"])
        expected["image_url"] = "https://example.test/saved-reference.png"
        self.assertEqual(actual, expected)

    def test_each_path_closes_one_full_circle_at_constant_distance_with_valid_angles(self):
        for path in PATHS:
            with self.subTest(path=path):
                points = self.request(path)["camera_trajectory"]
                self.assertGreaterEqual(len(points), 2)
                self.assertLessEqual(len(points), 12)
                self.assertTrue(all(a["time"] < b["time"] for a, b in zip(points, points[1:])))
                self.assertTrue(all(0 <= point["time"] <= 1 for point in points))
                self.assertTrue(all(-90 <= point["elevation"] <= 90 for point in points))
                self.assertTrue(all(-360 <= point["azimuth"] <= 360 for point in points))
                self.assertTrue(all(point["distance"] == 1 for point in points))
                vectors = [position(point) for point in points]
                for axis in range(3):
                    self.assertAlmostEqual(vectors[0][axis], vectors[-1][axis], places=8)
                self.assertAlmostEqual(vectors[0][0], 1, places=8)
                sweep = sum(math.acos(max(-1, min(1, sum(a * b for a, b in zip(start, end)))))
                            for start, end in zip(vectors, vectors[1:]))
                self.assertAlmostEqual(sweep, 2 * math.pi, places=6)

    def test_paths_preserve_the_initial_and_final_hold_schedule(self):
        for path in PATHS:
            with self.subTest(path=path):
                request = self.request(path)
                points = request["camera_trajectory"]
                self.assertEqual(points[0]["time"], 0)
                self.assertEqual(position(points[0]), position(points[1]))
                self.assertAlmostEqual(points[1]["time"] * request["duration"], .2)
                self.assertAlmostEqual(points[-1]["time"] * request["duration"], 5.3)
                self.assertAlmostEqual((1 - points[-1]["time"]) * request["duration"], .7)

    def test_horizontal_stays_in_the_equatorial_plane(self):
        vectors = [position(point) for point in self.request("around")["camera_trajectory"]]
        self.assertTrue(all(abs(vector[2]) < 1e-9 for vector in vectors))
        self.assertGreater(max(vector[1] for vector in vectors), .8)
        self.assertLess(min(vector[1] for vector in vectors), -.8)

    def test_vertical_visits_over_and_under_poles_in_one_meridian_without_elevation_wrap(self):
        points = self.request("over-under")["camera_trajectory"]
        vectors = [position(point) for point in points]
        self.assertTrue(all(abs(vector[1]) < 1e-8 for vector in vectors))
        self.assertAlmostEqual(max(vector[2] for vector in vectors), 1, places=6)
        self.assertAlmostEqual(min(vector[2] for vector in vectors), -1, places=6)
        self.assertLess(min(vector[0] for vector in vectors), -.99)
        self.assertTrue(all(abs(point["elevation"]) <= 90 for point in points))
        branch_changes = [(start, end) for start, end in zip(points, points[1:])
                          if start["azimuth"] != end["azimuth"]]
        self.assertEqual(len(branch_changes), 2)
        for start, end in branch_changes:
            self.assertEqual(start["elevation"], end["elevation"])
            self.assertEqual(abs(start["elevation"]), 90)
            self.assertEqual(end["azimuth"] - start["azimuth"], 180)
            self.assertGreater(end["time"] - start["time"], 0)
            self.assertLess(end["time"] - start["time"], .00001)

    def test_diagonal_is_a_45_degree_great_circle_rather_than_a_wavering_horizontal_orbit(self):
        vectors = [position(point) for point in self.request("diagonal")["camera_trajectory"]]
        for x, y, z in vectors:
            self.assertAlmostEqual(y, z, places=7)
            self.assertAlmostEqual(x * x + y * y + z * z, 1, places=8)
        self.assertGreater(max(vector[2] for vector in vectors), .6)
        self.assertLess(min(vector[2] for vector in vectors), -.6)
        self.assertLessEqual(max(abs(point["elevation"]) for point in
                                 self.request("diagonal")["camera_trajectory"]), 45.000001)

    def test_only_trajectory_changes_between_paths_and_callers_cannot_mutate_the_preset(self):
        original = copy.deepcopy(media.ORBIT_PRESET)
        default = self.request("around")
        expected = {key: value for key, value in default.items() if key != "camera_trajectory"}
        for path in PATHS:
            request = self.request(path)
            self.assertEqual({key: value for key, value in request.items() if key != "camera_trajectory"}, expected)
            request["camera_trajectory"][0]["distance"] = 99
            self.assertEqual(self.request(path)["camera_trajectory"][0]["distance"], 1)
        self.assertEqual(media.ORBIT_PRESET, original)

    def test_unknown_path_is_rejected_before_a_request_can_be_built(self):
        for path in ("", "spiral", "../vertical", 90, [], {}):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.request(path)


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, operation):
        self.pending.append(operation)

    def run_next(self):
        self.pending.pop(0)()


class OrbitPathBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = Queue()
        self.credential = fal_credentials.Credential(KEY, "test-owner", 9999999999)
        replacements = [patch.object(server, "DATA", self.root), patch.object(server, "JOBS", {}),
                        patch.object(fal_camera, "ROOT", self.root),
                        patch.object(fal_camera, "_json_request", side_effect=AssertionError("No provider requests"))]
        for name in vars(server):
            if name == "POOL" or name.endswith("_POOL"):
                replacements.append(patch.object(server, name, self.queue))
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        location = server.folder(VIDEO)
        (location / "frames").mkdir(parents=True)
        self.frame = location / "frames/moment.png"
        Image.new("RGB", (160, 90), "red").save(self.frame)
        media.write_json(server.manifest(VIDEO), {"id": VIDEO, "media": {"duration": 90, "fps": 30},
            "candidates": [{"id": "moment", "selected": True, "freeze_time": 30,
                            "tail_seconds": media.DEFAULTS["tail_seconds"], "frame_url": "/saved-frame.png"}],
            "generations": []})

    def start(self, path=None, reference=None):
        return server.start_generation(VIDEO, "fal-h3-max", reference, item_id="moment",
                                       orbit_path=path, credential=self.credential)

    def complete(self, started):
        project = server.load_project(VIDEO)
        run = next(run for run in project["generations"] if run["id"] == started["run_id"])
        run["status"] = "complete"
        media.write_json(server.manifest(VIDEO), project)
        media.write_json(server.folder(VIDEO) / "exports" / run["id"] / "run.json", run)
        self.queue.pending.clear()
        return run

    def test_unknown_path_does_not_create_a_run_job_or_provider_request(self):
        before = server.manifest(VIDEO).read_bytes()
        with self.assertRaises(ValueError):
            self.start("unknown-orbit")
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(server.JOBS, {})
        self.assertEqual(server.manifest(VIDEO).read_bytes(), before)
        self.assertFalse((server.folder(VIDEO) / "exports").exists())

    def test_http_selection_forwards_path_and_browser_credential_in_one_generation_call(self):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = "POST", "/api/generate"
        body = json.dumps({"video_id": VIDEO, "mode": "fal-h3-max", "item_id": "moment",
                           "orbit_path": "diagonal"}).encode()
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(body)
        handler.trusted, handler.send_json = Mock(return_value=True), Mock()
        auth = patch.object(server.demo_auth, "intercept", return_value=False) if hasattr(server, "demo_auth") else nullcontext()
        with auth, patch.object(fal_credentials, "require", return_value=self.credential), \
                patch.object(server, "start_generation", return_value={"job_id": "saved-job", "run_id": "saved-run"}) as start:
            handler.do_POST()
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["orbit_path"], "diagonal")
        self.assertIs(start.call_args.kwargs["credential"], self.credential)
        self.assertEqual(self.queue.pending, [])

    def test_queued_worker_uses_saved_path_and_parameters_after_preset_changes(self):
        started = self.start("over-under")
        saved = copy.deepcopy(server.read_run(VIDEO, started["run_id"]))
        expected = media.orbit_request("unused", orbit_path="over-under")
        expected = {key: value for key, value in expected.items() if key not in {"image_url", "prompt"}}
        self.assertEqual(saved["orbit_path"], "over-under")
        self.assertEqual(saved["request_parameters"], expected)
        changed = copy.deepcopy(media.ORBIT_PRESET)
        changed["input"]["camera_trajectory"][2]["azimuth"] = 12
        with patch.object(media, "ORBIT_PRESET", changed), \
                patch.object(fal_camera, "generate", side_effect=fal_camera.FalError("offline stop")) as generate:
            self.queue.run_next()
        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["request_parameters"], expected)
        self.assertEqual(generate.call_args.kwargs["credential_key"], KEY)
        self.assertEqual(server.read_run(VIDEO, started["run_id"])["request_parameters"], expected)

    def test_rerun_inherits_previous_path_and_frame_unless_another_path_is_explicit(self):
        previous = self.complete(self.start("over-under"))
        Image.new("RGB", (160, 90), "blue").save(self.frame)
        inherited = self.start(reference=previous["id"])
        inherited_run = self.complete(inherited)
        self.assertEqual(inherited_run["orbit_path"], "over-under")
        self.assertEqual(inherited_run["frame_sha256"], previous["frame_sha256"])
        explicit = self.start("diagonal", reference=previous["id"])
        explicit_run = server.read_run(VIDEO, explicit["run_id"])
        self.assertEqual(explicit_run["orbit_path"], "diagonal")
        self.assertEqual(explicit_run["frame_sha256"], previous["frame_sha256"])
        self.assertNotEqual(explicit_run["request_parameters"]["camera_trajectory"],
                            inherited_run["request_parameters"]["camera_trajectory"])

    def test_provider_payload_fingerprint_includes_path_and_receipt_preserves_exact_parameters(self):
        fingerprints = set()
        for path in PATHS:
            target = self.root / path
            with patch.object(fal_camera, "_json_request", side_effect=fal_camera.FalError("offline stop")) as request:
                with self.assertRaisesRegex(fal_camera.FalError, "offline stop"):
                    fal_camera.generate(self.frame, target, media.ORBIT_PRESET["input"]["prompt"],
                                        credential_key=KEY, orbit_path=path)
            request.assert_called_once()
            expected = media.orbit_request(request.call_args.args[3]["image_url"], orbit_path=path)
            self.assertEqual(request.call_args.args[3], expected)
            receipt = media.read_json(target / "fal-request.json")
            self.assertEqual(receipt["parameters"]["camera_trajectory"], expected["camera_trajectory"])
            fingerprints.add(receipt["input_sha256"])
            self.assertNotIn(KEY, (target / "fal-request.json").read_text())
        self.assertEqual(len(fingerprints), len(PATHS))

    def test_acknowledged_recovery_keeps_saved_path_after_preset_change_and_rejects_explicit_change(self):
        target = self.root / "saved-provider"
        prompt = media.ORBIT_PRESET["input"]["prompt"]
        with patch.object(fal_camera, "_json_request", side_effect=[
                {"request_id": "saved-paid-request"}, fal_camera.FalError("offline queue")]):
            with self.assertRaisesRegex(fal_camera.FalError, "offline queue"):
                fal_camera.generate(self.frame, target, prompt, credential_key=KEY, orbit_path="over-under")
        before = media.read_json(target / "fal-request.json")
        changed = copy.deepcopy(media.ORBIT_PRESET)
        changed["input"]["camera_trajectory"][2]["azimuth"] = 9
        with patch.object(media, "ORBIT_PRESET", changed), \
                patch.object(fal_camera, "_json_request", side_effect=fal_camera.FalError("saved read only")) as request:
            with self.assertRaisesRegex(fal_camera.FalError, "saved read only"):
                fal_camera.generate(self.frame, target, prompt, credential_key=KEY,
                                    resume_only=True)
        request.assert_called_once()
        self.assertIn("/saved-paid-request/status", request.call_args.args[0])
        self.assertEqual(len(request.call_args.args), 3)
        after = media.read_json(target / "fal-request.json")
        self.assertEqual(after["parameters"], before["parameters"])
        self.assertEqual(after["input_sha256"], before["input_sha256"])
        with patch.object(fal_camera, "_json_request") as request:
            with self.assertRaisesRegex(fal_camera.FalError, "different saved frame or prompt"):
                fal_camera.generate(self.frame, target, prompt, credential_key=KEY,
                                    orbit_path="diagonal", resume_only=True)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
