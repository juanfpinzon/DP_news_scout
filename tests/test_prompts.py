from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_context_preamble_includes_procurement_scope_and_priority_signals() -> None:
    prompt = (ROOT / "prompts" / "context_preamble.md").read_text(encoding="utf-8")

    assert "PepsiCo" in prompt
    assert "Digital Procurement" in prompt
    assert "Source-to-Pay (S2P)" in prompt
    assert "Procure-to-Pay (P2P)" in prompt
    assert "+2 bonus" in prompt

    for platform in [
        "SAP / SAP Ariba",
        "Archlet",
        "Keelvar",
        "Selectica",
        "SpendhQ",
        "Pirt",
        "Tirzo",
    ]:
        assert platform in prompt

    for company in [
        "Unilever",
        "Mars",
        "Mondelez",
        "Procter & Gamble",
        "Kraft Heinz",
    ]:
        assert company in prompt

    assert "Fortune 500 CPG company" in prompt
    assert "General corporate news" in prompt


def test_relevance_scoring_prompt_includes_rubric_and_json_contract() -> None:
    prompt = (ROOT / "prompts" / "relevance_scoring.md").read_text(encoding="utf-8")

    assert "score from 1 to 10" in prompt
    assert "+2 relevance bonus" in prompt
    assert "Return strict JSON only" in prompt
    assert '"scores"' in prompt
    assert '"url"' in prompt
    assert '"score"' in prompt
    assert '"reasoning"' in prompt
    assert "Direct procurement technology" in prompt
    assert "Competitive intelligence" in prompt


def test_digest_composition_prompt_includes_voice_and_json_contract() -> None:
    prompt = (ROOT / "prompts" / "digest_composition.md").read_text(encoding="utf-8")
    normalized = prompt.lower()

    assert "trusted advisor's morning brief" in prompt
    assert "Return strict JSON only" in prompt
    assert "top_story" in prompt
    assert "key_developments" in prompt
    assert "on_our_radar" in prompt
    assert "quick_hits" in prompt
    assert "why_it_matters" in prompt
    assert "one_liner" in prompt
    assert "url may appear only once" in normalized
