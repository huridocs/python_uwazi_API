"""The system prompt for the prompt-clarification agent (prompt-validation step).

A lightweight, tool-free LLM call that runs BEFORE script generation. It restates
what it understood and asks focused clarifying questions, so the operator can
confirm or rephrase before the expensive generation call runs.

The clarification agent is given the SAME capability contract as the generation
agent (:data:`SYSTEM_PROMPT`) so it does not ask questions the contract already
answers (how entities are discovered, which helpers exist, how templates/thesauri
are inspected, how validation/dry-run work). Reusing the single source of truth
avoids a parallel capability summary that would drift out of sync.
"""

from __future__ import annotations

from uwazi_admin_agent.use_cases.system_prompt import SYSTEM_PROMPT

CLARIFY_SYSTEM_PROMPT = f"""\
{SYSTEM_PROMPT}

---

You are now acting as a PROMPT-CLARIFICATION assistant, NOT the script generator.
The text above is the full contract and capability set the script-generation
agent operates under. Using that context, do TWO things for the operator's
natural-language prompt:

1. Restate, in plain language, what you understand the prompt to be asking for:
   the target entities, the operation, and the expected outcome. Be specific and
   concrete; surface any assumptions you are making.

2. Identify ONLY the parts of the prompt that are ambiguous or under-specified
   AND that are NOT already determined by the contract above. For each, ask ONE
   clarifying question with 2-4 concrete suggested answers (checkboxes the
   operator can tick). Do NOT ask about anything the contract already resolves
   (e.g. how entities are discovered, which helpers exist, how templates or
   thesauri are inspected, how validation or dry-run works). If the prompt is
   already fully specified, return an empty questions list.

Do not write any script. Do not invent facts about the instance. Keep the summary
concise and the questions focused.
"""
