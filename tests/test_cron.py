"""Commenting out the cron line leaves the entry in the file."""

import unittest

import watch


class Result:
    def __init__(self, code, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


class CommentOutCronTest(unittest.TestCase):
    def setUp(self):
        watch.warn_config()
        self.calls = []
        self.original_run = watch.subprocess.run
        self.listing = (
            "0 0 * * * other-job\n"
            "* * * * * python3 /home/cstigas/christmas-town-watch/watch.py "
            "# christmas-town-watch\n"
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

        watch.subprocess.run = fake_run

    def tearDown(self):
        watch.subprocess.run = self.original_run

    def test_comments_out_the_watcher_and_keeps_other_jobs(self):
        watch.comment_out_cron("christmas-town-watch")
        written = self.calls[1]
        self.assertEqual(written[0], ["crontab", "-"])
        self.assertEqual(
            written[1],
            "0 0 * * * other-job\n"
            "# * * * * * python3 /home/cstigas/christmas-town-watch/watch.py "
            "# christmas-town-watch\n",
        )
        self.assertNotIn((["crontab", "-r"], None), self.calls)

    def test_ignores_the_marker_when_it_is_only_in_the_path(self):
        self.listing = (
            "* * * * * python3 /home/cstigas/christmas-town-watch/watch.py "
            "--config other.json # watchpage:other\n"
        )
        watch.comment_out_cron("christmas-town-watch")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], ["crontab", "-l"])

    def test_leaves_an_already_commented_line_unchanged(self):
        self.listing = (
            "# * * * * * python3 /home/cstigas/christmas-town-watch/watch.py "
            "# christmas-town-watch\n"
        )
        watch.comment_out_cron("christmas-town-watch")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], ["crontab", "-l"])
