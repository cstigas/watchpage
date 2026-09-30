"""Commenting out the cron line leaves the entry in the file."""

import unittest

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
