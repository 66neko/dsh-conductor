from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from conductor.tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxSession
from examples.quickstart import check_tmux


class QuickstartProbeTests(unittest.TestCase):
    def run_probe(self, workspace: Path, lines: list[str]) -> None:
        session = mock.Mock(spec=TmuxSession)
        session.name = "quickstart-probe"
        session.capture.side_effect = lambda *, history_lines=0: "\n".join(lines[-(history_lines + 50):]) + "\n"
        session.exists.return_value = True
        with (
            mock.patch("examples.quickstart.TmuxSession.create", return_value=session),
            mock.patch("examples.quickstart.subprocess.run", return_value=mock.Mock(stdout=f"{HISTORY_LIMIT}\n")),
            mock.patch("conductor.worker_log.TmuxSession", return_value=session),
            redirect_stdout(io.StringIO()),
        ):
            try:
                check_tmux(workspace)
            finally:
                session.close.assert_called_once()

    def test_history_and_persisted_log_accept_tmux_trailing_padding(self) -> None:
        expected = [f"PROBE-{i:05d}" for i in range(1, 6001)]
        # tmux 3.2a 的 capture-pane -J 会在完整行后保留填充空格。
        for padding in ("", " " * 39):
            with self.subTest(padding=len(padding)), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                self.run_probe(workspace, [line + padding for line in expected])
                persisted = [
                    line[2:] for line in (workspace / "tmux-probe.log").read_text(encoding="utf-8").splitlines()
                    if line.startswith("| ")
                ]
                self.assertEqual(persisted, expected[-(DEFAULT_CAPTURE_HISTORY_LINES + 50):])

    def test_history_still_rejects_missing_duplicate_reordered_and_blank_lines(self) -> None:
        for defect in ("missing", "duplicate", "reordered", "blank"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lines = [f"PROBE-{i:05d}   " for i in range(1, 6001)]
                if defect == "missing":
                    del lines[100]
                elif defect == "duplicate":
                    lines[100] = lines[99]
                elif defect == "reordered":
                    lines[100], lines[101] = lines[101], lines[100]
                else:
                    lines.insert(100, "   ")
                with self.assertRaisesRegex(RuntimeError, "tmux 全量历史缺行或顺序错误"):
                    self.run_probe(Path(directory), lines)


if __name__ == "__main__":
    unittest.main()
