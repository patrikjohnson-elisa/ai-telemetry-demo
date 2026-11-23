import json
import tempfile
import unittest
from pathlib import Path

from backfill_vscode_chat_otel import build_traces, load_session_file, set_path


class VSCodeChatBackfillTest(unittest.TestCase):
    def test_set_and_append_paths(self):
        state = {"requests": []}
        set_path(state, ["requests"], [{"id": "one", "response": []}], append=True)
        set_path(state, ["requests", 0, "response"], [{"value": "a"}], append=True)
        set_path(state, ["requests", 0, "tokens"], 12)
        self.assertEqual(state["requests"][0]["response"][0]["value"], "a")
        self.assertEqual(state["requests"][0]["tokens"], 12)

    def test_replays_journal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            path = Path(handle.name)
            handle.write(json.dumps({"kind": 0, "v": {"sessionId": "s", "requests": []}}) + "\n")
            handle.write(json.dumps({"kind": 2, "k": ["requests"], "v": [{"requestId": "r", "response": []}]}) + "\n")
            handle.write(json.dumps({"kind": 1, "k": ["requests", 0, "completionTokens"], "v": 4}) + "\n")
        try:
            state = load_session_file(path)
            self.assertEqual(state["requests"][0]["completionTokens"], 4)
        finally:
            path.unlink()

    def test_trace_ids_are_deterministic(self):
        from backfill_vscode_chat_otel import ChatRequest
        item = ChatRequest("s", "r", 1000, 2000, "model", "prompt", "response", 10, 2, 1.5, 1000, 50, "agent", "agent", "/tmp", "hash", "title", [])
        first = build_traces([item]).SerializeToString()
        second = build_traces([item]).SerializeToString()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
