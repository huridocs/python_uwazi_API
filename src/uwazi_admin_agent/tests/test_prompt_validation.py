"""Isolated unit tests for the prompt-validation domain models + pure builder.

Per ``AGENTS.md``: no mocks/stubs, no network, no env creds. These tests exercise
pure construction and the deterministic :func:`build_final_prompt` seam with
literal inputs.
"""

from uwazi_admin_agent.domain.clarifying_question import ClarifyingQuestion
from uwazi_admin_agent.domain.prompt_understanding import PromptUnderstanding
from uwazi_admin_agent.domain.prompt_validation import QuestionAnswer, build_final_prompt


def test_build_final_prompt_no_answers_no_notes_returns_original() -> None:
    assert build_final_prompt("Merge duplicates", [], "") == "Merge duplicates"


def test_build_final_prompt_folds_selected_options() -> None:
    answers = [QuestionAnswer(question="Which template?", selected=["Judgment", "Order"])]
    result = build_final_prompt("Merge duplicates", answers, "")
    assert "Merge duplicates" in result
    assert "Which template?" in result
    assert "Judgment" in result
    assert "Order" in result


def test_build_final_prompt_folds_custom_answer() -> None:
    answers = [QuestionAnswer(question="Which template?", selected=[], custom="All templates")]
    result = build_final_prompt("Merge duplicates", answers, "")
    assert "All templates" in result


def test_build_final_prompt_omits_unresolved_questions() -> None:
    answers = [QuestionAnswer(question="Which template?", selected=[], custom=None)]
    result = build_final_prompt("Merge duplicates", answers, "")
    assert "Which template?" not in result
    assert result == "Merge duplicates"


def test_build_final_prompt_appends_notes() -> None:
    result = build_final_prompt("Merge duplicates", [], "Only published entities")
    assert "Additional notes:" in result
    assert "Only published entities" in result


def test_build_final_prompt_ignores_blank_notes() -> None:
    assert build_final_prompt("Merge duplicates", [], "   ") == "Merge duplicates"


def test_clarifying_question_defaults() -> None:
    question = ClarifyingQuestion(question="Which template?")
    assert question.options == []


def test_prompt_understanding_defaults() -> None:
    understanding = PromptUnderstanding(summary="Merge duplicate entities.")
    assert understanding.questions == []
