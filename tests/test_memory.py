"""Conversation memory: store integrity, state extraction, manifest, extraction."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from tokan.memory import (
    ContextManifest,
    ConversationState,
    ConversationStore,
    extract_state,
)
from tokan.proxy.interceptor import _last_payload_message


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "conv.db")

    def tearDown(self):
        self.dir.cleanup()


class TestConversationStore(_StoreCase):
    def test_append_assigns_sequential_versions_and_reads_back(self):
        store = ConversationStore(self.path)
        e1 = store.append("c1", source="user", kind="message", content={"text": "hi"})
        e2 = store.append("c1", source="assistant", kind="message", content={"text": "hello"})
        self.assertEqual((e1.version, e2.version), (1, 2))
        events = store.events("c1")
        self.assertEqual([e.content["text"] for e in events], ["hi", "hello"])
        self.assertEqual(store.last("c1").source, "assistant")

    def test_independent_conversations(self):
        store = ConversationStore(self.path)
        store.append("a", source="user", kind="message", content={"text": "for a"})
        store.append("b", source="user", kind="message", content={"text": "for b"})
        self.assertEqual(len(store.events("a")), 1)
        self.assertEqual(store.events("b")[0].content["text"], "for b")
        self.assertEqual(store.conversations(), ["a", "b"])
        self.assertIsNone(store.last("never-seen"))

    def test_integrity_verify_detects_tampering(self):
        store = ConversationStore(self.path)
        store.append("c1", source="user", kind="message", content={"text": "original"})
        store.close()
        # Tamper directly with the database (bypassing the API).
        conn = sqlite3.connect(self.path)
        conn.execute(
            "UPDATE conversation_events SET content = ? WHERE conversation = ?",
            (json.dumps({"text": "tampered"}), "c1"),
        )
        conn.commit()
        conn.close()
        store = ConversationStore(self.path)
        self.assertEqual(store.verify("c1"), 1)
        with self.assertRaises(ValueError):
            store.events("c1", verify=True)

    def test_verify_clean_store_reports_zero(self):
        store = ConversationStore(self.path)
        store.append("c1", source="user", kind="message", content={"text": "a"})
        store.append("c1", source="user", kind="message", content={"text": "b"})
        self.assertEqual(store.verify("c1"), 0)

    def test_retention_prune_only_with_policy(self):
        store = ConversationStore(self.path, retention_seconds=3600)
        old = store.append(
            "c1", source="user", kind="message", content={"text": "old"}, ts=1.0
        )
        store.append("c1", source="user", kind="message", content={"text": "new"})
        removed = store.prune()
        self.assertEqual(removed, 1)
        self.assertEqual([e.content["text"] for e in store.events("c1")], ["new"])
        self.assertNotIn(old.id, [e.id for e in store.events("c1")])

    def test_no_retention_never_prunes(self):
        store = ConversationStore(self.path)
        store.append("c1", source="user", kind="message", content={"text": "keep"}, ts=1.0)
        self.assertEqual(store.prune(), 0)
        self.assertEqual(len(store.events("c1")), 1)

    def test_tenant_isolation(self):
        a = ConversationStore(self.path, tenant="team-a")
        b = ConversationStore(self.path, tenant="team-b")
        a.append("shared-id", source="user", kind="message", content={"text": "a's secret"})
        self.assertEqual(b.events("shared-id"), [])
        b.append("shared-id", source="user", kind="message", content={"text": "b's data"})
        self.assertEqual(a.events("shared-id")[0].content["text"], "a's secret")
        self.assertEqual(b.events("shared-id")[0].content["text"], "b's data")

    def test_concurrent_appends_are_unique_and_ordered(self):
        store = ConversationStore(self.path)

        def writer(n):
            for i in range(25):
                store.append(
                    f"conv-{n}", source="user", kind="message",
                    content={"text": f"t{n}-{i}"},
                )

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for n in range(8):
            versions = [e.version for e in store.events(f"conv-{n}")]
            self.assertEqual(versions, list(range(1, 26)))


class TestStateExtraction(_StoreCase):
    def _conversation(self, texts_and_sources):
        store = ConversationStore(self.path)
        for source, text in texts_and_sources:
            store.append("c1", source=source, kind="message", content={"text": text})
        return store.events("c1")

    def test_objective_is_latest_user_message(self):
        events = self._conversation([
            ("user", "first ask"),
            ("assistant", "first answer"),
            ("user", "second ask about pipeline.py"),
        ])
        state = extract_state(events)
        self.assertEqual(state.objective, "second ask about pipeline.py")
        self.assertEqual(state.version, events[-1].version)

    def test_constraints_extracted_from_user_messages(self):
        events = self._conversation([
            ("user", "Refactor the module. Never modify tests/. Always keep imports sorted."),
            ("assistant", "done"),
        ])
        state = extract_state(events)
        joined = " ".join(state.constraints)
        self.assertIn("Never modify tests/.", joined)
        self.assertIn("Always keep imports sorted.", joined)

    def test_files_tracked_across_conversation(self):
        events = self._conversation([
            ("user", "look at tokan/pipeline.py"),
            ("assistant", "see tokan/cache/edge_cache.py for the LRU"),
        ])
        state = extract_state(events)
        self.assertIn("tokan/pipeline.py", state.files)
        self.assertIn("tokan/cache/edge_cache.py", state.files)

    def test_open_questions_from_latest_user_message(self):
        events = self._conversation([
            ("user", "should we shard?\nwhich db?\nplain statement"),
        ])
        state = extract_state(events)
        self.assertEqual(state.open_questions, ["should we shard?", "which db?"])

    def test_conflicting_objectives_latest_wins_history_retained(self):
        events = self._conversation([
            ("user", "objective A"),
            ("user", "objective B"),
        ])
        state = extract_state(events)
        self.assertEqual(state.objective, "objective B")
        # Both facts remain retrievable from the log — nothing overwritten.
        self.assertEqual([e.content["text"] for e in events], ["objective A", "objective B"])

    def test_empty_events(self):
        state = extract_state([])
        self.assertEqual(state, ConversationState(conversation=""))


class TestManifest(unittest.TestCase):
    def test_as_dict_carries_ledger_fields(self):
        m = ContextManifest(
            rid="r1", conversation="c1", ts=1.0, protocol="openai", route="llm",
            status=200, model="gpt-x", digest="abc123", original_tokens=100,
            optimized_tokens=80, protected={"system_messages": 2},
            included={"messages": 5},
        )
        d = m.as_dict()
        self.assertEqual(d["rid"], "r1")
        self.assertEqual(d["protected"]["system_messages"], 2)
        self.assertEqual(d["omitted"], [])
        self.assertEqual(d["policy"]["version"], "cdm-1")
        # Round-trips through JSON (it is stored as an event payload).
        self.assertEqual(json.loads(json.dumps(d)), d)


class TestLastPayloadMessage(unittest.TestCase):
    def test_openai_chat_string_and_list_content(self):
        payload = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "the question"},
            ]
        }
        self.assertEqual(_last_payload_message(payload, "openai"), ("user", "the question"))
        payload["messages"].append({
            "role": "assistant", "content": [{"type": "text", "text": "prior answer"}],
        })
        self.assertEqual(
            _last_payload_message(payload, "openai"), ("assistant", "prior answer")
        )

    def test_responses_input_items(self):
        payload = {
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q1"}]},
                {"type": "function_call", "name": "view", "arguments": "{}"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q2"}]},
            ]
        }
        self.assertEqual(_last_payload_message(payload, "openai"), ("user", "q2"))

    def test_anthropic_blocks(self):
        payload = {
            "messages": [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            ]
        }
        self.assertEqual(_last_payload_message(payload, "anthropic"), ("assistant", "answer"))

    def test_gemini_parts(self):
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": "q"}]},
                {"role": "model", "parts": [{"text": "a"}, {"text": "more"}]},
            ]
        }
        role, text = _last_payload_message(payload, "gemini")
        self.assertEqual(role, "model")
        self.assertIn("a", text)
        self.assertIn("more", text)

    def test_empty_and_tool_only_payloads(self):
        self.assertIsNone(_last_payload_message({}, "openai"))
        self.assertIsNone(_last_payload_message({"messages": [{"role": "user", "content": ""}]}, "openai"))
        self.assertIsNone(
            _last_payload_message(
                {"input": [{"type": "reasoning", "summary": []}]}, "openai"
            )
        )


if __name__ == "__main__":
    unittest.main()
