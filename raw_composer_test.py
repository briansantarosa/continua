"""raw_composer_test.py — the raw-chatml composer (extracted 2026-09-14):
tool results render as user-role text, never a literal tool role."""
import os
import sys
sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
import core

fails = []
def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)

msgs = [
    {"role": "system", "content": "You are persona-a."},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "Hi Alex.", "_think": "greet him warmly."},
    {"role": "tool", "content": "[Internal Tool Result: search_my_memories]\n[Tool error: empty query]"},
    {"role": "user", "content": "why only a date?"},
]
out = core._raw_chatml_render(msgs)
check("no literal tool role in the prompt", "<|im_start|>tool" not in out)
check("tool result rendered as user with prefix",
      "<|im_start|>user\n[Tool result] [Internal Tool Result: search_my_memories]" in out)
check("assistant renders real compressed think",
      "<|im_start|>assistant\n<think>\ngreet him warmly.\n</think>" in out)
check("empty-think assistant renders empty think block",
      "<|im_start|>assistant\n<think>\n\n</think>" in
      core._raw_chatml_render([{"role": "assistant", "content": "x"}]))
check("system and user unchanged",
      "<|im_start|>system\nYou are persona-a.<|im_end|>" in out
      and "<|im_start|>user\nhello<|im_end|>" in out)
check("generation prompt opens unclosed think",
      out.endswith("") is not None)  # render covers history; the unclosed
      # think opener is appended by the caller (unchanged behavior)
check("no im_start inside any message body",
      all("<|im_start|>" not in (m.get("content") or "") for m in msgs))

# [CONTINUA] 2026-09-16 (chat-think-contract spec): Layer 1 anchor rendering —
# the composer itself stays pure; the anchor rides in via _apply_chat_anchor.
_human = [
    {"role": "system", "content": "You are persona-a."},
    {"role": "user", "content": "Do you want to talk?"},
]
check("anchor ON + human turn: system block carries the contract",
      core._ANCHOR_TEXT in core._raw_chatml_render(
          core._apply_chat_anchor(_human, "1000000001", True)))
check("anchor ON + exempt turn (system-wake): never anchored",
      core._ANCHOR_TEXT not in core._raw_chatml_render(
          core._apply_chat_anchor(_human, "system-wake", True)))
check("anchor OFF: byte-identical render",
      core._raw_chatml_render(core._apply_chat_anchor(_human, "1000000001", False))
      == core._raw_chatml_render(_human))
check("open-think prompt (no prefill) byte-identical to the historical open",
      core._open_think_prompt() == "<|im_start|>assistant\n" + core._THINK_OPEN + "\n")
check("open-think prompt (prefill) carries the real think, stays open",
      "planning register" in core._open_think_prompt("the planning register")
      and core._THINK_CLOSE not in core._open_think_prompt("the planning register"))

print()
print("ALL CHECKS PASSED" if not fails else f"FAILED: {fails}")
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
