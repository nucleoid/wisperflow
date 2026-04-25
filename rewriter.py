"""LLM rewriter: turn verbatim transcription into polished writing.

This is the piece that makes it feel like Whisper Flow rather than raw dictation.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from config import CONFIG

SYSTEM_PROMPT = """\
You are a silent dictation post-processor. Your job: take a raw speech-to-text
transcription and produce the text the speaker actually intended to *write*.

HARD RULES
- Output ONLY the rewritten text. No preamble, no quotes, no explanation, no
  markdown fences. Never say "Here is" or "Sure". Never ask a question.
- Preserve the speaker's meaning, voice, and register (casual stays casual,
  formal stays formal). Do not invent facts, names, numbers, or details.
- Do not answer questions in the transcription. If the speaker asks "what time
  is it", output "What time is it?" — do not answer it.
- If the input is empty, whitespace, or obviously garbled noise, output nothing.

CLEAN UP
- Remove filler words and verbal tics: um, uh, er, ah, like, you know, I mean,
  sort of, kind of, basically, literally (when used as filler), right?, so...
- Remove false starts, stutters, and self-corrections. Keep only the final
  version the speaker converged on. Example:
    "I think we should — actually let's just ship it tomorrow"
    → "Let's just ship it tomorrow."
- Remove filler repetitions ("the the", "I I"). Keep intentional repetition.
- Fix run-ons, subject/verb agreement, tense slips, and dropped articles.

PUNCTUATION & FORMATTING
- Add correct punctuation and capitalization. Use sentence case.
- Break into paragraphs when the speaker clearly shifts topic; otherwise keep
  it as flowing prose. Do not add headings or bullet points unless the speaker
  explicitly dictates a list ("first… second… third…" or "bullet one…").
- If the speaker dictates punctuation explicitly ("comma", "period", "new
  line", "new paragraph", "question mark", "open quote"/"close quote"), apply
  it and DROP the spoken word.
- Numbers: keep as spoken unless the speaker says a figure ("twenty dollars"
  stays as words; "I spent $20" only if they say "dollar sign two zero").
- Preserve technical terms, product names, file paths, URLs, and code verbatim.
  If unsure whether a token is code vs. prose, prefer code.

COMMANDS vs. PROSE
- Short imperatives stay short. "send it" → "Send it." Not a paragraph.
- One-word answers stay one word. "yes" → "Yes." (or "Yes,")

WHEN UNSURE
- Make the smallest edit that yields fluent, natural written English.
- Never paraphrase for style. The speaker's words > your words.

EXAMPLES (study these carefully — your output must match this style)

Input:  <transcription>hello does this work</transcription>
Output: Hello, does this work?

Input:  <transcription>what time is it</transcription>
Output: What time is it?

Input:  <transcription>are you there</transcription>
Output: Are you there?

Input:  <transcription>can you send me the report</transcription>
Output: Can you send me the report?

Input:  <transcription>yes</transcription>
Output: Yes.

Input:  <transcription>send it</transcription>
Output: Send it.

Input:  <transcription>um so I was thinking like maybe we should uh ship it tomorrow</transcription>
Output: I was thinking maybe we should ship it tomorrow.

Input:  <transcription>I think we should — actually let's just ship it tomorrow</transcription>
Output: Let's just ship it tomorrow.

Notice: questions stay as questions (you NEVER answer them). Greetings stay as
greetings. Short input stays short. You only fix punctuation, capitalization,
fillers, and false starts. You do not respond, comment, or add anything.
"""


@dataclass
class RewriteResult:
    text: str
    used_llm: bool
    latency_ms: float


def _strip_wrapping_quotes(out: str) -> str:
    # Strip accidental wrapping quotes/fences if the model slipped.
    for pair in (('"', '"'), ("'", "'"), ("`", "`")):
        if len(out) >= 2 and out.startswith(pair[0]) and out.endswith(pair[1]):
            out = out[1:-1].strip()
    return out


class Rewriter:
    def __init__(self) -> None:
        self._backend: str = "none"
        self._client = None
        if CONFIG.rewriter == "claude":
            if not CONFIG.anthropic_api_key:
                print(
                    "[rewriter] ANTHROPIC_API_KEY not set — falling back to raw transcription.",
                    flush=True,
                )
                return
            # Lazy import so the app starts without the SDK if rewriter=none.
            from anthropic import Anthropic

            self._client = Anthropic(api_key=CONFIG.anthropic_api_key)
            self._backend = "claude"
        elif CONFIG.rewriter == "ollama":
            # Ollama is accessed over HTTP — no Python dep required.
            self._backend = "ollama"

    def _build_prompt(self, text: str, context: str) -> tuple[str, str]:
        system = SYSTEM_PROMPT
        if CONFIG.style_hints:
            system += f"\n\nADDITIONAL STYLE GUIDANCE FROM USER:\n{CONFIG.style_hints}"
        user_content = f"<transcription>\n{text}\n</transcription>"
        if context:
            user_content = f"<context>\n{context}\n</context>\n\n" + user_content
        return system, user_content

    def _rewrite_claude(self, system: str, user_content: str) -> str:
        msg = self._client.messages.create(  # type: ignore[union-attr]
            model=CONFIG.anthropic_model,
            max_tokens=CONFIG.rewriter_max_tokens,
            temperature=CONFIG.rewriter_temperature,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        ).strip()

    def _rewrite_ollama(self, system: str, user_content: str) -> str:
        payload = {
            "model": CONFIG.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "options": {
                "temperature": CONFIG.rewriter_temperature,
                # Cap so a misbehaving model can't hang the dictation pipeline.
                "num_predict": CONFIG.rewriter_max_tokens,
            },
        }
        req = urllib.request.Request(
            f"{CONFIG.ollama_host.rstrip('/')}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"ollama request failed: {e}") from e
        return (data.get("message", {}).get("content") or "").strip()

    def rewrite(self, text: str, *, context: str = "") -> RewriteResult:
        t0 = time.monotonic()
        stripped = text.strip()
        if not stripped or self._backend == "none":
            return RewriteResult(stripped, used_llm=False, latency_ms=0.0)

        system, user_content = self._build_prompt(stripped, context)
        try:
            if self._backend == "claude":
                out = self._rewrite_claude(system, user_content)
            elif self._backend == "ollama":
                out = self._rewrite_ollama(system, user_content)
            else:
                return RewriteResult(stripped, used_llm=False, latency_ms=0.0)
        except Exception as e:  # noqa: BLE001
            # Never swallow dictation on a rewriter failure — paste the raw.
            print(f"[rewriter] {self._backend} failed: {e!r} — using raw text.", flush=True)
            return RewriteResult(stripped, used_llm=False, latency_ms=(time.monotonic() - t0) * 1000)

        out = _strip_wrapping_quotes(out)
        return RewriteResult(
            text=out or stripped,
            used_llm=True,
            latency_ms=(time.monotonic() - t0) * 1000,
        )
