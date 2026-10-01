"""Cron setup appends one line. Commenting it out leaves the entry in the file."""

import os
import unittest
from pathlib import Path

import watchpage


class Result:
    def __init__(self, code, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


class CommentOutCronTest(unittest.TestCase):
    def setUp(self):
        watchpage.warn_config()
        self.calls = []
        self.original_run = watchpage.subprocess.run
        self.listing = (
            "0 0 * * * other-job\n"
            "* * * * * python3 /path/to/watchpage/watchpage.py "
            "# summer-tickets\n"
        )

        def fake_run(args, input=None, capture_output=None, text=None, check=None):
            self.calls.append((list(args), input))
            if args == ["crontab", "-r"]:
                raise AssertionError("crontab must not be deleted")
            if args == ["crontab", "-l"]:
                return Result(0, self.listing)
            if args == ["crontab", "-"]:
                return Result(0)
            raise AssertionError(args)

        watchpage.subprocess.run = fake_run

    def tearDown(self):
        watchpage.subprocess.run = self.original_run

    def test_comments_out_the_watcher_and_keeps_other_jobs(self):
        watchpage.comment_out_cron("summer-tickets")
        written = self.calls[1]
        self.assertEqual(written[0], ["crontab", "-"])
        self.assertEqual(
            written[1],
            "0 0 * * * other-job\n"
            "# * * * * * python3 /path/to/watchpage/watchpage.py "
            "# summer-tickets\n",
        )
        self.assertNotIn((["crontab", "-r"], None), self.calls)

    def test_ignores_the_marker_when_it_is_only_in_the_path(self):
        self.listing = (
            "* * * * * python3 /path/to/summer-tickets/watchpage.py "
            "--config other.json # watchpage:other\n"
        )
        watchpage.comment_out_cron("summer-tickets")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], ["crontab", "-l"])

    def test_leaves_an_already_commented_line_unchanged(self):
        self.listing = (
            "# * * * * * python3 /path/to/watchpage/watchpage.py "
            "# summer-tickets\n"
        )
        watchpage.comment_out_cron("summer-tickets")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], ["crontab", "-l"])


class InstallCronTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.listing = "0 0 * * * other-job\n"
        self.list_code = 0
        self.list_err = ""
        self.write_code = 0
        self.write_err = ""
        self.original_run = watchpage.subprocess.run

        def fake_run(args, input=None, capture_output=None, text=None, check=None):
            self.calls.append((list(args), input))
            if args == ["crontab", "-r"]:
                raise AssertionError("crontab must not be deleted")
            if args == ["crontab", "-l"]:
                return Result(self.list_code, self.listing, self.list_err)
            if args == ["crontab", "-"]:
                return Result(self.write_code, "", self.write_err)
            raise AssertionError(args)

        watchpage.subprocess.run = fake_run

    def tearDown(self):
        watchpage.subprocess.run = self.original_run
        os.environ.pop(watchpage.TEST_MODE, None)

    def line(self, root, flock_bin="/usr/bin/flock"):
        root = Path(root)
        return watchpage.build_cron_line(
            "* * * * *",
            root,
            root / "config.json",
            "summer-tickets",
            "watchpage:summer-tickets",
            flock_bin,
        )

    def test_appends_a_line_and_keeps_other_jobs(self):
        added = self.line("/tmp/watchpage")
        self.assertEqual(
            watchpage.install_cron_job("watchpage:summer-tickets", added),
            "installed",
        )
        written = self.calls[1]
        self.assertEqual(written[0], ["crontab", "-"])
        self.assertEqual(written[1], "0 0 * * * other-job\n" + added + "\n")
        self.assertNotIn((["crontab", "-r"], None), self.calls)

    def test_does_not_add_a_second_active_job(self):
        added = self.line("/tmp/watchpage")
        self.listing = "0 0 * * * other-job\n" + added + "\n"
        self.assertEqual(
            watchpage.install_cron_job("watchpage:summer-tickets", added),
            "active",
        )
        self.assertEqual(self.calls, [(["crontab", "-l"], None)])

    def test_does_not_append_when_the_job_is_commented_out(self):
        added = self.line("/tmp/watchpage")
        self.listing = "# " + added + "\n"
        self.assertEqual(
            watchpage.install_cron_job("watchpage:summer-tickets", added),
            "commented",
        )
        self.assertEqual(len(self.calls), 1)

    def test_installs_into_an_empty_crontab(self):
        added = self.line("/tmp/watchpage")
        self.list_code = 1
        self.list_err = "crontab: no crontab for user\n"
        self.listing = ""
        self.assertEqual(
            watchpage.install_cron_job("watchpage:summer-tickets", added),
            "installed",
        )
        self.assertEqual(self.calls[1][1], added + "\n")

    def test_refuses_to_replace_a_crontab_it_could_not_read(self):
        added = self.line("/tmp/watchpage")
        self.list_code = 1
        self.list_err = "crontab: permission denied\n"
        with self.assertRaises(SystemExit):
            watchpage.install_cron_job("watchpage:summer-tickets", added)
        self.assertEqual(len(self.calls), 1)

    def test_uncomment_restores_the_job_and_keeps_other_lines(self):
        added = self.line("/tmp/watchpage")
        self.listing = "0 0 * * * other-job\n# " + added + "\n"
        self.assertEqual(
            watchpage.uncomment_cron_job("watchpage:summer-tickets"),
            "uncommented",
        )
        self.assertEqual(
            self.calls[1][1],
            "0 0 * * * other-job\n" + added + "\n",
        )
        self.assertNotIn((["crontab", "-r"], None), self.calls)

    def test_marker_in_the_path_does_not_count_as_installed(self):
        self.listing = (
            "* * * * * python3 /path/to/summer-tickets/watchpage.py "
            "--config other.json # watchpage:other\n"
        )
        state, lines = watchpage.cron_job_state("watchpage:summer-tickets")
        self.assertEqual(state, "missing")
        self.assertEqual(lines, [])

    def test_built_line_can_be_commented_out(self):
        added = self.line("/tmp/watchpage")
        self.assertTrue(
            watchpage.comment_contains_marker(added, "watchpage:summer-tickets")
        )
        self.listing = "0 0 * * * other-job\n" + added + "\n"
        watchpage.comment_out_cron("watchpage:summer-tickets")
        self.assertEqual(
            self.calls[1][1],
            "0 0 * * * other-job\n# " + added + "\n",
        )

    def test_refuses_while_a_test_is_running(self):
        os.environ[watchpage.TEST_MODE] = "1"
        with self.assertRaises(SystemExit):
            watchpage.install_cron_job("watchpage:summer-tickets", "line")
        self.assertEqual(self.calls, [])

    def test_line_uses_name_for_lock_and_log(self):
        root = Path("/tmp/watchpage").resolve()
        added = self.line("/tmp/watchpage", flock_bin=None)
        self.assertNotIn("flock", added)
        self.assertIn(f"{root}/summer-tickets.log", added)
        self.assertIn(f"{root}/.venv/bin/python", added)
        self.assertIn(f"--config {root}/config.json", added)
        self.assertTrue(added.startswith("* * * * * "))
        self.assertTrue(added.endswith("# watchpage:summer-tickets"))

    def test_line_quotes_a_path_with_spaces(self):
        root = Path("/tmp/watch page").resolve()
        added = self.line("/tmp/watch page")
        self.assertIn(f"'{root}/summer-tickets.lock'", added)
        self.assertIn(f"'{root}/.venv/bin/python'", added)
        self.assertTrue(
            watchpage.comment_contains_marker(added, "watchpage:summer-tickets")
        )

    def test_rejects_a_schedule_that_is_not_five_fields(self):
        with self.assertRaises(SystemExit):
            watchpage.build_cron_line(
                "@daily",
                Path("/tmp/watchpage"),
                Path("/tmp/watchpage/config.json"),
                "summer-tickets",
                "watchpage:summer-tickets",
                None,
            )

    def test_rejects_a_marker_with_a_hash(self):
        with self.assertRaises(SystemExit):
            watchpage.build_cron_line(
                "* * * * *",
                Path("/tmp/watchpage"),
                Path("/tmp/watchpage/config.json"),
                "summer-tickets",
                "watch#tickets",
                None,
            )
