"""Secret detection + redaction (M6).

What is under test:

* Detection precision on a labeled set: real-format credentials are found;
  benign code/prompts are not (precision is the contract — false
  accusations on someone's source code are worse than missed formats).
* Redaction: typed placeholders, deterministic, overlap-safe, deep payload
  walk, original never mutated.
* Digest hygiene: after redaction, two payloads that differ only in secret
  material fingerprint identically — a cache key can never encode a secret.
* Scan cap: a pathological string scans its head and stops.
"""
import unittest

from tokan.pipeline import OptimizationPipeline
from tokan.secret import (
    SCAN_LIMIT,
    SECRET_PATTERNS,
    count_payload_secrets,
    detect_secrets,
    redact_payload,
    redact_text,
)

# Labeled positives — real *formats* (structure + prefix), never live secrets.
POSITIVES = {
    "aws_access_key_id": "AKIA" + "A1B2C3D4E5F6G7H8",
    "github_token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "openai_key": "sk-" + "p" * 40,
    "anthropic_key": "sk-ant-" + "a" * 40,
    "google_api_key": "AIza" + "A" * 35,
    "slack_token": "xoxb-" + "1" * 20 + "-" + "a" * 20,
    "stripe_key": "sk_live_" + "T" * 24,
    "npm_token": "npm_" + "a1B2" * 9,
    "jwt": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
    "private_key_block": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCA...",
}

# Labeled negatives — things that LOOK security-adjacent but are not secrets.
NEGATIVES = [
    "def compute_total(invoice): return invoice.amount * 1.19",
    "The sk- prefix convention is discussed in the style guide.",  # no key body
    "Set OPENAI_API_KEY in your environment before running tests.",
    "curl -H 'Authorization: Bearer $TOKEN' https://api.example.com",
    "class JWTValidator: pass  # jwt handling, no token embedded",
    "Version 1.2.3 of the widget was released on 2026-01-02.",
    "passwords must be at least 12 characters (see the policy doc)",
    "The Eiffel Tower is 330 metres tall.",
    "AKIA is the AWS key prefix; ours rotate every 90 days.",  # prefix word only
    "-----BEGIN CERTIFICATE-----\nMIIBkTCB+wIJAJ\n-----END CERTIFICATE-----",
]


class TestDetectionPrecision(unittest.TestCase):
    def test_every_labeled_positive_is_found_with_its_type(self):
        for expected_type, sample in POSITIVES.items():
            with self.subTest(secret=expected_type):
                found = {f.type for f in detect_secrets(sample)}
                self.assertIn(expected_type, found)

    def test_no_labeled_negative_is_flagged(self):
        for text in NEGATIVES:
            with self.subTest(text=text[:40]):
                self.assertEqual(detect_secrets(text), [], f"false positive: {text[:60]!r}")

    def test_openai_and_anthropic_keys_classify_distinctly(self):
        # sk-ant-... must NOT land in the openai bucket (the (?!ant-) guard).
        self.assertEqual(
            {f.type for f in detect_secrets(POSITIVES["anthropic_key"])},
            {"anthropic_key"},
        )

    def test_clean_text_has_no_findings(self):
        self.assertEqual(detect_secrets(""), [])
        self.assertEqual(detect_secrets("plain conversation, no credentials"), [])


class TestRedaction(unittest.TestCase):
    def test_replacement_uses_typed_placeholders(self):
        text, counts = redact_text(f"token {POSITIVES['github_token']} here")
        self.assertEqual(text, "token [REDACTED:github_token] here")
        self.assertEqual(counts, {"github_token": 1})

    def test_redaction_is_deterministic(self):
        text = f"aws {POSITIVES['aws_access_key_id']} jwt {POSITIVES['jwt']}"
        first = redact_text(text)
        second = redact_text(text)
        self.assertEqual(first, second)

    def test_overlapping_patterns_count_once(self):
        # sk-ant- matches the anthropic pattern; the openai pattern is
        # excluded by (?!ant-), but the de-overlap rule still guards any
        # future multi-pattern span.
        text, counts = redact_text(POSITIVES["anthropic_key"])
        self.assertEqual(sum(counts.values()), 1)
        self.assertEqual(text, "[REDACTED:anthropic_key]")

    def test_payload_walk_is_deep_and_preserves_structure(self):
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": f"fix {POSITIVES['openai_key']} please"},
            ],
            "temperature": 0,
            "tools": [{"type": "function", "function": {"name": "run",
                      "parameters": {"example": POSITIVES["slack_token"]}}}],
        }
        redacted, counts = redact_payload(payload)
        self.assertEqual(counts, {"openai_key": 1, "slack_token": 1})
        self.assertEqual(redacted["model"], "gpt-4o")
        self.assertEqual(redacted["temperature"], 0)
        self.assertEqual(redacted["messages"][1]["content"], "fix [REDACTED:openai_key] please")
        self.assertEqual(
            redacted["tools"][0]["function"]["parameters"]["example"],
            "[REDACTED:slack_token]",
        )
        # original untouched
        self.assertIn(POSITIVES["openai_key"], payload["messages"][1]["content"])

    def test_count_matches_redaction(self):
        payload = {"a": POSITIVES["github_token"], "b": [POSITIVES["jwt"], "clean"]}
        self.assertEqual(
            sum(count_payload_secrets(payload).values()),
            sum(redact_payload(payload)[1].values()),
        )

    def test_scan_cap_stops_at_the_head(self):
        needle = POSITIVES["openai_key"]
        big = needle + "x" * (SCAN_LIMIT + 1000)
        findings = detect_secrets(big)
        self.assertTrue(all(f.end <= SCAN_LIMIT for f in findings))
        # a secret planted AFTER the cap is not seen (documented limitation)
        planted = "x" * (SCAN_LIMIT + 10) + needle
        self.assertEqual(detect_secrets(planted), [])


class TestDigestHygiene(unittest.TestCase):
    """After redaction, the cache key can never encode the secret itself."""

    def test_payloads_differing_only_in_secret_fingerprint_identically(self):
        pipe = OptimizationPipeline()
        secret_a = POSITIVES["openai_key"]
        secret_b = "sk-" + "z" * 40
        pa, _ = redact_payload({"model": "m", "messages": [
            {"role": "user", "content": f"review this config: API_KEY={secret_a}"}]})
        pb, _ = redact_payload({"model": "m", "messages": [
            {"role": "user", "content": f"review this config: API_KEY={secret_b}"}]})
        ra = pipe.prepare(pa)
        rb = pipe.prepare(pb)
        self.assertEqual(ra.record.digest, rb.record.digest)
        # and the digest itself contains no trace of either secret
        self.assertNotIn(secret_a[:12], ra.record.digest)
        self.assertNotIn(secret_b[:12], ra.record.digest)

    def test_no_flagged_pattern_appears_in_any_digest_input(self):
        # The placeholder is structural, not secret material.
        pipe = OptimizationPipeline()
        redacted, _ = redact_payload({"model": "m", "messages": [
            {"role": "user", "content": POSITIVES["private_key_block"]}]})
        result = pipe.prepare(redacted)
        self.assertIn("[REDACTED:private_key_block]",
                      redacted["messages"][0]["content"])
        self.assertNotIn("MIIEpAIBAAKCA", result.record.digest)


class TestPatternSet(unittest.TestCase):
    def test_every_pattern_compiles_and_has_a_name(self):
        for name, pattern in SECRET_PATTERNS:
            self.assertTrue(name)
            self.assertTrue(hasattr(pattern, "finditer"))


if __name__ == "__main__":
    unittest.main()
