"""The background Excel / PDF download (services/excel_cache.py + the /excel and /pdf
routes in generation_router), on an in-memory SQLite database with fake builders.

No LibreOffice, no AI, no live database: DATABASE_URL is pointed at SQLite before anything
imports `database`, and the two real builders are replaced by fakes that return bytes.

LegacyExcelFlowTests uses only the Excel API as it stood before the PDF change (no `kind`
argument anywhere), so the same class can be run against the older commit to show the
Excel flow behaves the same.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import unittest

os.environ["DATABASE_URL"] = "sqlite://"

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from database import Base  # noqa: E402
from models.project_model import Project  # noqa: E402
from models.questionnaire_model import QuestionnaireAnswer  # noqa: E402
from models.user_model import User  # noqa: E402
from routers import generation_router as g  # noqa: E402
from services import excel_cache, generation_jobs  # noqa: E402

HAS_KIND = "kind" in excel_cache.get.__code__.co_varnames
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def body(resp) -> bytes:
    async def collect():
        return b"".join([c async for c in resp.body_iterator])
    return asyncio.run(collect())


class _Base(unittest.TestCase):
    """A fresh SQLite database, cache directory and build state for every test."""

    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.user = User(email="test@example.com", hashed_password="x")
        self.db.add(self.user)
        self.db.commit()
        self.project = Project(title="Cache test", user_id=self.user.id, industry="Manufacturing")
        self.db.add(self.project)
        self.db.commit()
        self.db.add(QuestionnaireAnswer(project_id=self.project.id, collected_data='{"a": 1}'))
        self.db.commit()

        self.cache_dir = tempfile.mkdtemp(prefix="finthara_cache_test_")
        self.saved = {
            "dir": excel_cache._DIR, "state": dict(excel_cache._state),
            "session": g.SessionLocal, "may_export": g.may_export,
            "excel": g._build_excel_file, "pdf": getattr(g, "_build_pdf_file", None),
            "submit": generation_jobs.submit_task,
        }
        excel_cache._DIR = self.cache_dir
        excel_cache._state.clear()
        g.SessionLocal = self.Session
        g.may_export = lambda *a, **k: (True, None)

        self.calls = {"excel": 0, "pdf": 0}
        self.queued = []

        def fake_excel(db, project):
            self.calls["excel"] += 1
            return f"XLSX {project.title} #{self.calls['excel']}".encode(), "model.xlsx", XLSX

        def fake_pdf(db, project):
            self.calls["pdf"] += 1
            return f"PDF {project.title} #{self.calls['pdf']}".encode(), "report.pdf", "application/pdf"

        g._build_excel_file = fake_excel
        if self.saved["pdf"] is not None:
            g._build_pdf_file = fake_pdf
        # Queue the background job instead of running it, so a test decides when it runs.
        generation_jobs.submit_task = lambda fn, *args: self.queued.append((fn, args))

    def tearDown(self):
        excel_cache._DIR = self.saved["dir"]
        excel_cache._state.clear()
        excel_cache._state.update(self.saved["state"])
        g.SessionLocal = self.saved["session"]
        g.may_export = self.saved["may_export"]
        g._build_excel_file = self.saved["excel"]
        if self.saved["pdf"] is not None:
            g._build_pdf_file = self.saved["pdf"]
        generation_jobs.submit_task = self.saved["submit"]
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def run_queued(self):
        jobs, self.queued[:] = list(self.queued), []
        for fn, args in jobs:
            fn(*args)

    def fp(self):
        return g._excel_fingerprint(self.db, self.project)

    def ep(self, fn):
        return fn(project=self.project, db=self.db, current_user=self.user)

    def rename(self, title):
        self.project.title = title
        self.db.commit()


class LegacyExcelFlowTests(_Base):
    """The Excel download, through the routes, as it worked before the PDF change."""

    def test_prepare_then_status_then_download(self):
        self.assertEqual(self.ep(g.excel_status), {"status": "none"})
        self.assertEqual(self.ep(g.prepare_excel), {"status": "running"})
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.ep(g.excel_status), {"status": "running"})
        self.run_queued()
        self.assertEqual(self.ep(g.excel_status), {"status": "ready"})
        resp = self.ep(g.download_excel)
        self.assertEqual(body(resp), b"XLSX Cache test #1")
        self.assertEqual(resp.media_type, XLSX)
        self.assertIn('filename="model.xlsx"', resp.headers["content-disposition"])
        self.assertEqual(self.calls["excel"], 1)     # served from the cache, not rebuilt

    def test_a_second_prepare_while_building_starts_nothing_new(self):
        self.ep(g.prepare_excel)
        self.assertEqual(self.ep(g.prepare_excel), {"status": "running"})
        self.assertEqual(len(self.queued), 1)

    def test_prepare_when_ready_does_not_rebuild(self):
        self.ep(g.prepare_excel)
        self.run_queued()
        self.assertEqual(self.ep(g.prepare_excel), {"status": "ready"})
        self.assertEqual(self.queued, [])

    def test_direct_download_without_prepare_builds_and_keeps_the_file(self):
        self.assertEqual(body(self.ep(g.download_excel)), b"XLSX Cache test #1")
        self.assertEqual(self.ep(g.excel_status), {"status": "ready"})
        self.assertEqual(body(self.ep(g.download_excel)), b"XLSX Cache test #1")
        self.assertEqual(self.calls["excel"], 1)

    def test_an_edit_means_the_old_file_is_never_served(self):
        self.ep(g.download_excel)
        old_fp = self.fp()
        self.rename("Edited title")
        self.assertNotEqual(self.fp(), old_fp)
        self.assertEqual(self.ep(g.excel_status), {"status": "none"})
        self.assertIsNone(excel_cache.get(self.project.id, self.fp()))
        self.assertEqual(body(self.ep(g.download_excel)), b"XLSX Edited title #2")

    def test_a_failed_build_is_reported_and_can_be_retried(self):
        def broken(db, project):
            raise RuntimeError("template missing")
        g._build_excel_file = broken
        self.ep(g.prepare_excel)
        self.run_queued()
        st = self.ep(g.excel_status)
        self.assertEqual(st["status"], "failed")
        self.assertIn("template missing", st["error"])
        self.assertEqual(self.ep(g.prepare_excel), {"status": "running"})
        self.assertEqual(len(self.queued), 1)


@unittest.skipUnless(HAS_KIND, "excel_cache has no `kind` (the code before the PDF change)")
class ExcelAndPdfKeptApartTests(_Base):

    def test_state_and_files_are_separate_per_kind(self):
        fp = self.fp()
        pid = self.project.id
        excel_cache.put(pid, fp, b"X", "a.xlsx", XLSX, "excel")
        self.assertIsNone(excel_cache.get(pid, fp, "pdf"))
        self.assertEqual(excel_cache.status(pid, fp, "pdf"), {"status": "none"})
        excel_cache.put(pid, fp, b"P", "a.pdf", "application/pdf", "pdf")
        self.assertEqual(excel_cache.get(pid, fp, "excel")[0], b"X")
        self.assertEqual(excel_cache.get(pid, fp, "pdf")[0], b"P")
        self.assertEqual(sorted(os.listdir(self.cache_dir)),
                         [f"{pid}.excel.bin", f"{pid}.excel.json",
                          f"{pid}.pdf.bin", f"{pid}.pdf.json"])

    def test_default_kind_is_excel(self):
        fp = self.fp()
        pid = self.project.id
        excel_cache.put(pid, fp, b"X", "a.xlsx", XLSX)
        self.assertEqual(excel_cache.get(pid, fp, "excel")[0], b"X")
        self.assertTrue(excel_cache.begin(pid))
        self.assertEqual(excel_cache.status(pid, fp, "excel"), {"status": "running"})

    def test_an_excel_build_in_flight_does_not_block_a_pdf_build(self):
        pid, fp = self.project.id, self.fp()
        self.assertTrue(excel_cache.begin(pid, "excel"))
        self.assertTrue(excel_cache.begin(pid, "pdf"))
        self.assertFalse(excel_cache.begin(pid, "excel"))
        excel_cache.finish(pid, kind="pdf")
        self.assertEqual(excel_cache.status(pid, fp, "excel"), {"status": "running"})
        excel_cache.finish(pid, error="boom", kind="excel")
        self.assertEqual(excel_cache.status(pid, fp, "pdf"), {"status": "none"})
        self.assertEqual(excel_cache.status(pid, fp, "excel")["status"], "failed")

    def test_both_prepared_at_once_through_the_routes(self):
        self.assertEqual(self.ep(g.prepare_excel), {"status": "running"})
        self.assertEqual(self.ep(g.prepare_pdf), {"status": "running"})
        self.assertEqual([a[-1] for _, a in self.queued], ["excel", "pdf"])
        self.run_queued()
        self.assertEqual(self.ep(g.excel_status), {"status": "ready"})
        self.assertEqual(self.ep(g.pdf_status), {"status": "ready"})
        self.assertEqual(body(self.ep(g.download_excel)), b"XLSX Cache test #1")
        pdf = self.ep(g.download_pdf)
        self.assertEqual(body(pdf), b"PDF Cache test #1")
        self.assertEqual(pdf.media_type, "application/pdf")
        self.assertEqual(self.calls, {"excel": 1, "pdf": 1})

    def test_concurrent_builds_on_real_threads(self):
        # The Excel build is held open while the PDF build runs to the end beside it.
        release = threading.Event()
        excel_started = threading.Event()

        def slow_excel(db, project):
            excel_started.set()
            release.wait(10)
            return b"XLSX slow", "model.xlsx", XLSX

        g._build_excel_file = slow_excel
        pid, fp = self.project.id, self.fp()
        excel_cache.begin(pid, "excel")
        excel_cache.begin(pid, "pdf")
        tx = threading.Thread(target=g._build_job, args=(pid, fp, "excel"))
        tp = threading.Thread(target=g._build_job, args=(pid, fp, "pdf"))
        tx.start()
        self.assertTrue(excel_started.wait(10))
        tp.start()
        tp.join(10)
        self.assertEqual(excel_cache.status(pid, fp, "pdf"), {"status": "ready"})
        self.assertEqual(excel_cache.status(pid, fp, "excel"), {"status": "running"})
        release.set()
        tx.join(10)
        self.assertEqual(excel_cache.status(pid, fp, "excel"), {"status": "ready"})
        self.assertEqual(excel_cache.get(pid, fp, "excel")[0], b"XLSX slow")
        self.assertEqual(excel_cache.get(pid, fp, "pdf")[0], b"PDF Cache test #1")

    def test_a_failed_pdf_leaves_the_excel_alone(self):
        def broken(db, project):
            raise RuntimeError("PDF export needs LibreOffice")
        g._build_pdf_file = broken
        self.ep(g.prepare_excel)
        self.ep(g.prepare_pdf)
        self.run_queued()
        self.assertEqual(self.ep(g.excel_status), {"status": "ready"})
        st = self.ep(g.pdf_status)
        self.assertEqual(st["status"], "failed")
        self.assertIn("LibreOffice", st["error"])

    def test_an_edit_makes_the_old_pdf_unservable(self):
        self.ep(g.download_pdf)
        self.rename("Edited title")
        self.assertEqual(self.ep(g.pdf_status), {"status": "none"})
        self.assertEqual(body(self.ep(g.download_pdf)), b"PDF Edited title #2")

    def test_an_edited_answer_changes_the_fingerprint(self):
        before = self.fp()
        qa = self.db.query(QuestionnaireAnswer).filter_by(project_id=self.project.id).first()
        qa.collected_data = '{"a": 2}'
        self.db.commit()
        self.assertNotEqual(self.fp(), before)

    def test_an_old_style_cache_file_breaks_nothing(self):
        # Before the PDF change the Excel was kept as {id}.bin / {id}.json. A file left there
        # by the previous deploy -- even one for this exact data -- is simply not read.
        pid, fp = self.project.id, self.fp()
        with open(os.path.join(self.cache_dir, f"{pid}.bin"), "wb") as fh:
            fh.write(b"OLD XLSX")
        with open(os.path.join(self.cache_dir, f"{pid}.json"), "w", encoding="utf-8") as fh:
            fh.write('{"fp": "%s", "fname": "old.xlsx", "media": "%s"}' % (fp, XLSX))
        self.assertIsNone(excel_cache.get(pid, fp))
        self.assertEqual(self.ep(g.excel_status), {"status": "none"})
        self.assertEqual(body(self.ep(g.download_excel)), b"XLSX Cache test #1")
        self.assertEqual(self.ep(g.excel_status), {"status": "ready"})

    def test_a_corrupt_cache_file_is_a_miss_not_an_error(self):
        pid, fp = self.project.id, self.fp()
        excel_cache.put(pid, fp, b"X", "a.xlsx", XLSX)
        with open(os.path.join(self.cache_dir, f"{pid}.excel.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(excel_cache.get(pid, fp))
        self.assertEqual(self.ep(g.excel_status), {"status": "none"})


if __name__ == "__main__":
    unittest.main()
