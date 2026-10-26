#!/usr/bin/env python3
"""Doc Governor benchmark: retrieval vs full-document resend.

The claim under test (honest, measurable):

    Document Q&A normally resends the ENTIRE document on every question.
    The Doc Governor sends [extractive summary + top-k retrieved chunks +
    question] instead. The benchmark measures BOTH sides of the trade:

      * cost  — prompt tokens per question, full-doc baseline vs doc mode
      * quality — recall@k: is the answer-bearing chunk actually in the
        retrieved set? (If the gold fact never reaches the prompt, the model
        cannot answer — that is the quality proxy, measured not asserted.)

Modes:
    (default)   Offline and deterministic: builds a ~70-clause synthetic
                contract with 8 known gold facts, ingests it, and evaluates
                every question. Also runs an *extractive oracle* simulation:
                an answer exists iff the gold fact is present in the prompt
                the model would receive.
    --live      Real comparison against the Copilot upstream: each question
                runs twice — full-doc resend (naive) vs /v1/doc/ask — and the
                answers are checked for the gold value. Costs real tokens on
                your Copilot subscription (bounded by --live-questions).

    python3 benchmarks/doc_qa.py
    python3 benchmarks/doc_qa.py --live --live-questions 3
    python3 benchmarks/doc_qa.py --top-k 5

Stdlib only. Exits 0 always (it reports; it does not gate CI).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokan.cdm import estimate_tokens  # noqa: E402
from tokan.doc import Document, assemble_messages, full_resend_tokens  # noqa: E402


# -------------------------------------------------------------------- fixture
FACTS = {
    "governing law": "Section 12 (Governing law): this Agreement is governed by the laws of the State of Delaware.",
    "termination": "Section 19 (Termination): either party may terminate this Agreement upon sixty (60) days written notice.",
    "payment": "Section 27 (Payment): all invoices are payable net thirty (30) days from the invoice date.",
    "liability": "Section 34 (Limitation of liability): aggregate liability is capped at $500,000 per contract year.",
    "confidentiality": "Section 41 (Confidentiality): these obligations survive termination for five (5) years.",
    "arbitration": "Section 48 (Dispute resolution): all disputes shall be resolved by binding arbitration in Chicago.",
    "insurance": "Section 55 (Insurance): the Vendor shall maintain cyber liability coverage of at least $2,000,000.",
    "audit": "Section 62 (Audit rights): the Customer may audit the books no more than once per calendar year.",
}

#: Short, stable answer substrings used to grade real (live) answers.
GOLD_VALUES = {
    "governing law": ["delaware"],
    "termination": ["60", "sixty"],
    "payment": ["thirty", "30 days"],
    "liability": ["500,000"],
    "confidentiality": ["five", "5 years"],
    "arbitration": ["chicago"],
    "insurance": ["2,000,000"],
    "audit": ["once"],
}

QUESTIONS = {
    "governing law": "Which state's law governs this agreement?",
    "termination": "How much notice is required to terminate?",
    "payment": "What are the payment terms for invoices?",
    "liability": "What is the cap on liability?",
    "confidentiality": "How long do confidentiality obligations survive?",
    "arbitration": "Where does dispute resolution happen?",
    "insurance": "How much cyber liability insurance is required?",
    "audit": "How often can the customer audit the books?",
}

_FILLER = (
    "Section {n} (Miscellaneous): the parties shall keep complete and accurate "
    "books and records relating to the services described herein, and shall "
    "provide reasonable access to such records upon request during normal "
    "business hours. Neither party may assign its rights without prior written "
    "consent except in connection with a merger or sale of substantially all "
    "assets. Notices must be delivered by registered mail or internationally "
    "recognized courier to the addresses listed in the preamble."
)

_FACT_CLAUSES = {12: "governing law", 19: "termination", 27: "payment",
                 34: "liability", 41: "confidentiality", 48: "arbitration",
                 55: "insurance", 62: "audit"}


def contract() -> str:
    lines = [
        "MASTER SERVICES AGREEMENT",
        "This Master Services Agreement is entered into by Acme Customer and "
        "Beta Vendor, effective as of the first day of the fiscal year.",
    ]
    for n in range(1, 71):
        keyword = _FACT_CLAUSES.get(n)
        lines.append(FACTS[keyword] if keyword else _FILLER.format(n=n))
    return "\n\n".join(lines)


def tokens_of(messages) -> int:
    """Token cost of a message list, serialized the same way for both modes."""
    return estimate_tokens(json.dumps(messages, sort_keys=True, ensure_ascii=False))


def baseline_messages(doc_text: str, question: str):
    """The naive client: whole document + question, every single time."""
    return [{"role": "user", "content": f"{doc_text}\n\nQuestion: {question}"}]


# ---------------------------------------------------------------- offline run
def run_offline(top_k: int) -> None:
    text = contract()
    doc = Document("benchmark-contract", text)
    rows = []
    for keyword, question in QUESTIONS.items():
        gold_fact = FACTS[keyword]
        gold_chunk = next(i for i, c in enumerate(doc.chunks) if gold_fact in c)

        full = full_resend_tokens(doc, question)
        messages, idxs, _ = assemble_messages(doc, question, top_k=top_k)
        sent = tokens_of(messages)

        # Quality: did the answer-bearing chunk make it into the prompt?
        hit_at_k = gold_chunk in idxs
        hit_at_1 = bool(idxs) and idxs[0] == gold_chunk
        # Oracle: answer exists iff the gold fact is present in the prompt.
        prompt = "\n".join(m["content"] for m in messages)
        answerable = gold_fact in prompt

        rows.append({
            "keyword": keyword, "full": full, "sent": sent,
            "delta": full - sent, "pct": 100.0 * (full - sent) / full,
            "hit_at_1": hit_at_1, "hit_at_k": hit_at_k, "answerable": answerable,
        })

    total_full = sum(r["full"] for r in rows)
    total_sent = sum(r["sent"] for r in rows)
    recall1 = sum(r["hit_at_1"] for r in rows)
    recallk = sum(r["hit_at_k"] for r in rows)
    answerable = sum(r["answerable"] for r in rows)
    n = len(rows)

    print(f"Doc Governor benchmark (offline) — {n}-question QA over one document")
    print(f"document: {doc.total_tokens:,} tok, {doc.chunk_count} chunks, "
          f"summary {doc.summary_tokens:,} tok, top_k={top_k}\n")
    print(f"{'question':<16} {'full-doc':>9} {'doc-mode':>9} {'saved':>9} {'save%':>7} "
          f"{'hit@1':>6} {'hit@k':>6} {'answerable':>10}")
    print("─" * 84)
    for r in rows:
        print(f"{r['keyword']:<16} {r['full']:>9,} {r['sent']:>9,} {r['delta']:>9,} "
              f"{r['pct']:>6.1f}% {'yes' if r['hit_at_1'] else 'no':>6} "
              f"{'yes' if r['hit_at_k'] else 'no':>6} "
              f"{'yes' if r['answerable'] else 'NO':>10}")
    print("─" * 84)
    print(f"{'TOTAL/AVG':<16} {total_full:>9,} {total_sent:>9,} "
          f"{total_full - total_sent:>9,} {100.0 * (total_full - total_sent) / total_full:>6.1f}% "
          f"{recall1:>3}/{n} {recallk:>3}/{n} {answerable:>6}/{n}")
    print(f"\ncontext as % of full document: "
          f"{100.0 * total_sent / total_full:.1f}% average")
    print("\nReading this honestly:")
    print(f" • cost: doc mode spends {total_sent:,} tokens where the naive flow")
    print(f"   spends {total_full:,} — {100.0 * (total_full - total_sent) / total_full:.1f}% fewer, because only the")
    print("   summary + retrieved excerpts travel (the rest stays server-side).")
    print(f" • quality: recall@1 = {recall1}/{n}, recall@{top_k} = {recallk}/{n}; "
          "the gold fact was present in the prompt")
    print(f"   for {answerable}/{n} questions (extractive-oracle answerability).")
    print(" • retrieval is TF-IDF (lexical): paraphrases that share no vocabulary")
    print("   with the document are a real limitation — measure, don't assume.")


# ------------------------------------------------------------------ live run
def _post(url: str, payload: dict, timeout: float = 180.0):
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _graded(answer: str, keyword: str) -> bool:
    lowered = answer.lower()
    return any(v in lowered for v in GOLD_VALUES[keyword])


def run_live(top_k: int, questions: int, port: int, model: str) -> None:
    text = contract()
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    if not token:
        print("gh is not authenticated — run `gh auth login` first.")
        return
    log_path = os.path.join("/tmp", "doc-qa-gw.log")
    log = open(log_path, "w")
    gw = subprocess.Popen(
        [sys.executable, "-m", "tokan", "serve", "--copilot", "--api-key", token,
         "--port", str(port), "--log-requests", "--fallback-model", model],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    time.sleep(4)
    base = f"http://127.0.0.1:{port}"
    keywords = list(QUESTIONS)[:questions]
    try:
        ing = _post(f"{base}/v1/ingest", {"text": text})
        doc = Document("live", text)  # client-side mirror for baseline costs
        print(f"Doc Governor benchmark (LIVE on Copilot) — {len(keywords)} questions")
        print(f"document: {ing['total_tokens']:,} tok -> doc_id {ing['doc_id']}, "
              f"{ing['chunks']} chunks, summary {ing['summary_tokens']:,} tok\n")
        print(f"{'question':<16} {'mode':<9} {'prompt':>8} {'gold?':>6}  answer")
        print("─" * 100)
        totals = {"full": 0, "sent": 0, "full_ok": 0, "doc_ok": 0}
        for keyword in keywords:
            question = QUESTIONS[keyword]

            # Baseline: resend the whole document, like a naive client.
            base_resp = _post(f"{base}/v1/chat/completions", {
                "model": model,
                "messages": [{"role": "user",
                              "content": f"{text}\n\nQuestion: {question}"}],
            })
            base_prompt = int(base_resp.get("usage", {}).get("prompt_tokens", 0))
            base_answer = base_resp["choices"][0]["message"]["content"]
            base_ok = _graded(base_answer, keyword)
            totals["full"] += base_prompt
            totals["full_ok"] += base_ok

            # Doc mode: summary + retrieved excerpts only.
            doc_resp = _post(f"{base}/v1/doc/ask", {
                "doc_id": ing["doc_id"], "question": question, "model": model,
                "top_k": top_k,
            })
            doc_prompt = int(doc_resp.get("usage", {}).get("prompt_tokens", 0))
            doc_answer = doc_resp["choices"][0]["message"]["content"]
            doc_ok = _graded(doc_answer, keyword)
            totals["sent"] += doc_prompt
            totals["doc_ok"] += doc_ok

            short_a = " ".join(base_answer.split())[:44]
            short_b = " ".join(doc_answer.split())[:44]
            print(f"{keyword:<16} {'full-doc':<9} {base_prompt:>8,} "
                  f"{'yes' if base_ok else 'NO':>6}  {short_a}...")
            print(f"{'':<16} {'doc':<9} {doc_prompt:>8,} "
                  f"{'yes' if doc_ok else 'NO':>6}  {short_b}...")
        print("─" * 100)
        saved = totals["full"] - totals["sent"]
        pct = 100.0 * saved / totals["full"] if totals["full"] else 0.0
        print(f"prompt tokens: full-doc {totals['full']:,} vs doc {totals['sent']:,} "
              f"-> {saved:,} saved ({pct:.1f}%)")
        print(f"gold answers graded correct: full-doc {totals['full_ok']}/{len(keywords)}, "
              f"doc mode {totals['doc_ok']}/{len(keywords)}")
        print("\nReading this honestly:")
        print(" • Both modes see real Copilot; grading checks for the gold value,")
        print("   so a wrong/absent answer counts as a miss in BOTH columns.")
        print(" • Fewer prompt tokens is only a win if answers stay correct —")
        print("   the two 'gold?' columns are the trade-off, side by side.")
    finally:
        gw.terminate()
        gw.wait(timeout=5)
        log.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-k", type=int, default=3, help="retrieved excerpts per question")
    ap.add_argument("--live", action="store_true",
                    help="run against the real Copilot upstream (needs gh auth)")
    ap.add_argument("--live-questions", type=int, default=3,
                    help="questions to run in --live mode (bounds token spend)")
    ap.add_argument("--port", type=int, default=4101, help="gateway port for --live")
    ap.add_argument("--model", default="gpt-4o-mini", help="model id for --live")
    args = ap.parse_args()
    if args.live:
        run_live(max(1, min(10, args.top_k)), max(1, args.live_questions),
                 args.port, args.model)
    else:
        run_offline(max(1, min(10, args.top_k)))


if __name__ == "__main__":
    main()
