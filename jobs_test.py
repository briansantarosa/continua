"""Agent-free tests for supervised background jobs."""
import os
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jobs as jb

class TestJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_jobs, self.old_logs, self.old_desk = (jb.JOBS_DIR,
            jb.LOG_DIR, jb.DESK_ROOT)
        jb.JOBS_DIR = os.path.join(self.tmp, "jobs")
        jb.LOG_DIR = os.path.join(self.tmp, "logs")
        jb.DESK_ROOT = self.tmp
        self.desk = os.path.join(self.tmp, "residenta")
        os.makedirs(os.path.join(self.desk, "tmp"), exist_ok=True)
        # the desk must be continua-accessible (the transient unit runs as
        # uid continua — a bob-owned 700 desk fails WorkingDirectory setup);
        # the PARENT must be traversable by continua too (o+x), while the
        # state/log dirs stay bob-owned
        import pwd, subprocess
        uid = pwd.getpwnam("continua").pw_uid
        subprocess.run(["sudo", "-n", "chown", "-R", str(uid), self.desk],
                       check=True)
        os.chmod(self.tmp, 0o711)

    def tearDown(self):
        # kill anything the tests started
        st = jb._load_state("residenta")
        for jid in list(st["jobs"]):
            jb.stop("residenta", jid)
        jb.JOBS_DIR, jb.LOG_DIR, jb.DESK_ROOT = self.old_jobs, self.old_logs, self.old_desk

    def test_start_sleeps_and_completes(self):
        res = jb.start("residenta", "sleep 2 && echo done", name="sleeper")
        self.assertTrue(res["ok"], res)
        self.assertIn("continua-job-", res["unit"])
        time.sleep(3)
        st = jb.status("residenta")
        self.assertFalse(st["jobs"][res["job_id"]]["alive"])  # transient unit done
        out = jb.output("residenta", res["job_id"])
        self.assertIn("done", "\n".join(out["lines"]))

    def test_start_refuses_sudo(self):
        res = jb.start("residenta", "sudo cat /etc/shadow")
        self.assertFalse(res["ok"])
        self.assertIn("sudo", res["error"])

    def test_concurrency_bound(self):
        r1 = jb.start("residenta", "sleep 30", name="a")
        r2 = jb.start("residenta", "sleep 30", name="b")
        r3 = jb.start("residenta", "sleep 30", name="c")
        self.assertTrue(r1["ok"] and r2["ok"] and r3["ok"])
        r4 = jb.start("residenta", "sleep 30", name="d")
        self.assertFalse(r4["ok"])
        self.assertIn("concurrent", r4["error"])
        # cleanup
        for r in (r1, r2, r3):
            jb.stop("residenta", r["job_id"])

    def test_unknown_job(self):
        self.assertFalse(jb.status("residenta", "nope")["ok"])
        self.assertFalse(jb.stop("residenta", "nope")["ok"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
