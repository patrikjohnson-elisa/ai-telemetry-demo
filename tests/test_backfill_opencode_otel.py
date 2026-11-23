import sqlite3
import tempfile
import unittest
from pathlib import Path

from backfill_opencode_otel import build_payloads, load_history, open_database, parser, payload_summary


class BackfillTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = Path(handle.name)
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE session (
              id TEXT, project_id TEXT, parent_id TEXT, title TEXT, version TEXT,
              directory TEXT, time_created INTEGER, time_updated INTEGER,
              agent TEXT, model TEXT, metadata TEXT
            );
            CREATE TABLE message (
              id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (
              id TEXT, message_id TEXT, session_id TEXT,
              time_created INTEGER, time_updated INTEGER, data TEXT
            );
        """)
        db.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "session-1", "project-1", None, "test", "1.18.20", "/tmp", 1000, 4000,
            "build", '{"providerID":"provider","id":"model"}', "{}",
        ))
        db.execute("INSERT INTO message VALUES (?,?,?,?,?)", (
            "user-1", "session-1", 1100, 1100,
            '{"role":"user","time":{"created":1100},"agent":"build",'
            '"model":{"providerID":"provider","modelID":"model"}}',
        ))
        db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (
            "prompt-1", "user-1", "session-1", 1100, 1100,
            '{"type":"text","text":"hello"}',
        ))
        db.execute("INSERT INTO message VALUES (?,?,?,?,?)", (
            "assistant-1", "session-1", 1200, 3000,
            '{"role":"assistant","time":{"created":1200,"completed":3000},'
            '"parentID":"user-1","modelID":"model","providerID":"provider",'
            '"mode":"build","cost":0.25,"tokens":{"input":10,"output":5,'
            '"reasoning":2,"cache":{"read":3,"write":1}},"finish":"stop"}',
        ))
        db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (
            "answer-1", "assistant-1", "session-1", 2900, 2900,
            '{"type":"text","text":"world"}',
        ))
        # Legacy migrations can stamp non-tool parts much later than the actual run.
        db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (
            "step-1", "assistant-1", "session-1", 999999, 999999,
            '{"type":"step-finish"}',
        ))
        db.commit()
        db.close()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_builds_expected_signals_and_historical_times(self):
        with open_database(self.path) as db:
            history = load_history(db, None)
        payloads = build_payloads(history, "test.")
        summary = payload_summary(payloads)
        self.assertEqual(summary["trace_spans"], 2)
        self.assertEqual(summary["log_records"], 4)
        self.assertEqual(payloads.min_ms, 1000)
        self.assertEqual(payloads.max_ms, 3000)
        llm = payloads.traces.resource_spans[0].scope_spans[0].spans[1]
        self.assertEqual(llm.start_time_unix_nano, 1_200_000_000)
        self.assertEqual(llm.end_time_unix_nano, 3_000_000_000)
        self.assertTrue(llm.parent_span_id)

    def test_ids_are_deterministic(self):
        with open_database(self.path) as db:
            history = load_history(db, None)
        first = build_payloads(history, "test.").traces.SerializeToString()
        second = build_payloads(history, "test.").traces.SerializeToString()
        self.assertEqual(first, second)

    def test_creation_cutoff(self):
        with open_database(self.path) as db:
            included = load_history(db, None, created_before_ms=1001)
            excluded = load_history(db, None, created_before_ms=1000)
        self.assertEqual(len(included.sessions), 1)
        self.assertEqual(len(excluded.sessions), 0)

    def test_raw_streams_and_no_metrics_are_defaults(self):
        args = parser().parse_args(["export", "--dry-run"])
        self.assertEqual(args.trace_stream, "opencode_raw")
        self.assertEqual(args.log_stream, "opencode_raw")
        self.assertFalse(args.include_metrics)

    def test_metrics_can_be_enabled_explicitly(self):
        args = parser().parse_args(["export", "--dry-run", "--include-metrics"])
        self.assertTrue(args.include_metrics)


if __name__ == "__main__":
    unittest.main()
