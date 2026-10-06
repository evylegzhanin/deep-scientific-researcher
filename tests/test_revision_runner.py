import json
from pathlib import Path

from scripts import run_revision_checks as runner


def test_versioned_runner_uses_selected_image_and_separate_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "Path", lambda value: tmp_path if value == "/home/ubuntu/benchmark-results" else Path(value))
    monkeypatch.setattr("sys.argv", ["run_revision_checks", "--revision", "revision-2", "--image", "research-backend:revision2"])
    artifacts = {"scripts.benchmark_quality":"quality-checks.json", "scripts.benchmark_security":"security-checks.json",
                 "scripts.run_pipeline_load":"research-load-summary.json", "scripts.benchmark_inference":"inference-context.json"}
    stages = []

    def execute(command, **kwargs):
        assert "research-backend:revision2" in command
        if "-m" in command:
            module = command[command.index("-m") + 1]
            stages.append(module)
            assert not any("/revision-1" in arg for arg in command)
            assert any("/revision-2" in arg for arg in command)
            (tmp_path / "revision-2" / artifacts[module]).write_text("{}")
        return 0

    monkeypatch.setattr(runner.subprocess, "call", execute)
    runner.main()
    status = json.loads((tmp_path / "revision-2/suite-status.json").read_text())
    assert stages == list(artifacts)
    assert all(stage["state"] == "complete" for stage in status["stages"])
    assert "finished_at" in status
